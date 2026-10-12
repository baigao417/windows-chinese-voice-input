from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]

BAD_FEEDBACK_CATEGORIES: list[dict[str, str]] = [
    {"slug": "long_sentence", "label": "长句"},
    {"slug": "filler_words", "label": "口头禅多"},
    {"slug": "mixed_zh_en", "label": "中英夹杂"},
    {"slug": "proper_nouns", "label": "专有名词"},
    {"slug": "punctuation", "label": "标点差"},
    {"slug": "structure_failed", "label": "结构化失败"},
    {"slug": "asr_error", "label": "ASR 听错"},
]


def log(message: str) -> None:
    print(message, flush=True)


def beep(kind: str) -> None:
    try:
        import winsound

        if kind == "start":
            winsound.Beep(880, 120)
        elif kind == "stop":
            winsound.Beep(660, 120)
        elif kind == "done":
            winsound.Beep(1040, 120)
        else:
            winsound.Beep(320, 180)
    except Exception:
        pass


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_config(path: str | None = None) -> dict[str, Any]:
    example = ROOT / "config.example.json"
    cfg = read_json(example)
    config_path = Path(path) if path else ROOT / "config.json"
    if config_path.exists():
        cfg = deep_merge(cfg, read_json(config_path))
    return cfg


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def normalize_bad_feedback_category(value: str) -> dict[str, str]:
    normalized = str(value or "").strip()
    for category in BAD_FEEDBACK_CATEGORIES:
        if normalized in {category["slug"], category["label"]}:
            return category
    allowed = ", ".join(f'{c["label"]}({c["slug"]})' for c in BAD_FEEDBACK_CATEGORIES)
    raise RuntimeError(f"unknown bad-result category: {value}; allowed: {allowed}")


