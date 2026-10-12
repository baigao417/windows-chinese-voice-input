from __future__ import annotations

import argparse
import json
import mimetypes
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request, error


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / "secrets.local.json"
DEFAULT_RAW = ROOT.parent / "examples" / "audio"
DEFAULT_RESULTS = ROOT / "results"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_set(value: Any) -> bool:
    return isinstance(value, str) and value.strip() and "填到" not in value and "不要填" not in value


def mask_state(value: Any) -> str:
    if not is_set(value):
        return "empty"
    return f"set_{len(str(value))}_chars"


def choose_audio(raw_dir: Path) -> Path:
    audios = sorted(
        [p for p in raw_dir.iterdir() if p.suffix.lower() in {".wav", ".m4a", ".mp3", ".ogg", ".flac"}],
        key=lambda p: p.stat().st_size,
    )
    if not audios:
        raise SystemExit(f"No audio files found in {raw_dir}")
    return audios[0]


def multipart_form(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----codex-smoke-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
    parts.append(file_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass
class SmokeResult:
    provider: str
    model: str
    ok: bool
    status: str
    elapsed_ms: float
    text: str = ""
    text_preview: str = ""
    chars: int = 0
    error_type: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def post_audio_transcription(url: str, api_key: str, model: str, audio: Path, timeout: int = 90) -> SmokeResult:
    fields = {"model": model}
    body, content_type = multipart_form(fields, "file", audio)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": "voice-input-benchmark/0.1",
    }
    req = request.Request(url, data=body, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            elapsed = (time.perf_counter() - t0) * 1000
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw}
            text = data.get("text") or data.get("result") or data.get("raw", "")
            text = str(text).strip()
            return SmokeResult(
                provider="",
                model=model,
                ok=True,
                status=f"HTTP {getattr(resp, 'status', '')}",
                elapsed_ms=round(elapsed, 2),
                text=text,
                text_preview=text[:120],
                chars=len(text),
            )
    except error.HTTPError as e:
        elapsed = (time.perf_counter() - t0) * 1000
        detail = e.read().decode("utf-8", errors="replace")[:500]
        return SmokeResult("", model, False, f"HTTP {e.code}", round(elapsed, 2), error_type="HTTPError", error=detail)
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return SmokeResult("", model, False, "exception", round(elapsed, 2), error_type=type(e).__name__, error=str(e)[:500])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW))
    parser.add_argument("--audio", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--providers", default="siliconflow,groq", help="comma list: siliconflow,groq")
    args = parser.parse_args()

    secrets_path = Path(args.secrets)
    raw_dir = Path(args.raw_dir)
    audio = Path(args.audio) if args.audio else choose_audio(raw_dir)
    out_path = Path(args.output) if args.output else DEFAULT_RESULTS / f"provider-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = load_json(secrets_path)
    providers = {p.strip().lower() for p in args.providers.split(",") if p.strip()}
    results: list[SmokeResult] = []

    if "siliconflow" in providers:
        sf = cfg.get("siliconflow", {})
        key = sf.get("api_key", "")
        base_url = str(sf.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
        models = sf.get("asr_models") or ["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"]
        if is_set(key):
            for model in models:
                r = post_audio_transcription(f"{base_url}/audio/transcriptions", key, model, audio)
                r.provider = "siliconflow"
                results.append(r)
        else:
            results.append(SmokeResult("siliconflow", "", False, "skipped", 0, error_type="missing_key", error="siliconflow.api_key empty"))

    if "groq" in providers:
        groq = cfg.get("groq", {})
        key = groq.get("api_key", "")
        models = groq.get("models") or ["whisper-large-v3", "whisper-large-v3-turbo"]
        if is_set(key):
            for model in models:
                r = post_audio_transcription("https://api.groq.com/openai/v1/audio/transcriptions", key, model, audio)
                r.provider = "groq"
                results.append(r)
        else:
            results.append(SmokeResult("groq", "", False, "skipped", 0, error_type="missing_key", error="groq.api_key empty"))

    safe_config = {
        "secrets_file": str(secrets_path),
        "audio": str(audio),
        "audio_bytes": audio.stat().st_size,
        "key_state": {
            "siliconflow": mask_state(cfg.get("siliconflow", {}).get("api_key")),
            "groq": mask_state(cfg.get("groq", {}).get("api_key")),
            "volcengine_doubao_asr.api_key": mask_state(cfg.get("volcengine_doubao_asr", {}).get("api_key")),
            "dashscope_qwen": mask_state(cfg.get("dashscope_qwen", {}).get("api_key")),
        },
        "results": [r.to_dict() for r in results],
    }
    out_path.write_text(json.dumps(safe_config, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"AUDIO={audio}")
    print(f"OUTPUT={out_path}")
    for r in results:
        print(f"{r.provider}\t{r.model}\tok={r.ok}\tstatus={r.status}\telapsed_ms={r.elapsed_ms}\tchars={r.chars}\terror_type={r.error_type}")
        if r.text_preview:
            print(f"  preview={r.text_preview}")
        if r.error:
            print(f"  error={r.error[:200].replace(chr(10), ' ')}")
    return 0 if all(r.ok for r in results if r.status != "skipped") else 2


if __name__ == "__main__":
    raise SystemExit(main())

