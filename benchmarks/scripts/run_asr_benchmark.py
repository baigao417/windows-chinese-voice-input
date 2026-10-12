from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from smoke_asr_providers import load_json, is_set, post_audio_transcription

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT.parent / "examples" / "audio"
RESULTS = ROOT / "results"
SECRETS = ROOT / "secrets.local.json"

PUNCT_RE = re.compile(r"[\s\u3000，。！？；：、“”‘’（）【】《》,.!?;:'\"()\[\]{}<>\-—_`~]")


def normalize_for_cer(s: str) -> str:
    s = s.lower().strip()
    s = PUNCT_RE.sub("", s)
    return s


def edit_distance(a: str, b: str) -> int:
    # DP over codepoints; memory optimized
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    r = normalize_for_cer(ref)
    h = normalize_for_cer(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


def duration_sec(path: Path) -> float | None:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)
        ], text=True, stderr=subprocess.DEVNULL).strip()
        return round(float(out), 3)
    except Exception:
        return None


def sample_pairs(raw_dir: Path) -> list[tuple[str, Path, Path]]:
    pairs = []
    for audio in sorted(raw_dir.glob("*.m4a"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
        ref = audio.with_suffix(".txt")
        if ref.exists():
            pairs.append((audio.stem, audio, ref))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="siliconflow,groq")
    parser.add_argument("--raw-dir", default=str(RAW))
    parser.add_argument("--secrets", default=str(SECRETS))
    parser.add_argument("--volc-endpoint", default="nostream")
    parser.add_argument("--output-prefix", default=time.strftime("asr-benchmark-%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    secrets = load_json(Path(args.secrets))
    providers = {p.strip().lower() for p in args.providers.split(",") if p.strip()}
    pairs = sample_pairs(raw_dir)
    if not pairs:
        raise SystemExit(f"No audio/reference pairs found in {raw_dir}")

    jobs: list[tuple[str, str, str, str]] = []  # provider, model, url, key
    if "siliconflow" in providers:
        sf = secrets.get("siliconflow", {})
        key = sf.get("api_key", "")
        if is_set(key):
            base_url = str(sf.get("base_url") or "https://api.siliconflow.cn/v1").rstrip("/")
            for model in sf.get("asr_models") or ["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"]:
                jobs.append(("siliconflow", model, f"{base_url}/audio/transcriptions", key))
    if "groq" in providers:
        g = secrets.get("groq", {})
        key = g.get("api_key", "")
        if is_set(key):
            for model in g.get("models") or ["whisper-large-v3", "whisper-large-v3-turbo"]:
                jobs.append(("groq", model, "https://api.groq.com/openai/v1/audio/transcriptions", key))

    RESULTS.mkdir(parents=True, exist_ok=True)
    csv_path = RESULTS / f"{args.output_prefix}.csv"
    json_path = RESULTS / f"{args.output_prefix}.json"
    md_path = RESULTS / f"{args.output_prefix}.md"

    rows: list[dict[str, Any]] = []
    for sample_id, audio, ref_path in pairs:
        ref = ref_path.read_text(encoding="utf-8").strip()
        dur = duration_sec(audio)
        for provider, model, url, key in jobs:
            print(f"RUN {sample_id} {provider} {model}", flush=True)
            r = post_audio_transcription(url, key, model, audio, timeout=180)
            row = {
                "sample_id": sample_id,
                "audio": str(audio),
                "duration_sec": dur,
                "reference_chars": len(ref),
                "provider": provider,
                "model": model,
                "ok": r.ok,
                "status": r.status,
                "elapsed_ms": r.elapsed_ms,
                "rtf_wall": round((r.elapsed_ms / 1000) / dur, 4) if dur else "",
                "chars": r.chars,
                "cer_norm": round(cer(ref, r.text), 4) if r.ok else "",
                "hypothesis": r.text,
                "error_type": r.error_type,
                "error": r.error,
            }
            row["hypothesis_truncated_at_120"] = False
            rows.append(row)
        if "volcengine" in providers or "doubao" in providers or "volcengine_doubao_asr" in providers:
            from smoke_volcengine_asr import run_smoke as run_volcengine_smoke

            model = f"bigmodel_{args.volc_endpoint}"
            print(f"RUN {sample_id} volcengine_doubao_asr {model}", flush=True)
            vr = run_volcengine_smoke(Path(args.secrets), raw_dir, audio, args.volc_endpoint, timeout=180)
            row = {
                "sample_id": sample_id,
                "audio": str(audio),
                "duration_sec": dur,
                "reference_chars": len(ref),
                "provider": "volcengine_doubao_asr",
                "model": model,
                "ok": vr["ok"],
                "status": vr["status"],
                "elapsed_ms": vr["elapsed_ms"],
                "rtf_wall": round((vr["elapsed_ms"] / 1000) / dur, 4) if dur else "",
                "chars": vr["chars"],
                "cer_norm": round(cer(ref, vr["text"]), 4) if vr["ok"] else "",
                "hypothesis": vr["text"],
                "error_type": "",
                "error": vr["error"],
            }
            row["hypothesis_truncated_at_120"] = False
            rows.append(row)

    # Recompute note: if truncated, CER is not reliable. This script is v0 smoke-benchmark; full transcript support next.
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    summary: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["provider"], row["model"])
        s = summary.setdefault(key, {"n": 0, "ok": 0, "elapsed": [], "rtf": [], "cer": [], "truncated": 0})
        s["n"] += 1
        if row["ok"]:
            s["ok"] += 1
            s["elapsed"].append(float(row["elapsed_ms"]))
            if row["rtf_wall"] != "": s["rtf"].append(float(row["rtf_wall"]))
            if row["cer_norm"] != "": s["cer"].append(float(row["cer_norm"]))
            if row["hypothesis_truncated_at_120"]: s["truncated"] += 1

    lines = ["# ASR benchmark smoke summary", "", f"Raw dir: `{raw_dir}`", f"Samples: {len(pairs)}", "", "| Provider | Model | OK | avg elapsed ms | avg RTF wall | avg CER* | truncated |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for (provider, model), s in summary.items():
        avg_elapsed = sum(s["elapsed"]) / len(s["elapsed"]) if s["elapsed"] else 0
        avg_rtf = sum(s["rtf"]) / len(s["rtf"]) if s["rtf"] else 0
        avg_cer = sum(s["cer"]) / len(s["cer"]) if s["cer"] else 0
        lines.append(f"| {provider} | {model} | {s['ok']}/{s['n']} | {avg_elapsed:.1f} | {avg_rtf:.3f} | {avg_cer:.3f} | {s['truncated']} |")
    lines += ["", "* CER computed on normalized full transcript: whitespace and punctuation removed; case folded."]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"CSV={csv_path}")
    print(f"JSON={json_path}")
    print(f"MD={md_path}")
    print("SUMMARY")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