def path_for_log(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_text_file(value: str | None) -> str:
    p = resolve_path(value)
    if not p or not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def dictionary_paths(cfg: dict[str, Any]) -> list[Path]:
    values: list[str] = []
    configured_paths = cfg.get("dictionary_paths")
    if isinstance(configured_paths, list):
        values.extend(str(item) for item in configured_paths if is_set(item))
    elif is_set(cfg.get("dictionary_path")):
        values.append(str(cfg["dictionary_path"]))
    if is_set(cfg.get("dictionary_path")) and str(cfg["dictionary_path"]) not in values:
        values.insert(0, str(cfg["dictionary_path"]))

    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = resolve_path(value)
        if not path:
            continue
        key = str(path).lower()
        if key not in seen:
            paths.append(path)
            seen.add(key)
    return paths


def _dictionary_entry_from_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [part.strip() for part in line.split("|")]
    phrase = parts[0]
    if not phrase:
        return None
    aliases: list[str] = []
    if len(parts) > 1 and parts[1]:
        aliases = [item.strip() for item in re.split(r"[,，;；]", parts[1]) if item.strip()]
    return {
        "phrase": phrase,
        "aliases": aliases,
        "pronunciation": parts[2] if len(parts) > 2 else "",
        "notes": parts[3] if len(parts) > 3 else "",
    }


def _normalize_dictionary_entry(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        return _dictionary_entry_from_line(raw)
    if not isinstance(raw, dict):
        return None
    phrase = str(raw.get("phrase") or raw.get("word") or raw.get("term") or "").strip()
    if not phrase:
        return None
    aliases_raw = raw.get("aliases") or raw.get("alias") or []
    if isinstance(aliases_raw, str):
        aliases = [item.strip() for item in re.split(r"[,，;；]", aliases_raw) if item.strip()]
    elif isinstance(aliases_raw, list):
        aliases = [str(item).strip() for item in aliases_raw if str(item).strip()]
    else:
        aliases = []
    return {
        "phrase": phrase,
        "aliases": aliases,
        "pronunciation": str(raw.get("pronunciation") or "").strip(),
        "notes": str(raw.get("notes") or raw.get("note") or "").strip(),
    }


def load_dictionary_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in dictionary_paths(cfg):
        if not path.exists():
            continue
        try:
            if path.suffix.lower() == ".jsonl":
                candidates = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
            elif path.suffix.lower() == ".json":
                payload = read_json(path)
                candidates = payload.get("entries", payload) if isinstance(payload, dict) else payload
            else:
                candidates = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            log(f"[dictionary] failed to load {path}: {e}")
            continue

        for candidate in candidates if isinstance(candidates, list) else []:
            entry = _normalize_dictionary_entry(candidate)
            if not entry:
                continue
            key = entry["phrase"].lower()
            if key in seen:
                continue
            entries.append(entry)
            seen.add(key)
    return entries


def local_dictionary_path(cfg: dict[str, Any]) -> Path:
    for path in dictionary_paths(cfg):
        if path.name.lower() == "dictionary.local.jsonl":
            return path
    return ROOT / "dictionary.local.jsonl"


def load_local_dictionary_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = local_dictionary_path(cfg)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError:
            raw = line
        entry = _normalize_dictionary_entry(raw)
        if entry:
            entries.append(entry)
    return entries


def save_local_dictionary_entries(cfg: dict[str, Any], entries: list[dict[str, Any]]) -> Path:
    path = local_dictionary_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        clean = _normalize_dictionary_entry(entry)
        if not clean:
            continue
        key = clean["phrase"].lower()
        if key in seen:
            continue
        normalized.append(clean)
        seen.add(key)
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in normalized)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
    return path


def upsert_local_dictionary_entry(
    cfg: dict[str, Any],
    *,
    phrase: str,
    aliases: list[str] | None = None,
    pronunciation: str = "",
    notes: str = "",
) -> int:
    clean = _normalize_dictionary_entry(
        {
            "phrase": phrase,
            "aliases": aliases or [],
            "pronunciation": pronunciation,
            "notes": notes,
        }
    )
    if not clean:
        raise ValueError("phrase is required")
    entries = load_local_dictionary_entries(cfg)
    key = clean["phrase"].lower()
    replaced = False
    for idx, entry in enumerate(entries):
        if entry["phrase"].lower() == key:
            entries[idx] = clean
            replaced = True
            break
    if not replaced:
        entries.append(clean)
    save_local_dictionary_entries(cfg, entries)
    return len(entries)


def delete_local_dictionary_entry(cfg: dict[str, Any], phrase: str) -> bool:
    key = phrase.strip().lower()
    if not key:
        return False
    entries = load_local_dictionary_entries(cfg)
    remaining = [entry for entry in entries if entry["phrase"].lower() != key]
    if len(remaining) == len(entries):
        return False
    save_local_dictionary_entries(cfg, remaining)
    return True


def format_dictionary_for_prompt(cfg: dict[str, Any]) -> str:
    lines: list[str] = []
    for entry in load_dictionary_entries(cfg):
        details: list[str] = []
        if entry.get("aliases"):
            details.append("aliases: " + ", ".join(entry["aliases"]))
        if entry.get("pronunciation"):
            details.append("pronunciation: " + str(entry["pronunciation"]))
        if entry.get("notes"):
            details.append("notes: " + str(entry["notes"]))
        line = str(entry["phrase"])
        if details:
            line += " (" + "; ".join(details) + ")"
        lines.append(line)
    return "\n".join(lines)


def dictionary_initial_prompt(cfg: dict[str, Any]) -> str:
    max_terms = int(cfg.get("dictionary_max_initial_prompt_terms", 120))
    terms: list[str] = []
    for entry in load_dictionary_entries(cfg):
        terms.append(str(entry["phrase"]))
        if entry.get("pronunciation"):
            terms.append(str(entry["pronunciation"]))
        if len(terms) >= max_terms:
            break
    terms = [term for term in terms if term.strip()][:max_terms]
    return "常见专有名词：" + "，".join(terms) if terms else ""


def dictionary_summary(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    entries = load_dictionary_entries(cfg)
    stt = cfg.get("stt", {})
    stt_engine = str(stt.get("engine") or "")
    return [
        {
            "path": str(path),
            "exists": path.exists(),
        }
        for path in dictionary_paths(cfg)
    ] + [
        {"total_entries": len(entries)},
        {
            "applies_to": [
                "rewrite_prompt",
                "faster_whisper_fallback_initial_prompt",
            ],
            "cloud_asr": (
                "not_sent_to_siliconflow_api; official docs/openapi only confirm file+model"
                if stt_engine == "siliconflow"
                else "not_applicable"
            ),
        },
    ]


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"api_key", "access_key", "secret_key", "token", "password"}:
                redacted[key] = "***" if is_set(item) else item
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def strip_qwen_thinking(text: str) -> str:
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    return text.strip().strip("`").strip()


def rewrite_safety_reasons(source: str, output: str) -> list[str]:
    if not output.strip():
        return ["empty_output"]
    source_len = max(1, len(source.strip()))
    output_len = len(output.strip())
    max_allowed_len = max(800, source_len * 6)
    if output_len > max_allowed_len:
        return [f"output_expanded_too_much:{output_len}>{max_allowed_len}"]

    source_patterns = [
        "帮我",
        "告诉我",
        "请你",
        "怎么",
        "如何",
        "写一个",
        "写一下",
        "总结",
        "翻译",
        "ask",
        "tell",
        "write",
        "summarize",
        "translate",
    ]
    answer_patterns = [
        "以下是",
        "当然",
        "好的",
        "好，",
        "好:",
        "好：",
        "你可以",
        "建议你",
        "我来帮你",
        "帮你整理",
        "为你整理",
        "整理一下",
        "作为",
        "抱歉",
        "无法",
        "here is",
        "sure",
        "certainly",
        "as an ai",
    ]
    src = source.lower()
    prefix = output.strip()[:500].lower()
    source_has_instruction = any(p.lower() in src for p in source_patterns)
    output_looks_answerish = any(p.lower() in prefix for p in answer_patterns)
    reasons: list[str] = []
    if source_has_instruction and output_looks_answerish:
        reasons.append("source_instruction_output_answerish")
    if any(p in output.lower() for p in ["as an ai", "作为ai", "作为 ai", "我不能", "无法帮助"]):
        reasons.append("assistant_disclaimer")
    return reasons


def is_set(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def multipart_form(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----voice-input-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode())
    parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
    parts.append(file_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def get_json(url: str, api_key: str, timeout_seconds: float = 20) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e


@dataclass
class RecordedAudio:
    path: Path
    seconds: float


@dataclass
class PasteResult:
    method: str
    copied: bool
    pasted: bool
    guarded: bool = False
    guard_reason: str | None = None


@dataclass(frozen=True)
class WindowSnapshot:
    hwnd: int
    pid: int | None = None

    def summary(self) -> str:
        pid = self.pid if self.pid is not None else "unknown"
        return f"hwnd=0x{self.hwnd:x}; pid={pid}"


class AudioRecorder:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._stream: Any = None
        self._frames: list[Any] = []
        self._started_at = 0.0
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("already recording")

            import sounddevice as sd

            audio_cfg = self.cfg["audio"]
            self._frames = []

            def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                if status:
                    log(f"[audio] {status}")
                self._frames.append(indata.copy())

            self._stream = sd.InputStream(
                samplerate=int(audio_cfg["sample_rate"]),
                channels=int(audio_cfg["channels"]),
                dtype="float32",
                device=audio_cfg.get("device"),
                callback=callback,
            )
            self._stream.start()
            self._started_at = time.time()

    def stop_to_wav(self) -> RecordedAudio:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("not recording")

            stream = self._stream
            self._stream = None
            stream.stop()
            stream.close()
            seconds = max(0.0, time.time() - self._started_at)

            if not self._frames:
                raise RuntimeError("no audio frames captured")

            import numpy as np

            audio = np.concatenate(self._frames, axis=0)
            audio = np.clip(audio, -1.0, 1.0)
            pcm = (audio * 32767).astype(np.int16)

            tmp_dir = Path(tempfile.gettempdir()) / "windows-typeless-voice-input"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = tmp_dir / f"recording-{int(time.time())}.wav"

            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(int(self.cfg["audio"]["channels"]))
                wf.setsampwidth(2)
                wf.setframerate(int(self.cfg["audio"]["sample_rate"]))
                wf.writeframes(pcm.tobytes())

            return RecordedAudio(path=wav_path, seconds=seconds)


class WhisperTranscriber:
    def __init__(self, cfg: dict[str, Any], stt_cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.stt_cfg = stt_cfg or cfg["stt"]
        self._model: Any = None
        self._device: str | None = None
        self._compute_type: str | None = None

    def _resolve_device(self) -> str:
        requested = self.stt_cfg.get("device", "auto")
        if requested != "auto":
            return requested
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    def _resolve_compute_type(self, device: str) -> str:
        requested = self.stt_cfg.get("compute_type", "auto")
        if requested != "auto":
            return requested
        return "float16" if device == "cuda" else "int8"

    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        from faster_whisper import WhisperModel

        stt = self.stt_cfg
        self._device = self._resolve_device()
        self._compute_type = self._resolve_compute_type(self._device)
        cache_dir = resolve_path(stt.get("model_cache_dir"))
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

        log(
            "[stt] loading model "
            f"{stt['model']} on {self._device} ({self._compute_type}); "
            "first run may download model files"
        )
        self._model = WhisperModel(
            stt["model"],
            device=self._device,
            compute_type=self._compute_type,
            download_root=str(cache_dir) if cache_dir else None,
        )
        return self._model

    def transcribe(self, audio_path: Path) -> str:
        model = self._load()
        stt = self.stt_cfg
        initial_prompt = dictionary_initial_prompt(self.cfg)

        t0 = time.time()
        segments, info = model.transcribe(
            str(audio_path),
            language=stt.get("language") or None,
            beam_size=int(stt.get("beam_size", 5)),
            vad_filter=bool(stt.get("vad_filter", True)),
            initial_prompt=initial_prompt or None,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        elapsed = time.time() - t0
        lang = getattr(info, "language", "unknown")
        log(f"[stt] done in {elapsed:.1f}s; detected={lang}; chars={len(text)}")
        return text


class SiliconFlowTranscriber:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def _api_key(self) -> str:
        stt = self.cfg["stt"]
        direct = stt.get("api_key")
        if is_set(direct):
            return str(direct)

        env_name = str(stt.get("api_key_env") or "SILICONFLOW_API_KEY")
        from_env = os.environ.get(env_name)
        if is_set(from_env):
            return str(from_env)

        secrets_file = resolve_path(stt.get("secrets_file"))
        if secrets_file and secrets_file.exists():
            provider = str(stt.get("secrets_provider") or "siliconflow")
            key_name = str(stt.get("secrets_key") or "api_key")
            try:
                secret_cfg = read_json(secrets_file).get(provider, {})
                from_file = secret_cfg.get(key_name)
                if is_set(from_file):
                    return str(from_file)
            except Exception:
                pass

        raise RuntimeError(
            "SiliconFlow API key missing. Set SILICONFLOW_API_KEY or configure stt.secrets_file."
        )

    def transcribe(self, audio_path: Path) -> str:
        stt = self.cfg["stt"]
        model = str(stt.get("model") or "TeleAI/TeleSpeechASR")
        base_url = str(stt.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
        url = f"{base_url}/audio/transcriptions"
        fields = {"model": model}
        language = stt.get("language")
        if is_set(language):
            fields["language"] = str(language)
        body, content_type = multipart_form(fields, "file", audio_path)
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(
                req, timeout=float(stt.get("timeout_seconds", 180))
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"SiliconFlow ASR HTTP {e.code}: {detail}") from e

        text = str(payload.get("text") or "").strip()
        elapsed = time.time() - t0
        log(f"[stt:siliconflow] {model} done in {elapsed:.1f}s; chars={len(text)}")
        if not text:
            raise RuntimeError(f"SiliconFlow ASR returned empty text: {payload}")
        return text


class TranscriberRouter:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        stt = cfg["stt"]
        engine = str(stt.get("engine") or "faster-whisper")
        if engine == "siliconflow":
            self.primary: Any = SiliconFlowTranscriber(cfg)
        elif engine == "faster-whisper":
            self.primary = WhisperTranscriber(cfg)
        else:
            raise RuntimeError(f"unsupported stt engine: {engine}")

        self.fallback: Any | None = None
        fallback_cfg = stt.get("fallback")
        if isinstance(fallback_cfg, dict) and fallback_cfg.get("enabled", True):
            fallback_engine = fallback_cfg.get("engine")
            if fallback_engine == "faster-whisper":
                self.fallback = WhisperTranscriber(cfg, fallback_cfg)
            else:
                raise RuntimeError(f"unsupported stt fallback engine: {fallback_engine}")

    def transcribe(self, audio_path: Path) -> str:
        try:
            return self.primary.transcribe(audio_path)
        except Exception as e:
            if not self.fallback:
                raise
            log(f"[stt] primary failed, falling back to local whisper: {e}")
            return self.fallback.transcribe(audio_path)


def normalize_llm_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for source_key, target_key in [
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
    ]:
        raw = value.get(source_key)
        if raw is None or raw == "":
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            out[target_key] = parsed
    if "total_tokens" not in out and ("prompt_tokens" in out or "completion_tokens" in out):
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out or None


class TextEnhancer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.last_model: str | None = None
        self.last_usage: dict[str, int] | None = None

    def enhance(self, mode: str, transcript: str) -> str:
        self.last_model = None
        self.last_usage = None
        llm = self.cfg["llm"]
        if not llm.get("enabled", True):
            return transcript

        mode_cfg = self.cfg["modes"][mode]
        prompt = read_text_file(mode_cfg["prompt_path"])
        dictionary = format_dictionary_for_prompt(self.cfg)
        user_content = (
            "<custom_vocabulary>\n"
            f"{dictionary}\n"
            "</custom_vocabulary>\n\n"
            "<transcript>\n"
            f"{transcript}\n"
            "</transcript>"
        )

        errors: list[str] = []
        for model in self._models(llm):
            t0 = time.time()
            try:
                content = self._chat(llm, model, prompt, user_content)
                enhanced = strip_qwen_thinking(content)
                reasons = rewrite_safety_reasons(transcript, enhanced)
                if reasons:
                    raise RuntimeError("rewrite safety rejected: " + ",".join(reasons))
                elapsed = time.time() - t0
                self.last_model = model
                log(f"[llm:{llm.get('provider')}] {model} done in {elapsed:.1f}s; chars={len(enhanced)}")
                return enhanced
            except Exception as e:
                errors.append(f"{model}: {e}")
                log(f"[llm] {model} failed: {e}")

        raise RuntimeError("all rewrite models failed: " + " | ".join(errors))

    def _models(self, llm: dict[str, Any]) -> list[str]:
        models: list[str] = []
        primary = llm.get("model")
        if is_set(primary):
            models.append(str(primary))
        fallback = llm.get("fallback_model")
        if is_set(fallback) and str(fallback) not in models:
            models.append(str(fallback))
        for item in llm.get("fallback_models") or []:
            if is_set(item) and str(item) not in models:
                models.append(str(item))
        if not models:
            raise RuntimeError("llm.model is required")
        return models

    def _chat(self, llm: dict[str, Any], model: str, prompt: str, user_content: str) -> str:
        provider = str(llm.get("provider") or "ollama")
        if provider == "ollama":
            return self._chat_ollama(llm, model, prompt, user_content)
        if provider == "openai-compatible":
            return self._chat_openai_compatible(llm, model, prompt, user_content)
        raise RuntimeError(f"unsupported llm provider: {provider}")

    def _chat_ollama(
        self, llm: dict[str, Any], model: str, prompt: str, user_content: str
    ) -> str:
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": prompt
                    + "\n\n额外约束：不要输出思考过程，不要输出 <think> 标签。",
                },
                {"role": "user", "content": user_content},
            ],
            "options": {"temperature": float(llm.get("temperature", 0.2))},
        }

        base = str(llm["base_url"]).rstrip("/")
        url = f"{base}/api/chat"
        try:
            body = self._post_json(url, payload, float(llm.get("timeout_seconds", 180)))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code == 404:
                raise RuntimeError(
                    f"Ollama model not available: {model}. "
                    f"Run: ollama pull {model}. {detail}"
                ) from e
            raise RuntimeError(f"Ollama HTTP {e.code}: {detail}") from e
        self.last_usage = normalize_llm_usage(
            {
                "prompt_tokens": body.get("prompt_eval_count"),
                "completion_tokens": body.get("eval_count"),
            }
        )
        return str(body.get("message", {}).get("content") or body.get("response") or "")

    def _chat_openai_compatible(
        self, llm: dict[str, Any], model: str, prompt: str, user_content: str
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": prompt
                    + "\n\n额外约束：不要输出思考过程，不要输出 <think> 标签。",
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": float(llm.get("temperature", 0.2)),
            "max_tokens": int(llm.get("max_tokens", 1024)),
        }

        base = str(llm.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
        url = f"{base}/chat/completions"
        body = self._post_json(
            url,
            payload,
            float(llm.get("timeout_seconds", 180)),
            headers={"Authorization": f"Bearer {self._api_key(llm)}"},
        )
        self.last_usage = normalize_llm_usage(body.get("usage"))
        choices = body.get("choices") or []
        if not choices:
            return ""
        choice = choices[0] or {}
        message = choice.get("message") or {}
        return self._content_to_text(message.get("content") or choice.get("text") or "")

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    def _api_key(self, llm: dict[str, Any]) -> str:
        direct = llm.get("api_key")
        if is_set(direct):
            return str(direct)

        env_name = str(llm.get("api_key_env") or "SILICONFLOW_API_KEY")
        from_env = os.environ.get(env_name)
        if is_set(from_env):
            return str(from_env)

        secrets_file = resolve_path(llm.get("secrets_file"))
        if secrets_file and secrets_file.exists():
            provider = str(llm.get("secrets_provider") or "siliconflow")
            key_name = str(llm.get("secrets_key") or "api_key")
            try:
                secret_cfg = read_json(secrets_file).get(provider, {})
                from_file = secret_cfg.get(key_name)
                if is_set(from_file):
                    return str(from_file)
            except Exception:
                pass

        raise RuntimeError(
            "LLM API key missing. Set api_key_env or configure llm.secrets_file."
        )

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces: list[str] = []
            for part in content:
                if isinstance(part, str):
                    pieces.append(part)
                elif isinstance(part, dict):
                    value = part.get("text") or part.get("content")
                    if isinstance(value, str):
                        pieces.append(value)
            return "".join(pieces)
        return str(content or "")


class ResultStore:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("last_result_path") or "logs/last-result.json")
        if self.path is None:
            raise RuntimeError("history.last_result_path is invalid")

    def save(
        self,
        *,
        text: str,
        raw_transcript: str,
        mode: str,
        rewrite_degraded: bool,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": mode,
            "rewrite_degraded": bool(rewrite_degraded),
            "chars": len(text),
            "raw_chars": len(raw_transcript),
            "text": text,
            "raw_transcript": raw_transcript,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log(f"[history] saved last result: {self.path}")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise RuntimeError(f"last result not found: {self.path}")
        payload = read_json(self.path)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise RuntimeError(f"last result is empty: {self.path}")
        return payload

    def load_text(self) -> str:
        return str(self.load()["text"])


class UsageStore:
    def __init__(self, cfg: dict[str, Any]) -> None:
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("usage_log_path") or "logs/usage.jsonl")
        if self.path is None:
            raise RuntimeError("history.usage_log_path is invalid")

    def append(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"text", "raw_transcript", "transcript"}
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        log(f"[usage] appended metadata: {self.path}")


class FeedbackStore:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("feedback_log_path") or "logs/bad-feedback.jsonl")
        self.case_dir = resolve_path(history_cfg.get("bad_case_dir") or "logs/bad-cases")
        if self.path is None:
            raise RuntimeError("history.feedback_log_path is invalid")
        if self.case_dir is None:
            raise RuntimeError("history.bad_case_dir is invalid")

    def mark_bad(self, category: str, *, source: str, note: str = "") -> dict[str, Any]:
        selected = normalize_bad_feedback_category(category)
        result = ResultStore(self.cfg).load()
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        feedback_id = f"bad-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        case_path = self.case_dir / f"{feedback_id}.json"
        case_payload = {
            "id": feedback_id,
            "created_at": created_at,
            "source": source,
            "category": selected["label"],
            "category_slug": selected["slug"],
            "note": note,
            "last_result": result,
        }
        case_path.write_text(
            json.dumps(case_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        index_payload = {
            "id": feedback_id,
            "created_at": created_at,
            "source": source,
            "category": selected["label"],
            "category_slug": selected["slug"],
            "note": note,
            "last_result_created_at": result.get("created_at"),
            "mode": result.get("mode"),
            "rewrite_degraded": bool(result.get("rewrite_degraded")),
            "chars": result.get("chars"),
            "raw_chars": result.get("raw_chars"),
            "case_path": path_for_log(case_path),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(index_payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        log(
            f"[feedback] marked bad: category={selected['label']} id={feedback_id} "
            f"case={path_for_log(case_path)}"
        )
        return index_payload

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())


class RuntimeStatusStore:
    def __init__(self, cfg: dict[str, Any]) -> None:
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("daemon_status_path") or "logs/daemon-status.json")
        if self.path is None:
            raise RuntimeError("history.daemon_status_path is invalid")

    def write(self, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[str, Any] = {}
        if self.path.exists():
            try:
                previous = read_json(self.path)
            except Exception:
                previous = {}
        payload = {
            **previous,
            **fields,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return read_json(self.path)

    def summary(self) -> str:
        try:
            payload = self.load()
        except Exception as e:
            return f"Daemon status: unreadable ({e}); path={self.path}"
        if not payload:
            return f"Daemon status: missing; path={self.path}"
        pid = payload.get("pid")
        event = payload.get("event") or payload.get("state") or "unknown"
        mode = payload.get("mode") or payload.get("default_mode") or "unknown"
        clipboard_only = payload.get("clipboard_only")
        updated_at = payload.get("updated_at") or "unknown"
        return (
            f"Daemon status: event={event}; pid={pid}; mode={mode}; "
            f"clipboard_only={clipboard_only}; updated_at={updated_at}; path={self.path}"
        )


def pid_from_payload(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


class DaemonLock:
    def __init__(self, cfg: dict[str, Any]) -> None:
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("daemon_lock_path") or "logs/daemon.lock.json")
        if self.path is None:
            raise RuntimeError("history.daemon_lock_path is invalid")

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return read_json(self.path)

    def acquire(self, **fields: Any) -> None:
        existing = None
        if self.path.exists():
            try:
                existing = self.load()
            except Exception:
                existing = None
        existing_pid = pid_from_payload(existing)
        if existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid):
            raise RuntimeError(
                f"daemon already running: pid={existing_pid}; lock={self.path}. "
                "Use --daemon-status or the control panel to inspect it."
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **fields,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def release(self) -> None:
        try:
            payload = self.load()
        except Exception:
            payload = None
        if pid_from_payload(payload) == os.getpid():
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                pass

    def summary(self) -> str:
        try:
            payload = self.load()
        except Exception as e:
            return f"Daemon lock: unreadable ({e}); path={self.path}"
        if not payload:
            return f"Daemon lock: missing; path={self.path}"
        pid = pid_from_payload(payload)
        active = is_pid_running(pid)
        mode = payload.get("mode") or payload.get("default_mode") or "unknown"
        acquired_at = payload.get("acquired_at") or "unknown"
        state = "active" if active else "stale"
        return f"Daemon lock: {state}; pid={pid}; mode={mode}; acquired_at={acquired_at}; path={self.path}"


class DaemonControlStore:
    def __init__(self, cfg: dict[str, Any]) -> None:
        history_cfg = cfg.get("history", {}) if isinstance(cfg.get("history"), dict) else {}
        self.path = resolve_path(history_cfg.get("daemon_control_path") or "logs/daemon-control.json")
        if self.path is None:
            raise RuntimeError("history.daemon_control_path is invalid")

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return read_json(self.path)

    def request_stop(self, *, source: str, reason: str = "") -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "command": "stop",
            "request_id": uuid.uuid4().hex,
            "source": source,
            "reason": reason,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload

    def summary(self) -> str:
        try:
            payload = self.load()
        except Exception as e:
            return f"Daemon control: unreadable ({e}); path={self.path}"
        if not payload:
            return f"Daemon control: missing; path={self.path}"
        command = payload.get("command") or "unknown"
        request_id = payload.get("request_id") or "unknown"
        requested_at = payload.get("requested_at") or "unknown"
        source = payload.get("source") or "unknown"
        return (
            f"Daemon control: command={command}; request_id={request_id}; "
            f"source={source}; requested_at={requested_at}; path={self.path}"
        )


def daily_report_paths(output_prefix: Path | None = None) -> tuple[Path, Path]:
    prefix = output_prefix or ROOT.parent / "benchmarks" / "results" / "daily-trial-report"
    if prefix.suffix:
        prefix = prefix.with_suffix("")
    return prefix.with_suffix(".json"), prefix.with_suffix(".md")


def generate_daily_trial_report(cfg: dict[str, Any], output_prefix: Path | None = None) -> tuple[Path, Path, str]:
    script = ROOT.parent / "benchmarks" / "scripts" / "summarize_daily_trial.py"
    if not script.exists():
        raise RuntimeError(f"daily trial report script not found: {script}")
    json_path, md_path = daily_report_paths(output_prefix)
    cmd = [
        sys.executable,
        str(script),
        "--usage-input",
        str(UsageStore(cfg).path),
        "--bad-feedback-input",
        str(FeedbackStore(cfg).path),
        "--output-prefix",
        str(output_prefix or (ROOT.parent / "benchmarks" / "results" / "daily-trial-report")),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    output = "\n".join(part.strip() for part in [proc.stdout, proc.stderr] if part.strip())
    if proc.returncode != 0:
        raise RuntimeError(f"daily trial report failed with exit={proc.returncode}: {output}")
    return json_path, md_path, output


def daily_report_status_line(output_prefix: Path | None = None) -> str:
    json_path, md_path = daily_report_paths(output_prefix)
    if not json_path.exists():
        return f"Daily report: missing; markdown={md_path}"
    try:
        payload = read_json(json_path)
    except Exception as e:
        return f"Daily report: unreadable ({e}); markdown={md_path}"

    status = str(payload.get("status") or "unknown")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    bad_feedback = payload.get("bad_feedback") if isinstance(payload.get("bad_feedback"), dict) else {}
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    first_recommendation = ""
    for item in recommendations:
        text = str(item or "").strip()
        if text:
            first_recommendation = text.lstrip("- ").strip()
            break
    if len(first_recommendation) > 110:
        first_recommendation = first_recommendation[:107].rstrip() + "..."
    runs = usage.get("runs", 0)
    bad_runs = bad_feedback.get("runs", 0)
    bad_rate = payload.get("bad_rate")
    try:
        bad_rate_text = "n/a" if bad_rate is None else f"{float(bad_rate):.1%}"
    except (TypeError, ValueError):
        bad_rate_text = "n/a"
    line = f"Daily report: {status}; runs={runs}; bad={bad_runs}; bad_rate={bad_rate_text}; markdown={md_path}"
    if first_recommendation:
        line += f"\nNext: {first_recommendation}"
    return line


def audio_duration_seconds(path: Path) -> float | None:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return frames / rate if rate else None
        except Exception:
            return None
    ffprobe = None
    try:
        from shutil import which

        ffprobe = which("ffprobe")
    except Exception:
        ffprobe = None
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        return float(proc.stdout.strip())
    except Exception:
        return None


def foreground_window_snapshot() -> WindowSnapshot | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow())
        if not hwnd:
            return None
        pid_value = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
        pid = int(pid_value.value) if pid_value.value else None
        return WindowSnapshot(hwnd=hwnd, pid=pid)
    except Exception:
        return None


def foreground_matches(target: WindowSnapshot | None) -> bool:
    if target is None:
        return True
    current = foreground_window_snapshot()
    if current is None:
        return False
    return current.hwnd == target.hwnd


class TextPaster:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def paste(
        self,
        text: str,
        no_paste: bool = False,
        clipboard_only: bool = False,
        target_window: WindowSnapshot | None = None,
    ) -> PasteResult:
        paste_cfg = self.cfg["paste"]
        if no_paste or not paste_cfg.get("enabled", True):
            if paste_cfg.get("suppress_output_when_disabled", False):
                log("[paste] disabled; output suppressed")
            else:
                print("\n--- OUTPUT START ---")
                print(text)
                print("--- OUTPUT END ---\n")
            return PasteResult(method="none", copied=False, pasted=False)

        method = "clipboard" if clipboard_only else str(paste_cfg.get("method") or "hotkey")
        if method not in {"hotkey", "clipboard", "autohotkey"}:
            raise RuntimeError(f"unsupported paste method: {method}")

        import pyperclip

        if (
            method in {"hotkey", "autohotkey"}
            and target_window is not None
            and paste_cfg.get("focus_guard_enabled", True)
            and not foreground_matches(target_window)
        ):
            pyperclip.copy(text)
            reason = f"foreground changed from {target_window.summary()}"
            log(f"[paste] focus guard copied to clipboard instead of auto-paste; {reason}")
            return PasteResult(
                method="clipboard_guarded",
                copied=True,
                pasted=False,
                guarded=True,
                guard_reason="foreground_changed",
            )

        if method == "autohotkey":
            self._paste_with_autohotkey(text, paste_cfg)
            return PasteResult(method=method, copied=True, pasted=True)

        old_clipboard = None
        if method == "hotkey" and paste_cfg.get("restore_clipboard", True):
            try:
                old_clipboard = pyperclip.paste()
            except Exception:
                old_clipboard = None

        pyperclip.copy(text)
        if method == "clipboard":
            log("[paste] copied to clipboard")
            return PasteResult(method=method, copied=True, pasted=False)

        time.sleep(float(paste_cfg.get("paste_delay_seconds", 0.12)))
        import pyautogui

        pyautogui.hotkey("ctrl", "v")
        log("[paste] pasted via Ctrl+V")
        time.sleep(float(paste_cfg.get("restore_delay_seconds", 0.8)))

        if old_clipboard is not None:
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass
        return PasteResult(method=method, copied=True, pasted=True)

    def _paste_with_autohotkey(self, text: str, paste_cfg: dict[str, Any]) -> None:
        exe = self._autohotkey_executable(paste_cfg)
        script = resolve_path(str(paste_cfg.get("autohotkey_script") or "scripts/paste_text.ahk"))
        if script is None or not script.exists():
            raise RuntimeError(f"AutoHotkey paste script not found: {script}")

        timeout_seconds = float(paste_cfg.get("autohotkey_timeout_seconds", 5))
        paste_delay_ms = int(float(paste_cfg.get("paste_delay_seconds", 0.12)) * 1000)
        restore_delay_ms = int(float(paste_cfg.get("restore_delay_seconds", 0.8)) * 1000)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".txt",
                prefix="voice-input-paste-",
                delete=False,
            ) as f:
                f.write(text)
                tmp_path = Path(f.name)
            proc = subprocess.run(
                [str(exe), str(script), str(tmp_path), str(paste_delay_ms), str(restore_delay_ms)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"AutoHotkey paste timed out after {timeout_seconds}s") from e
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"AutoHotkey paste failed with exit={proc.returncode}: {details}")
        log("[paste] pasted via AutoHotkey")

    def _autohotkey_executable(self, paste_cfg: dict[str, Any]) -> Path:
        configured = str(paste_cfg.get("autohotkey_exe") or os.environ.get("AUTOHOTKEY_EXE") or "").strip()
        if configured:
            configured_path = Path(configured)
            candidates = [configured_path]
            if not configured_path.is_absolute():
                resolved = resolve_path(configured)
                if resolved is not None:
                    candidates.append(resolved)
            for path in candidates:
                if path.exists():
                    return path
            found = shutil.which(configured)
            if found:
                return Path(found)
            raise RuntimeError(f"AutoHotkey executable not found: {configured}")

        candidates: list[str] = [
            "AutoHotkey64.exe",
            "AutoHotkey.exe",
            str(Path(os.environ.get("ProgramFiles", "")) / "AutoHotkey" / "v2" / "AutoHotkey64.exe"),
            str(Path(os.environ.get("ProgramFiles", "")) / "AutoHotkey" / "AutoHotkey.exe"),
            str(Path(os.environ.get("ProgramFiles(x86)", "")) / "AutoHotkey" / "AutoHotkey.exe"),
        ]
        for candidate in candidates:
            found = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
            if found and Path(found).exists():
                return Path(found)
        raise RuntimeError(
            "AutoHotkey executable not found. Install AutoHotkey v2 or set paste.autohotkey_exe / AUTOHOTKEY_EXE."
        )


class StatusOverlay:
    """Small Typeless-like always-on-top status capsule.

    Runs Tk in a background thread and communicates through a queue so the
    speech pipeline never blocks on UI failures. Recording state exposes
    Typeless-like click targets: cancel on the left, accept on the right.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.ui_cfg = cfg.get("ui", {}).get("overlay", {})
        self.enabled = bool(self.ui_cfg.get("enabled", True))
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failed = False
        self._actions: dict[str, Any] = {}

    def set_actions(
        self,
        *,
        on_cancel: Any | None = None,
        on_accept: Any | None = None,
        on_undo_cancel: Any | None = None,
    ) -> None:
        self._actions = {
            "cancel": on_cancel,
            "accept": on_accept,
            "undo_cancel": on_undo_cancel,
        }

    def start(self) -> None:
        if not self.enabled or self._thread is not None or self._failed:
            return
        self._thread = threading.Thread(target=self._run, name="status-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def show_recording(self) -> None:
        self._send("recording")

    def show_processing(self, label: str = "Thinking", eta_seconds: float | None = None) -> None:
        self._send("processing", label="Thinking", eta_seconds=eta_seconds)

    def show_done(self, label: str = "Done", duration_seconds: float | None = None) -> None:
        self._send("done", label=label, duration_seconds=duration_seconds)

    def show_error(self, label: str = "Error") -> None:
        self._send("error", label=label)

    def show_cancelled(self, duration_seconds: float | None = None) -> None:
        self._send("cancelled", duration_seconds=duration_seconds)

    def hide(self) -> None:
        self._send("hide")

    def close(self) -> None:
        self._send("close")

    def _send(self, command: str, **payload: Any) -> None:
        if not self.enabled or self._failed:
            return
        if self._thread is None:
            self.start()
        try:
            self._queue.put_nowait((command, payload))
        except Exception:
            pass

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception as e:
            self._failed = True
            log(f"[ui] overlay unavailable: {e}")
            self._ready.set()
            return

        processing_width = int(self.ui_cfg.get("processing_width", 142))
        recording_width = int(self.ui_cfg.get("recording_width", 176))
        cancelled_width = int(self.ui_cfg.get("cancelled_width", 260))
        width = max(recording_width, processing_width, cancelled_width)
        height = int(self.ui_cfg.get("height", 64))
        bottom_margin = int(self.ui_cfg.get("bottom_margin", 72))
        default_eta = float(self.ui_cfg.get("processing_eta_seconds", 12))
        transparent_color = str(self.ui_cfg.get("transparent_color", "#ff00ff"))
        show_eta_text = bool(self.ui_cfg.get("processing_show_eta_text", False))
        show_progress_fill = bool(self.ui_cfg.get("processing_show_progress_fill", False))

        try:
            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.attributes("-alpha", float(self.ui_cfg.get("opacity", 0.96)))
            try:
                root.attributes("-transparentcolor", transparent_color)
            except Exception:
                pass
            canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bg=transparent_color)
            canvas.pack()

            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            x = max(0, (screen_w - width) // 2)
            y = max(0, screen_h - height - bottom_margin)
            root.geometry(f"{width}x{height}+{x}+{y}")

            state: dict[str, Any] = {
                "mode": "hidden",
                "label": "",
                "started_at": time.time(),
                "eta_seconds": default_eta,
                "frame": 0,
                "hide_at": 0.0,
            }
            click_zones: dict[str, tuple[float, float, float, float]] = {}

            def capsule_shape(
                left: float,
                top: float,
                right: float,
                bottom: float,
                fill: str,
                outline: str | None = None,
            ) -> None:
                radius = max(1.0, (bottom - top) / 2)
                outline = outline or fill
                canvas.create_oval(left, top, left + radius * 2, bottom, fill=fill, outline=outline)
                canvas.create_oval(right - radius * 2, top, right, bottom, fill=fill, outline=outline)
                canvas.create_rectangle(left + radius, top, right - radius, bottom, fill=fill, outline=outline)

            def rounded_rect(
                left: float,
                top: float,
                right: float,
                bottom: float,
                radius: float,
                fill: str,
                outline: str | None = None,
            ) -> None:
                outline = outline or fill
                canvas.create_rectangle(left + radius, top, right - radius, bottom, fill=fill, outline=outline)
                canvas.create_rectangle(left, top + radius, right, bottom - radius, fill=fill, outline=outline)
                canvas.create_oval(left, top, left + radius * 2, top + radius * 2, fill=fill, outline=outline)
                canvas.create_oval(right - radius * 2, top, right, top + radius * 2, fill=fill, outline=outline)
                canvas.create_oval(left, bottom - radius * 2, left + radius * 2, bottom, fill=fill, outline=outline)
                canvas.create_oval(right - radius * 2, bottom - radius * 2, right, bottom, fill=fill, outline=outline)

            def capsule_bounds(capsule_w: int, capsule_h: int = 52) -> tuple[float, float, float, float]:
                left = (width - capsule_w) / 2
                top = (height - capsule_h) / 2
                return left, top, left + capsule_w, top + capsule_h

            def rounded_capsule(
                capsule_w: int,
                fill: str,
                outline: str = "#242222",
                capsule_h: int = 52,
            ) -> tuple[float, float, float, float]:
                left, top, right, bottom = capsule_bounds(capsule_w, capsule_h)
                capsule_shape(left + 1, top + 2, right + 1, bottom + 2, "#353535", "#353535")
                capsule_shape(left, top, right, bottom, fill, outline)
                return left, top, right, bottom

            def draw_recording() -> None:
                left, top, right, bottom = rounded_capsule(recording_width, "#000000", "#000000", 52)
                center_y = (top + bottom) / 2
                button_d = 38
                button_y0 = center_y - button_d / 2
                left_button = (left + 6, button_y0, left + 6 + button_d, button_y0 + button_d)
                right_button = (right - 6 - button_d, button_y0, right - 6, button_y0 + button_d)
                click_zones["cancel"] = left_button
                click_zones["accept"] = right_button
                canvas.create_oval(*left_button, fill="#404040", outline="#404040")
                canvas.create_oval(*right_button, fill="#fbfbfb", outline="#fbfbfb")
                lx = (left_button[0] + left_button[2]) / 2
                rx = (right_button[0] + right_button[2]) / 2
                icon_r = 9
                canvas.create_line(lx - icon_r, center_y - icon_r, lx + icon_r, center_y + icon_r, fill="#f5f5f5", width=3, capstyle=tk.ROUND)
                canvas.create_line(lx + icon_r, center_y - icon_r, lx - icon_r, center_y + icon_r, fill="#f5f5f5", width=3, capstyle=tk.ROUND)
                canvas.create_line(rx - 11, center_y, rx - 3, center_y + 8, fill="#111111", width=3, capstyle=tk.ROUND)
                canvas.create_line(rx - 3, center_y + 8, rx + 13, center_y - 10, fill="#111111", width=3, capstyle=tk.ROUND)

                dot_count = 12
                start_x = left + 58
                end_x = right - 58
                gap = (end_x - start_x) / max(1, dot_count - 1)
                frame = int(state["frame"])
                for i in range(dot_count):
                    phase = frame / 5.0 + i * 0.55
                    brightness = int(112 + 76 * (0.5 + 0.5 * math.sin(phase)))
                    color = f"#{brightness:02x}{brightness:02x}{brightness:02x}"
                    x = start_x + i * gap
                    canvas.create_oval(x - 1.5, center_y - 1.5, x + 1.5, center_y + 1.5, fill=color, outline=color)

            def draw_processing(fill: str, text_fill: str) -> None:
                left, top, right, bottom = rounded_capsule(processing_width, fill, fill, 52)
                elapsed = max(0, time.time() - float(state.get("started_at") or time.time()))
                eta = float(state.get("eta_seconds") or default_eta)
                if show_progress_fill and state.get("mode") == "processing" and eta > 0:
                    progress = max(0.12, min(0.96, elapsed / eta))
                    progress_right = left + (right - left) * progress
                    radius = (bottom - top) / 2
                    progress_fill = "#3f3f3f"
                    canvas.create_oval(left, top, left + radius * 2, bottom, fill=progress_fill, outline=progress_fill)
                    if progress_right > left + radius:
                        canvas.create_rectangle(
                            left + radius,
                            top,
                            progress_right,
                            bottom,
                            fill=progress_fill,
                            outline=progress_fill,
                        )
                if state.get("mode") == "processing":
                    text = "Thinking" if not show_eta_text else f"Thinking ~{max(0, math.ceil(eta - elapsed))}s"
                else:
                    text = str(state.get("label") or "Thinking")
                canvas.create_text(
                    width / 2,
                    height / 2,
                    text=text,
                    fill=text_fill,
                    font=("Segoe UI", 15, "bold"),
                )

            def draw_cancelled() -> None:
                toast_w = cancelled_width
                toast_h = 58
                left = (width - toast_w) / 2
                top = (height - toast_h) / 2
                right = left + toast_w
                bottom = top + toast_h
                rounded_rect(left + 4, top + 4, right + 4, bottom + 4, 14, "#333333", "#333333")
                rounded_rect(left, top, right, bottom, 14, "#191718", "#191718")
                info_x = left + 28
                info_y = (top + bottom) / 2
                canvas.create_oval(info_x - 9, info_y - 9, info_x + 9, info_y + 9, outline="#7287f4", width=2)
                canvas.create_text(info_x, info_y + 1, text="i", fill="#7287f4", font=("Segoe UI", 12, "bold"))
                canvas.create_text(left + 56, info_y, text="转录已取消", fill="#ffffff", font=("Segoe UI", 15, "bold"), anchor="w")
                btn_w = 72
                btn_h = 40
                btn_left = right - btn_w - 10
                btn_top = info_y - btn_h / 2
                btn_right = btn_left + btn_w
                btn_bottom = btn_top + btn_h
                click_zones["undo_cancel"] = (btn_left, btn_top, btn_right, btn_bottom)
                capsule_shape(btn_left, btn_top, btn_right, btn_bottom, "#404040", "#404040")
                canvas.create_text((btn_left + btn_right) / 2, info_y, text="撤销", fill="#ffffff", font=("Segoe UI", 14, "bold"))

            def run_action(name: str) -> None:
                callback = self._actions.get(name)
                if not callback:
                    return
                threading.Thread(target=callback, name=f"overlay-{name}", daemon=True).start()

            def on_click(event: Any) -> None:
                if state.get("mode") == "recording":
                    for name in ("cancel", "accept"):
                        zone = click_zones.get(name)
                        if zone and zone[0] <= event.x <= zone[2] and zone[1] <= event.y <= zone[3]:
                            run_action(name)
                            return
                if state.get("mode") == "cancelled":
                    zone = click_zones.get("undo_cancel")
                    if zone and zone[0] <= event.x <= zone[2] and zone[1] <= event.y <= zone[3]:
                        run_action("undo_cancel")

            canvas.bind("<Button-1>", on_click)

            def pump() -> None:
                now = time.time()
                while True:
                    try:
                        command, payload = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if command == "recording":
                        state.update(mode="recording", started_at=now, frame=0, hide_at=0.0)
                    elif command == "processing":
                        state.update(
                            mode="processing",
                            label="Thinking",
                            eta_seconds=payload.get("eta_seconds") or default_eta,
                            started_at=now,
                            hide_at=0.0,
                        )
                    elif command == "done":
                        duration = float(payload.get("duration_seconds") or 0.25)
                        state.update(mode="processing", label="Thinking", hide_at=now + duration)
                    elif command == "error":
                        state.update(mode="error", label=payload.get("label", "Error"), hide_at=now + 1.5)
                    elif command == "cancelled":
                        duration = float(payload.get("duration_seconds") or 6.0)
                        state.update(mode="cancelled", label="转录已取消", hide_at=now + duration)
                    elif command == "hide":
                        state.update(mode="hidden", hide_at=0.0)
                    elif command == "close":
                        root.destroy()
                        return

                if state["hide_at"] and now >= float(state["hide_at"]):
                    state.update(mode="hidden", hide_at=0.0)

                canvas.delete("all")
                click_zones.clear()
                mode = state["mode"]
                if mode == "hidden":
                    root.withdraw()
                else:
                    root.deiconify()
                    if mode == "recording":
                        draw_recording()
                    elif mode == "processing":
                        draw_processing("#3b3b3b", "#9c9c9c")
                    elif mode == "done":
                        draw_processing("#3b3b3b", "#9c9c9c")
                    elif mode == "error":
                        draw_processing("#2a0606", "#ffb8b8")
                    elif mode == "cancelled":
                        draw_cancelled()
                state["frame"] = int(state["frame"]) + 1
                root.after(80, pump)

            self._ready.set()
            pump()
            root.mainloop()
        except Exception as e:
            self._failed = True
            self._ready.set()
            log(f"[ui] overlay failed: {e}")


class VoicePipeline:
    def __init__(
        self,
        cfg: dict[str, Any],
        no_paste: bool = False,
        clipboard_only: bool = False,
        status_overlay: StatusOverlay | None = None,
    ) -> None:
        self.cfg = cfg
        self.no_paste = no_paste
        self.clipboard_only = clipboard_only
        self.status_overlay = status_overlay
        self.transcriber = TranscriberRouter(cfg)
        self.enhancer = TextEnhancer(cfg)
        self.paster = TextPaster(cfg)
        self.result_store = ResultStore(cfg)
        self.usage_store = UsageStore(cfg)

    def process_audio_file(
        self,
        mode: str,
        audio_path: Path,
        target_window: WindowSnapshot | None = None,
    ) -> str:
        total_started_at = time.time()
        audio_seconds = audio_duration_seconds(audio_path)
        if self.status_overlay:
            self.status_overlay.show_processing("Thinking", eta_seconds=6)
        stt_started_at = time.time()
        transcript = self.transcriber.transcribe(audio_path)
        stt_elapsed_ms = (time.time() - stt_started_at) * 1000
        return self.process_text(
            mode,
            transcript,
            audio_seconds=audio_seconds,
            stt_elapsed_ms=stt_elapsed_ms,
            total_started_at=total_started_at,
            target_window=target_window,
        )

    def process_text(
        self,
        mode: str,
        transcript: str,
        *,
        audio_seconds: float | None = None,
        stt_elapsed_ms: float | None = None,
        total_started_at: float | None = None,
        target_window: WindowSnapshot | None = None,
    ) -> str:
        if mode not in self.cfg["modes"]:
            raise RuntimeError(f"unknown mode: {mode}")

        if not transcript.strip():
            raise RuntimeError("empty transcript")

        text = transcript
        rewrite_degraded = False
        rewrite_elapsed_ms: float | None = None
        if self.cfg["modes"][mode].get("enhance", True):
            try:
                if self.status_overlay:
                    self.status_overlay.show_processing("Thinking", eta_seconds=12)
                rewrite_started_at = time.time()
                text = self.enhancer.enhance(mode, transcript)
                rewrite_elapsed_ms = (time.time() - rewrite_started_at) * 1000
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                rewrite_degraded = True
                log(f"[llm] unavailable, using raw transcript: {e}")
            except Exception as e:
                rewrite_degraded = True
                log(f"[llm] failed, using raw transcript: {e}")

        if self.status_overlay:
            self.status_overlay.show_processing("Thinking", eta_seconds=2)
        paste_started_at = time.time()
        self.result_store.save(
            text=text,
            raw_transcript=transcript,
            mode=mode,
            rewrite_degraded=rewrite_degraded,
        )
        paste_result = self.paster.paste(
            text,
            no_paste=self.no_paste,
            clipboard_only=self.clipboard_only,
            target_window=target_window,
        )
        paste_elapsed_ms = (time.time() - paste_started_at) * 1000
        if audio_seconds is not None or stt_elapsed_ms is not None:
            self.usage_store.append(
                {
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "mode": mode,
                    "audio_seconds": round(audio_seconds, 3) if audio_seconds is not None else None,
                    "stt_engine": self.cfg.get("stt", {}).get("engine"),
                    "stt_model": self.cfg.get("stt", {}).get("model"),
                    "llm_provider": self.cfg.get("llm", {}).get("provider"),
                    "llm_model": self.enhancer.last_model or self.cfg.get("llm", {}).get("model"),
                    "llm_usage": self.enhancer.last_usage,
                    "stt_elapsed_ms": round(stt_elapsed_ms, 1) if stt_elapsed_ms is not None else None,
                    "rewrite_elapsed_ms": round(rewrite_elapsed_ms, 1) if rewrite_elapsed_ms is not None else None,
                    "paste_elapsed_ms": round(paste_elapsed_ms, 1),
                    "total_elapsed_ms": round((time.time() - total_started_at) * 1000, 1)
                    if total_started_at is not None
                    else None,
                    "raw_chars": len(transcript),
                    "chars": len(text),
                    "rewrite_degraded": bool(rewrite_degraded),
                    "paste_method": paste_result.method,
                    "copied": paste_result.copied,
                    "pasted": paste_result.pasted,
                    "paste_guarded": paste_result.guarded,
                    "paste_guard_reason": paste_result.guard_reason,
                }
            )
        if self.status_overlay:
            if rewrite_degraded:
                self.status_overlay.show_done("Raw ASR", duration_seconds=2.5)
            else:
                self.status_overlay.show_done("Copied" if self.clipboard_only or paste_result.guarded else "Done")
        return text


def list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())


def dry_run(cfg: dict[str, Any]) -> None:
    print("Project root:", ROOT)
    print("Hotkeys:", cfg["hotkeys"])
    print("STT:", redact_secrets(cfg["stt"]))
    print("LLM:", redact_secrets(cfg["llm"]))
    print("Paste:", cfg.get("paste", {}))
    print("UI:", cfg.get("ui", {}))
    history_path = ResultStore(cfg).path
    usage_path = UsageStore(cfg).path
    status_path = RuntimeStatusStore(cfg).path
    lock_path = DaemonLock(cfg).path
    control_path = DaemonControlStore(cfg).path
    print("History:", history_path, "exists=", history_path.exists())
    print("Usage log:", usage_path, "exists=", usage_path.exists())
    print("Daemon status:", status_path, "exists=", status_path.exists())
    print("Daemon lock:", lock_path, "exists=", lock_path.exists())
    print("Daemon control:", control_path, "exists=", control_path.exists())
    print("Modes:")
    for name, mode in cfg["modes"].items():
        prompt = resolve_path(mode["prompt_path"])
        print(f"  - {name}: {mode['name']} | prompt={prompt} | exists={prompt.exists() if prompt else False}")
    print("Dictionary:", dictionary_summary(cfg))


def api_smoke(cfg: dict[str, Any]) -> None:
    """Check provider reachability without transcription/chat inference calls."""

    print("== API smoke ==")
    stt = cfg["stt"]
    if str(stt.get("engine") or "") == "siliconflow":
        model = str(stt.get("model") or "")
        base_url = str(stt.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
        payload = get_json(
            f"{base_url}/models?type=audio&sub_type=speech-to-text",
            SiliconFlowTranscriber(cfg)._api_key(),
            20,
        )
        ids = [str(item.get("id") or "") for item in payload.get("data") or [] if isinstance(item, dict)]
        listed: bool | str = model in ids if ids else "unknown"
        print(f"OK  STT provider reachable: {base_url}; model={model}; listed={listed}")
    else:
        print(f"SKIP STT API smoke for engine={stt.get('engine')}")

    llm = cfg["llm"]
    if not llm.get("enabled", True):
        print("SKIP LLM API smoke; llm.enabled=false")
    elif str(llm.get("provider") or "") == "openai-compatible":
        model = str(llm.get("model") or "")
        base_url = str(llm.get("base_url") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("llm.base_url missing")
        payload = get_json(
            f"{base_url}/models?type=text&sub_type=chat",
            TextEnhancer(cfg)._api_key(llm),
            20,
        )
        ids = [str(item.get("id") or "") for item in payload.get("data") or [] if isinstance(item, dict)]
        listed = model in ids if ids else "unknown"
        print(f"OK  LLM provider reachable: {base_url}; model={model}; listed={listed}")
    elif str(llm.get("provider") or "") == "ollama":
        model = str(llm.get("model") or "")
        base_url = str(llm.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        payload = get_json(f"{base_url}/api/tags", "", 10)
        names = [str(item.get("name") or "") for item in payload.get("models") or [] if isinstance(item, dict)]
        listed = model in names or f"{model}:latest" in names
        print(f"OK  Ollama reachable: {base_url}; model={model}; listed={listed}")
    else:
        print(f"SKIP LLM API smoke for provider={llm.get('provider')}")


def request_daemon_stop(cfg: dict[str, Any], *, source: str, wait_seconds: float = 5.0) -> bool:
    lock = DaemonLock(cfg)
    control = DaemonControlStore(cfg)
    try:
        payload = lock.load()
    except Exception as e:
        log(f"[daemon] lock unreadable: {e}")
        payload = None
    pid = pid_from_payload(payload)
    if not pid or not is_pid_running(pid):
        log(f"[daemon] no active daemon lock; {lock.summary()}")
        return True

    request = control.request_stop(source=source, reason=f"stop pid={pid}")
    log(f"[daemon] stop requested: pid={pid}; request_id={request['request_id']}; control={control.path}")
    if wait_seconds <= 0:
        return True
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            latest = lock.load()
        except Exception:
            latest = None
        latest_pid = pid_from_payload(latest)
        if not latest_pid or latest_pid != pid or not is_pid_running(latest_pid):
            log("[daemon] stopped")
            return True
        time.sleep(0.2)
    log(f"[daemon] stop request timed out after {wait_seconds:.1f}s; {lock.summary()}")
    return False


def parse_win32_hotkey(hotkey: str) -> tuple[int, int]:
    modifier_bits = {
        "alt": 0x0001,
        "ctrl": 0x0002,
        "control": 0x0002,
        "shift": 0x0004,
        "win": 0x0008,
        "windows": 0x0008,
        "cmd": 0x0008,
        "command": 0x0008,
    }
    key_codes = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "return": 0x0D,
        "esc": 0x1B,
        "escape": 0x1B,
        "space": 0x20,
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "insert": 0x2D,
        "delete": 0x2E,
    }
    for idx in range(1, 25):
        key_codes[f"f{idx}"] = 0x70 + idx - 1

    tokens = [part.strip().lower() for part in str(hotkey or "").split("+") if part.strip()]
    if not tokens:
        raise ValueError("empty hotkey")

    modifiers = 0x4000  # MOD_NOREPEAT: avoid repeated toggles while the keys are held.
    key_code: int | None = None
    for token in tokens:
        if token in modifier_bits:
            modifiers |= modifier_bits[token]
            continue
        if key_code is not None:
            raise ValueError(f"hotkey has multiple non-modifier keys: {hotkey}")
        if len(token) == 1 and "a" <= token <= "z":
            key_code = ord(token.upper())
        elif len(token) == 1 and "0" <= token <= "9":
            key_code = ord(token)
        elif token in key_codes:
            key_code = key_codes[token]
        else:
            raise ValueError(f"unsupported hotkey key: {token!r} in {hotkey!r}")
    if key_code is None:
        raise ValueError(f"hotkey missing key: {hotkey}")
    return modifiers, key_code


class Win32HotkeyRuntime:
    """Register Windows global hotkeys without leaking the trigger key to apps."""

    def __init__(self, bindings: list[tuple[str, str, Any]]) -> None:
        if os.name != "nt":
            raise RuntimeError("win32 hotkey backend is only available on Windows")

        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._callbacks: dict[int, tuple[str, Any]] = {}
        self._registered_ids: list[int] = []
        self._next_id = 7100
        self._wm_hotkey = 0x0312
        self._pm_remove = 0x0001

        self._user32.RegisterHotKey.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL

        # Ensure this thread has a message queue before RegisterHotKey posts to it.
        msg = wintypes.MSG()
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        try:
            for label, hotkey, callback in bindings:
                self.register(label, hotkey, callback)
        except Exception:
            self.close()
            raise

    def register(self, label: str, hotkey: str, callback: Any) -> None:
        modifiers, key_code = parse_win32_hotkey(hotkey)
        hotkey_id = self._next_id
        self._next_id += 1
        if not self._user32.RegisterHotKey(None, hotkey_id, modifiers, key_code):
            error = self._ctypes.get_last_error()
            raise RuntimeError(
                f"RegisterHotKey failed for {label}={hotkey!r}; "
                f"Windows error={error}. The hotkey may already be used by another app."
            )
        self._callbacks[hotkey_id] = (label, callback)
        self._registered_ids.append(hotkey_id)

    def run_until_stopped(self, stop_event: threading.Event) -> None:
        msg = self._wintypes.MSG()
        while not stop_event.is_set():
            while self._user32.PeekMessageW(self._ctypes.byref(msg), None, 0, 0, self._pm_remove):
                if msg.message != self._wm_hotkey:
                    self._user32.TranslateMessage(self._ctypes.byref(msg))
                    self._user32.DispatchMessageW(self._ctypes.byref(msg))
                    continue
                callback_item = self._callbacks.get(int(msg.wParam))
                if not callback_item:
                    continue
                label, callback = callback_item
                try:
                    callback()
                except Exception as e:
                    log(f"[hotkey] callback failed: {label}: {e}")
            stop_event.wait(0.05)

    def close(self) -> None:
        for hotkey_id in reversed(self._registered_ids):
            try:
                self._user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
        self._registered_ids.clear()
        self._callbacks.clear()


def run_daemon(
    cfg: dict[str, Any],
    no_paste: bool = False,
    clipboard_only: bool = False,
    no_overlay: bool = False,
) -> None:
    default_mode = cfg.get("default_mode", "default")
    if default_mode not in cfg.get("modes", {}):
        default_mode = "long" if "long" in cfg.get("modes", {}) else next(iter(cfg["modes"]))
    daemon_lock = DaemonLock(cfg)
    daemon_lock.acquire(
        default_mode=default_mode,
        mode=default_mode,
        clipboard_only=bool(clipboard_only),
        no_paste=bool(no_paste),
        overlay=not no_overlay,
        paste_method="clipboard" if clipboard_only else cfg.get("paste", {}).get("method"),
        focus_guard_enabled=cfg.get("paste", {}).get("focus_guard_enabled", True),
    )

    try:
        recorder = AudioRecorder(cfg)
        overlay = None if no_overlay else StatusOverlay(cfg)
        if overlay:
            overlay.start()
        pipeline = VoicePipeline(
            cfg,
            no_paste=no_paste,
            clipboard_only=clipboard_only,
            status_overlay=overlay,
        )
    except Exception:
        daemon_lock.release()
        raise
    state_lock = threading.Lock()
    work_queue: queue.Queue[tuple[str, Path, WindowSnapshot | None]] = queue.Queue()
    current_mode: dict[str, Any] = {"mode": None, "target_window": None}
    status_store = RuntimeStatusStore(cfg)
    control_store = DaemonControlStore(cfg)
    control_store.clear()
    stop_event = threading.Event()
    status_store.write(
        event="started",
        pid=os.getpid(),
        default_mode=default_mode,
        clipboard_only=bool(clipboard_only),
        no_paste=bool(no_paste),
        overlay=not no_overlay,
        paste_method="clipboard" if clipboard_only else cfg.get("paste", {}).get("method"),
    )

    def control_watcher() -> None:
        while not stop_event.wait(0.5):
            try:
                payload = control_store.load()
            except Exception as e:
                status_store.write(event="control_error", pid=os.getpid(), last_error=str(e)[:240])
                continue
            if not payload:
                continue
            if payload.get("command") == "stop":
                status_store.write(
                    event="stop_requested",
                    pid=os.getpid(),
                    request_id=payload.get("request_id"),
                    source=payload.get("source"),
                )
                control_store.clear()
                stop_event.set()
                return

    def worker() -> None:
        while True:
            mode, path, target_window = work_queue.get()
            try:
                log(f"[pipeline] processing mode={mode}; file={path}")
                status_store.write(event="processing", pid=os.getpid(), mode=mode, queue_size=work_queue.qsize())
                pipeline.process_audio_file(mode, path, target_window=target_window)
                status_store.write(event="idle", pid=os.getpid(), mode=mode, last_result="ok", queue_size=work_queue.qsize())
                beep("done")
            except Exception as e:
                beep("error")
                if overlay:
                    label = "No speech" if "empty transcript" in str(e).lower() else "Error"
                    overlay.show_error(label)
                status_store.write(
                    event="error",
                    pid=os.getpid(),
                    mode=mode,
                    last_error=str(e)[:240],
                    queue_size=work_queue.qsize(),
                )
                log(f"[pipeline] failed: {e}")
            finally:
                work_queue.task_done()

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=control_watcher, daemon=True).start()

    def start_recording(mode: str) -> None:
        with state_lock:
            if recorder.is_recording:
                return
            target_window = foreground_window_snapshot()
            recorder.start()
            current_mode["mode"] = mode
            current_mode["target_window"] = target_window
            current_mode["cancelled_item"] = None
        beep("start")
        if overlay:
            overlay.show_recording()
        status_store.write(
            event="recording",
            pid=os.getpid(),
            mode=mode,
            target_window=target_window.summary() if target_window else None,
        )
        log(f"[record] START mode={mode}. Press the same hotkey again, or click the check capsule, to stop.")

    def stop_current_recording(*, process: bool, source: str) -> None:
        with state_lock:
            if not recorder.is_recording:
                return
            active_mode = str(current_mode.get("mode") or default_mode)
            target_window = current_mode.get("target_window")
            if not isinstance(target_window, WindowSnapshot):
                target_window = None
            audio = recorder.stop_to_wav()
            current_mode["mode"] = None
            current_mode["target_window"] = None
        beep("stop")
        if process:
            if overlay:
                overlay.show_processing("Thinking", eta_seconds=12)
            log(f"[record] STOP mode={active_mode}; seconds={audio.seconds:.1f}; source={source}")
            work_queue.put((active_mode, audio.path, target_window))
            status_store.write(
                event="queued",
                pid=os.getpid(),
                mode=active_mode,
                audio_seconds=round(audio.seconds, 3),
                queue_size=work_queue.qsize(),
                source=source,
            )
            return
        with state_lock:
            current_mode["cancelled_item"] = (active_mode, audio.path, target_window)
        if overlay:
            overlay.show_cancelled(duration_seconds=6.0)
        status_store.write(
            event="cancelled",
            pid=os.getpid(),
            mode=active_mode,
            audio_seconds=round(audio.seconds, 3),
            source=source,
        )
        log(f"[record] CANCEL mode={active_mode}; seconds={audio.seconds:.1f}; source={source}")

    def undo_cancelled_recording() -> None:
        with state_lock:
            item = current_mode.get("cancelled_item")
            current_mode["cancelled_item"] = None
        if not item:
            return
        mode, path, target_window = item
        if not isinstance(path, Path):
            return
        if overlay:
            overlay.show_processing("Thinking", eta_seconds=12)
        work_queue.put((str(mode), path, target_window if isinstance(target_window, WindowSnapshot) else None))
        status_store.write(
            event="queued_after_cancel_undo",
            pid=os.getpid(),
            mode=str(mode),
            queue_size=work_queue.qsize(),
        )
        log(f"[record] UNDO CANCEL mode={mode}; file={path}")

    def toggle(mode: str) -> None:
        with state_lock:
            is_recording = recorder.is_recording
        if is_recording:
            stop_current_recording(process=True, source="hotkey")
        else:
            start_recording(mode)

    def copy_last() -> None:
        try:
            text = pipeline.result_store.load_text()
            pipeline.paster.paste(text, clipboard_only=True)
            beep("done")
            if overlay:
                overlay.show_done("Copied")
            status_store.write(event="copy_last", pid=os.getpid(), result="ok")
            log("[history] copied last result to clipboard")
        except Exception as e:
            beep("error")
            if overlay:
                overlay.show_error("No result")
            status_store.write(event="copy_last_error", pid=os.getpid(), last_error=str(e)[:240])
            log(f"[history] copy-last failed: {e}")

    if overlay:
        overlay.set_actions(
            on_cancel=lambda: stop_current_recording(process=False, source="overlay_cancel"),
            on_accept=lambda: stop_current_recording(process=True, source="overlay_accept"),
            on_undo_cancel=undo_cancelled_recording,
        )

    hk = cfg["hotkeys"]
    hotkey_backend = str(hk.get("backend") or ("win32" if os.name == "nt" else "keyboard")).lower()
    keyboard_module: Any | None = None
    win32_hotkeys: Win32HotkeyRuntime | None = None
    try:
        hotkey_bindings: list[tuple[str, str, Any]] = []
        if hk.get("toggle"):
            hotkey_bindings.append(("toggle", hk["toggle"], lambda: toggle(default_mode)))
        else:
            hotkey_bindings.append(("short", hk["short"], lambda: toggle("short")))
            hotkey_bindings.append(("long", hk["long"], lambda: toggle("long")))
        if hk.get("copy_last"):
            hotkey_bindings.append(("copy_last", hk["copy_last"], copy_last))
        hotkey_bindings.append(("quit", hk["quit"], lambda: stop_event.set()))

        if hotkey_backend == "win32":
            win32_hotkeys = Win32HotkeyRuntime(hotkey_bindings)
        elif hotkey_backend == "keyboard":
            import keyboard

            keyboard_module = keyboard
            for _, hotkey, callback in hotkey_bindings:
                keyboard_module.add_hotkey(hotkey, callback, suppress=True)
        else:
            raise RuntimeError(f"unsupported hotkeys.backend: {hotkey_backend}")
    except Exception as e:
        status_store.write(event="hotkey_error", pid=os.getpid(), last_error=str(e)[:240])
        daemon_lock.release()
        if overlay:
            overlay.close()
        raise
    log("Windows Typeless-like voice input is running.")
    log(f"  hotkey backend: {hotkey_backend}")
    if hk.get("toggle"):
        log(f"  toggle: {hk['toggle']} -> {default_mode}")
    else:
        log(f"  short: {hk['short']}")
        log(f"  long : {hk['long']}")
    if hk.get("copy_last"):
        log(f"  copy last: {hk['copy_last']}")
    log(f"  quit : {hk['quit']}")
    if clipboard_only:
        log("Mode: clipboard-only. Results are copied to clipboard; press Ctrl+V yourself.")
    else:
        log("Mode: auto-paste. Focus an input box before stopping recording so paste goes there.")
    try:
        if win32_hotkeys:
            win32_hotkeys.run_until_stopped(stop_event)
        else:
            stop_event.wait()
    finally:
        status_store.write(event="stopped", pid=os.getpid())
        daemon_lock.release()
        control_store.clear()
        if win32_hotkeys:
            win32_hotkeys.close()
        if keyboard_module:
            try:
                keyboard_module.unhook_all_hotkeys()
            except Exception:
                pass
        if overlay:
            overlay.close()
        log("Bye.")


def overlay_smoke(cfg: dict[str, Any]) -> None:
    overlay = StatusOverlay(cfg)
    overlay.start()
    log("[ui] overlay smoke: recording")
    overlay.show_recording()
    time.sleep(1.2)
    log("[ui] overlay smoke: thinking")
    overlay.show_processing("Thinking", eta_seconds=4)
    time.sleep(1.5)
    log("[ui] overlay smoke: cancelled")
    overlay.show_cancelled(duration_seconds=1.5)
    time.sleep(1.7)
    log("[ui] overlay smoke: done")
    overlay.show_done("Done")
    time.sleep(0.7)
    overlay.close()


def control_panel(cfg: dict[str, Any], smoke_seconds: float | None = None) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as e:
        raise RuntimeError(f"Tkinter control panel unavailable: {e}") from e

    root = tk.Tk()
    root.title("Windows Typeless Voice Input")
    root.geometry("820x620")
    root.minsize(760, 520)

    daemon: dict[str, subprocess.Popen[str] | None] = {"proc": None}
    daemon_mode = tk.StringVar(value="未启动")
    summary_var = tk.StringVar()
    status_var = tk.StringVar(value="Ready")

    def capture(fn: Any) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            fn()
        return buf.getvalue().strip()

    def append(text: str) -> None:
        output.configure(state="normal")
        output.insert("end", text.rstrip() + "\n")
        output.see("end")
        output.configure(state="disabled")

    def run_task(label: str, fn: Any) -> None:
        status_var.set(f"Running {label}...")

        def worker() -> None:
            try:
                result = capture(fn)
                root.after(0, lambda: append(f"$ {label}\n{result or 'OK'}\n"))
                root.after(0, lambda: status_var.set(f"{label} done"))
            except Exception as e:
                root.after(0, lambda: append(f"$ {label}\nFAIL {e}\n"))
                root.after(0, lambda: status_var.set(f"{label} failed"))

        threading.Thread(target=worker, daemon=True).start()

    def config_summary() -> str:
        stt = cfg.get("stt", {})
        llm = cfg.get("llm", {})
        hotkeys = cfg.get("hotkeys", {})
        paste = cfg.get("paste", {})
        entries = load_dictionary_entries(cfg)
        store = ResultStore(cfg)
        usage_path = UsageStore(cfg).path
        feedback_store = FeedbackStore(cfg)
        runtime_status = RuntimeStatusStore(cfg)
        daemon_lock = DaemonLock(cfg)
        daemon_control = DaemonControlStore(cfg)
        last = "missing"
        if store.path.exists():
            try:
                payload = store.load()
                last = (
                    f"exists; chars={payload.get('chars')}; "
                    f"raw_chars={payload.get('raw_chars')}; "
                    f"created_at={payload.get('created_at')}"
                )
            except Exception:
                last = "exists; unreadable"
        return "\n".join(
            [
                f"Hotkey: toggle={hotkeys.get('toggle')} quit={hotkeys.get('quit')} copy_last={hotkeys.get('copy_last')}",
                f"STT: {stt.get('engine')} / {stt.get('model')}",
                f"LLM: {llm.get('provider')} / {llm.get('model')} / fallback={llm.get('fallback_model')}",
                (
                    f"Paste: method={paste.get('method')} "
                    f"focus_guard={paste.get('focus_guard_enabled')} "
                    f"restore_clipboard={paste.get('restore_clipboard')}"
                ),
                f"Dictionary entries: {len(entries)}",
                f"Last result: {last}",
                f"Usage log: {usage_path}; exists={usage_path.exists()}",
                f"Bad feedback: {feedback_store.path}; count={feedback_store.count()}",
                runtime_status.summary(),
                daemon_lock.summary(),
                daemon_control.summary(),
                daily_report_status_line(),
                f"Daemon started from panel: {daemon_mode.get()}",
            ]
        )

    def refresh() -> None:
        summary_var.set(config_summary())

    def open_path(path: Path, create_parent: bool = False, create_file: bool = False) -> None:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        if create_file and not path.exists():
            path.write_text("", encoding="utf-8")
        if not path.exists():
            messagebox.showwarning("Missing path", str(path))
            return
        os.startfile(str(path))  # type: ignore[attr-defined]

    def start_daemon(extra: list[str], label: str) -> None:
        proc = daemon.get("proc")
        if proc and proc.poll() is None:
            append("Daemon already running from this panel. Stop it first.\n")
            return
        cmd = [sys.executable, "-m", "win_voice_input", *extra]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONPATH"] = str(ROOT / "src")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        daemon["proc"] = proc
        daemon_mode.set(f"{label}; pid={proc.pid}")
        refresh()
        append(f"Started {label}; pid={proc.pid}\n")

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                root.after(0, lambda line=line: append("[daemon] " + line.rstrip()))
            code = proc.wait()
            root.after(0, lambda: daemon_mode.set(f"stopped; exit={code}"))
            root.after(0, refresh)

        threading.Thread(target=reader, daemon=True).start()

    def stop_daemon() -> None:
        proc = daemon.get("proc")
        if not proc or proc.poll() is not None:
            append("No panel-started daemon is running.\n")
            return
        proc.terminate()
        append(f"Stopping daemon pid={proc.pid}\n")

    def stop_active_daemon() -> None:
        ok = request_daemon_stop(cfg, source="control_panel", wait_seconds=0)
        append("Stop active daemon requested.\n" if ok else "Stop active daemon request failed.\n")
        refresh()

    def copy_last() -> None:
        try:
            payload = ResultStore(cfg).load()
            text = str(payload["text"])
            TextPaster(cfg).paste(text, clipboard_only=True)
            append(f"Copied last result to clipboard; chars={len(text)}\n")
            refresh()
        except Exception as e:
            append(f"Copy last failed: {e}\n")

    def generate_daily_report_for_panel() -> None:
        json_path, md_path, output = generate_daily_trial_report(cfg)
        print(output)
        print(f"Daily report markdown: {md_path}")
        print(f"Daily report json: {json_path}")
        root.after(0, refresh)

    def mark_bad_dialog() -> None:
        try:
            payload = ResultStore(cfg).load()
        except Exception as e:
            messagebox.showwarning("Mark bad", f"No last result to mark: {e}")
            return

        win = tk.Toplevel(root)
        win.title("Mark bad result")
        win.geometry("520x300")
        win.minsize(480, 260)

        tk.Label(win, text="Mark the most recent result as bad", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        tk.Label(
            win,
            text=(
                f"created_at={payload.get('created_at')}  mode={payload.get('mode')}  "
                f"chars={payload.get('chars')}  raw_chars={payload.get('raw_chars')}"
            ),
            fg="#666",
            wraplength=480,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 10))
        tk.Label(
            win,
            text="Click one category. The index log stores metadata only; the full private case snapshot stays under ignored logs/bad-cases/.",
            fg="#666",
            wraplength=480,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 10))

        grid = tk.Frame(win, padx=12)
        grid.pack(fill="x")

        def mark(category: dict[str, str]) -> None:
            try:
                record = FeedbackStore(cfg).mark_bad(category["slug"], source="control_panel")
            except Exception as e:
                messagebox.showerror("Mark bad", str(e))
                return
            append(f"Marked bad: {record['category']} id={record['id']} case={record['case_path']}\n")
            status_var.set(f"Marked bad: {record['category']}")
            refresh()
            win.destroy()

        for idx, category in enumerate(BAD_FEEDBACK_CATEGORIES):
            tk.Button(
                grid,
                text=category["label"],
                command=lambda category=category: mark(category),
                height=2,
            ).grid(row=idx // 2, column=idx % 2, padx=5, pady=5, sticky="ew")
        for col in range(2):
            grid.columnconfigure(col, weight=1)

    def manage_dictionary() -> None:
        win = tk.Toplevel(root)
        win.title("Personal Dictionary")
        win.geometry("760x520")
        win.minsize(680, 460)

        phrase_var = tk.StringVar()
        aliases_var = tk.StringVar()
        pronunciation_var = tk.StringVar()
        path_var = tk.StringVar(value=str(local_dictionary_path(cfg)))
        entries: list[dict[str, Any]] = []

        def refresh_entries() -> None:
            nonlocal entries
            entries = load_local_dictionary_entries(cfg)
            listbox.delete(0, "end")
            for entry in entries:
                listbox.insert("end", entry["phrase"])
            path_var.set(str(local_dictionary_path(cfg)))
            refresh()

        def selected_entry() -> dict[str, Any] | None:
            selection = listbox.curselection()
            if not selection:
                return None
            idx = int(selection[0])
            if idx < 0 or idx >= len(entries):
                return None
            return entries[idx]

        def clear_fields() -> None:
            phrase_var.set("")
            aliases_var.set("")
            pronunciation_var.set("")
            notes.delete("1.0", "end")

        def load_selected(_event: Any | None = None) -> None:
            entry = selected_entry()
            if not entry:
                return
            phrase_var.set(entry.get("phrase", ""))
            aliases_var.set(", ".join(entry.get("aliases") or []))
            pronunciation_var.set(entry.get("pronunciation", ""))
            notes.delete("1.0", "end")
            notes.insert("1.0", entry.get("notes", ""))

        def save_entry() -> None:
            aliases = [
                item.strip()
                for item in re.split(r"[,，;；]", aliases_var.get())
                if item.strip()
            ]
            try:
                total = upsert_local_dictionary_entry(
                    cfg,
                    phrase=phrase_var.get(),
                    aliases=aliases,
                    pronunciation=pronunciation_var.get(),
                    notes=notes.get("1.0", "end").strip(),
                )
            except Exception as e:
                messagebox.showerror("Dictionary", str(e))
                return
            refresh_entries()
            append(f"Dictionary entry saved; local_entries={total}\n")

        def delete_selected() -> None:
            entry = selected_entry()
            if not entry:
                messagebox.showinfo("Dictionary", "Select an entry first.")
                return
            if not messagebox.askyesno("Dictionary", "Delete selected local dictionary entry?"):
                return
            if delete_local_dictionary_entry(cfg, entry["phrase"]):
                clear_fields()
                refresh_entries()
                append("Dictionary entry deleted.\n")
            else:
                messagebox.showwarning("Dictionary", "Entry was not found.")

        left = tk.Frame(win, padx=10, pady=10)
        left.pack(side="left", fill="both", expand=False)
        tk.Label(left, text="Local entries", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        listbox = tk.Listbox(left, width=32, height=22)
        listbox.pack(fill="both", expand=True)
        listbox.bind("<<ListboxSelect>>", load_selected)

        right = tk.Frame(win, padx=10, pady=10)
        right.pack(side="right", fill="both", expand=True)
        tk.Label(right, textvariable=path_var, fg="#666", wraplength=430, justify="left").pack(anchor="w")

        form = tk.Frame(right)
        form.pack(fill="x", pady=(10, 0))
        tk.Label(form, text="Phrase").grid(row=0, column=0, sticky="w", pady=4)
        tk.Entry(form, textvariable=phrase_var).grid(row=0, column=1, sticky="ew", pady=4)
        tk.Label(form, text="Aliases").grid(row=1, column=0, sticky="w", pady=4)
        tk.Entry(form, textvariable=aliases_var).grid(row=1, column=1, sticky="ew", pady=4)
        tk.Label(form, text="Pronunciation").grid(row=2, column=0, sticky="w", pady=4)
        tk.Entry(form, textvariable=pronunciation_var).grid(row=2, column=1, sticky="ew", pady=4)
        form.columnconfigure(1, weight=1)

        tk.Label(right, text="Notes").pack(anchor="w", pady=(10, 2))
        notes = tk.Text(right, height=8, wrap="word")
        notes.pack(fill="both", expand=True)

        actions = tk.Frame(right)
        actions.pack(fill="x", pady=10)
        tk.Button(actions, text="New", command=clear_fields).pack(side="left", padx=4)
        tk.Button(actions, text="Add / Update", command=save_entry).pack(side="left", padx=4)
        tk.Button(actions, text="Delete selected", command=delete_selected).pack(side="left", padx=4)
        tk.Button(actions, text="Refresh", command=refresh_entries).pack(side="left", padx=4)

        tk.Label(
            right,
            text="Only dictionary.local.jsonl is edited here. Public dictionary.txt is read-only in this panel.",
            fg="#666",
            wraplength=430,
            justify="left",
        ).pack(anchor="w")
        refresh_entries()

    top = tk.Frame(root, padx=12, pady=10)
    top.pack(fill="x")
    tk.Label(top, text="Windows Typeless Voice Input", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    tk.Label(top, textvariable=status_var, fg="#555").pack(anchor="w")

    summary = tk.Label(root, textvariable=summary_var, justify="left", anchor="w", padx=12)
    summary.pack(fill="x")

    buttons = tk.Frame(root, padx=12, pady=8)
    buttons.pack(fill="x")
    tk.Button(buttons, text="Refresh", command=refresh).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Dry run", command=lambda: run_task("dry-run", lambda: dry_run(cfg))).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="API smoke", command=lambda: run_task("api-smoke", lambda: api_smoke(cfg))).grid(row=0, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Overlay smoke", command=lambda: run_task("overlay-smoke", lambda: overlay_smoke(cfg))).grid(row=0, column=3, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Start daemon", command=lambda: start_daemon([], "daemon")).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Start clipboard-only", command=lambda: start_daemon(["--clipboard-only"], "clipboard-only daemon")).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Start daily safe", command=lambda: start_daemon(["--daily"], "daily safe daemon")).grid(row=1, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Stop panel daemon", command=stop_daemon).grid(row=1, column=3, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Copy last", command=copy_last).grid(row=2, column=3, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open config", command=lambda: open_path(ROOT / "config.json" if (ROOT / "config.json").exists() else ROOT / "config.example.json")).grid(row=2, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open dictionary", command=lambda: open_path(ROOT / "dictionary.local.jsonl", create_file=True)).grid(row=2, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open logs", command=lambda: open_path(ROOT / "logs", create_parent=True)).grid(row=2, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Manage dictionary", command=manage_dictionary).grid(row=3, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open usage log", command=lambda: open_path(UsageStore(cfg).path, create_parent=True, create_file=True)).grid(row=3, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Mark bad", command=mark_bad_dialog).grid(row=3, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open bad feedback", command=lambda: open_path(FeedbackStore(cfg).path, create_parent=True, create_file=True)).grid(row=3, column=3, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Daily report", command=lambda: run_task("daily-report", generate_daily_report_for_panel)).grid(row=4, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open daily report", command=lambda: open_path(daily_report_paths()[1])).grid(row=4, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open daemon status", command=lambda: open_path(RuntimeStatusStore(cfg).path, create_parent=True, create_file=True)).grid(row=4, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open README", command=lambda: open_path(ROOT / "README.md")).grid(row=4, column=3, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open daemon lock", command=lambda: open_path(DaemonLock(cfg).path, create_parent=True, create_file=True)).grid(row=5, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Stop active daemon", command=stop_active_daemon).grid(row=5, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(buttons, text="Open daemon control", command=lambda: open_path(DaemonControlStore(cfg).path, create_parent=True, create_file=True)).grid(row=5, column=2, padx=4, pady=4, sticky="ew")
    for col in range(4):
        buttons.columnconfigure(col, weight=1)

    output = tk.Text(root, height=18, state="disabled", wrap="word")
    output.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def on_close() -> None:
        proc = daemon.get("proc")
        if proc and proc.poll() is None:
            if not messagebox.askyesno("Daemon running", "Stop daemon started from this panel?"):
                root.destroy()
                return
            proc.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    append("Control panel ready. This panel does not print secrets or dictionary contents.\n")
    if smoke_seconds is not None:
        root.after(int(smoke_seconds * 1000), root.destroy)
    root.mainloop()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows Typeless-like voice input")
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--mode", default="default")
    parser.add_argument("--audio-file", help="Transcribe this audio file once")
    parser.add_argument("--text", help="Enhance this transcript text once")
    parser.add_argument("--no-paste", action="store_true", help="Print output instead of pasting")
    parser.add_argument("--no-print-output", action="store_true", help="With --no-paste, suppress printing the generated text")
    parser.add_argument("--clipboard-only", action="store_true", help="Copy output to clipboard without pressing Ctrl+V")
    parser.add_argument("--daily", action="store_true", help="Start safe daily daemon mode: clipboard-only, overlay on, no auto Ctrl+V")
    parser.add_argument("--paste-method", choices=["hotkey", "clipboard", "autohotkey"], help="Override paste.method for this run")
    parser.add_argument("--copy-last", action="store_true", help="Copy the most recent saved result to clipboard")
    parser.add_argument("--show-last", action="store_true", help="Print the most recent saved result")
    parser.add_argument("--mark-bad", help="Mark the most recent saved result as bad with a category label or slug")
    parser.add_argument("--mark-bad-note", default="", help="Optional local note for --mark-bad")
    parser.add_argument("--daily-report", action="store_true", help="Generate the local metadata-only daily trial report")
    parser.add_argument("--daily-report-output-prefix", type=Path, help="Output prefix for --daily-report")
    parser.add_argument("--daemon-status", action="store_true", help="Print the local daemon status metadata summary")
    parser.add_argument("--stop-daemon", action="store_true", help="Request the active daemon to stop via the local control file")
    parser.add_argument("--stop-timeout-seconds", type=float, default=5.0, help="Seconds to wait for --stop-daemon; 0 means fire-and-forget")
    parser.add_argument("--no-overlay", action="store_true", help="Disable the Typeless-like status overlay")
    parser.add_argument("--overlay-smoke", action="store_true", help="Show a short recording/thinking/done overlay demo")
    parser.add_argument("--api-smoke", action="store_true", help="Check configured provider connectivity without inference calls")
    parser.add_argument("--control-panel", action="store_true", help="Open the Windows control panel")
    parser.add_argument("--control-panel-smoke", action="store_true", help="Open the control panel briefly for smoke testing")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices")
    parser.add_argument("--dry-run", action="store_true", help="Validate config without loading models")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    cfg = load_config(args.config)
    if args.paste_method:
        cfg["paste"]["method"] = args.paste_method
    if args.no_print_output:
        cfg["paste"]["suppress_output_when_disabled"] = True
    if args.daily:
        args.clipboard_only = True
        cfg["paste"]["suppress_output_when_disabled"] = True

    if args.dry_run:
        dry_run(cfg)
        return 0
    if args.api_smoke:
        api_smoke(cfg)
        return 0
    if args.control_panel or args.control_panel_smoke:
        control_panel(cfg, smoke_seconds=1.2 if args.control_panel_smoke else None)
        return 0
    if args.list_devices:
        list_devices()
        return 0
    if args.overlay_smoke:
        overlay_smoke(cfg)
        return 0
    if args.daemon_status:
        print(RuntimeStatusStore(cfg).summary())
        print(DaemonLock(cfg).summary())
        print(DaemonControlStore(cfg).summary())
        return 0
    if args.stop_daemon:
        return 0 if request_daemon_stop(cfg, source="cli", wait_seconds=args.stop_timeout_seconds) else 1
    if args.show_last or args.copy_last:
        try:
            store = ResultStore(cfg)
            payload = store.load()
            text = str(payload["text"])
            if args.show_last:
                print("\n--- LAST RESULT START ---")
                print(text)
                print("--- LAST RESULT END ---\n")
            if args.copy_last:
                TextPaster(cfg).paste(text, clipboard_only=True)
        except Exception as e:
            log(f"[history] failed: {e}")
            return 1
        return 0
    if args.mark_bad:
        try:
            FeedbackStore(cfg).mark_bad(args.mark_bad, source="cli", note=args.mark_bad_note)
        except Exception as e:
            log(f"[feedback] failed: {e}")
            return 1
        return 0
    if args.daily_report:
        try:
            json_path, md_path, output = generate_daily_trial_report(cfg, args.daily_report_output_prefix)
            if output:
                print(output)
            print(f"Daily report markdown: {md_path}")
            print(f"Daily report json: {json_path}")
        except Exception as e:
            log(f"[daily-report] failed: {e}")
            return 1
        return 0

    overlay = None if args.no_overlay else StatusOverlay(cfg)
    if overlay and (args.audio_file or args.text):
        overlay.start()
    pipeline = VoicePipeline(
        cfg,
        no_paste=args.no_paste,
        clipboard_only=args.clipboard_only,
        status_overlay=overlay,
    )
    if args.text:
        try:
            pipeline.process_text(args.mode, args.text)
        except Exception as e:
            log(f"[run] failed: {e}")
            return 1
        finally:
            if overlay:
                time.sleep(0.4)
                overlay.close()
        return 0
    if args.audio_file:
        try:
            pipeline.process_audio_file(args.mode, Path(args.audio_file))
        except Exception as e:
            log(f"[run] failed: {e}")
            return 1
        finally:
            if overlay:
                time.sleep(0.4)
                overlay.close()
        return 0

    run_daemon(
        cfg,
        no_paste=args.no_paste,
        clipboard_only=args.clipboard_only,
        no_overlay=args.no_overlay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
