from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any


from run_asr_benchmark import cer, duration_sec, sample_pairs
from smoke_volcengine_asr import (
    ENDPOINTS,
    audio_only_request,
    full_client_request,
    is_set,
    load_json,
    parse_server_message,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / "secrets.local.json"
DEFAULT_RAW = ROOT.parent / "examples" / "audio"
DEFAULT_RESULTS = ROOT / "results"


def pcm_s16le_16k_mono(input_path: Path) -> bytes:
    return subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-",
        ],
        stderr=subprocess.DEVNULL,
    )


def chunks(data: bytes, chunk_bytes: int) -> list[bytes]:
    return [data[i : i + chunk_bytes] for i in range(0, len(data), chunk_bytes)]


def extract_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text
    if isinstance(result, list):
        return "".join(item.get("text", "") for item in result if isinstance(item, dict))
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def has_definite_utterance(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    utterances = result.get("utterances") if isinstance(result, dict) else None
    if not isinstance(utterances, list):
        return False
    return any(isinstance(u, dict) and u.get("definite") for u in utterances)


def prefix_instability(prev: str, cur: str) -> float:
    if not prev:
        return 0.0
    n = min(len(prev), len(cur))
    i = 0
    while i < n and prev[i] == cur[i]:
        i += 1
    return 1.0 - (i / len(prev))


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    blocked = {"final_text", "responses"}
    clean = {key: value for key, value in row.items() if key not in blocked}
    if "final_chars" not in clean:
        clean["final_chars"] = len(str(row.get("final_text") or ""))
    return clean


def auth_headers(cfg: dict[str, Any], resource_id: str, request_id: str, connect_id: str) -> tuple[list[str], str]:
    api_key = cfg.get("api_key")
    app_key = cfg.get("app_key")
    access_key = cfg.get("access_key")
    headers = [
        f"X-Api-Resource-Id: {resource_id}",
        f"X-Api-Request-Id: {request_id}",
        f"X-Api-Connect-Id: {connect_id}",
        "X-Api-Sequence: -1",
    ]
    if is_set(api_key):
        headers.append(f"X-Api-Key: {api_key}")
        return headers, "api_key"
    if is_set(app_key) and is_set(access_key):
        headers.append(f"X-Api-App-Key: {app_key}")
        headers.append(f"X-Api-Access-Key: {access_key}")
        return headers, "app_access_key"
    raise RuntimeError("volcengine_doubao_asr credentials are missing")


def run_stream_sample(
    secrets_path: Path,
    audio_path: Path,
    *,
    endpoint_name: str,
    chunk_ms: int,
    pace: str,
    timeout: float,
) -> dict[str, Any]:
    import uuid
    import websocket  # websocket-client; only needed when connecting.

    secrets = load_json(secrets_path)
    cfg = secrets.get("volcengine_doubao_asr", {})
    resource_id = cfg.get("resource_id") or "volc.bigasr.sauc.duration"
    if not is_set(resource_id):
        resource_id = "volc.bigasr.sauc.duration"
    endpoint = ENDPOINTS.get(endpoint_name, endpoint_name)
    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    headers, auth_mode = auth_headers(cfg, resource_id, request_id, connect_id)

    pcm = pcm_s16le_16k_mono(audio_path)
    chunk_bytes = int(16000 * 2 * (chunk_ms / 1000))
    audio_chunks = chunks(pcm, chunk_bytes)
    request_payload = {
        "user": {"uid": "codex-stream-benchmark"},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
            "language": "zh-CN",
        },
        "request": {
            "model_name": "bigmodel",
            "enable_nonstream": True,
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
        },
    }

    responses: list[dict[str, Any]] = []
    text_events: list[dict[str, Any]] = []
    errors: list[str] = []
    final_text = ""
    first_text_at: float | None = None
    first_definite_at: float | None = None
    final_at: float | None = None
    audio_end_at: float | None = None
    logid = ""

    start = time.perf_counter()
    ws = websocket.create_connection(endpoint, header=headers, timeout=timeout, enable_multithread=True)
    response_headers = getattr(ws, "headers", {}) or {}
    if isinstance(response_headers, dict):
        logid = response_headers.get("x-tt-logid") or response_headers.get("X-Tt-Logid") or ""
    ws.settimeout(0.03)

    def recv_available(block_timeout: float | None = None, max_messages: int | None = None) -> None:
        nonlocal final_text, first_text_at, final_at, first_definite_at
        old_timeout = ws.gettimeout()
        if block_timeout is not None:
            ws.settimeout(block_timeout)
        seen = 0
        try:
            while True:
                if max_messages is not None and seen >= max_messages:
                    break
                try:
                    raw = ws.recv()
                except TimeoutError:
                    break
                except websocket.WebSocketTimeoutException:
                    break
                now = time.perf_counter()
                seen += 1
                parsed = parse_server_message(raw)
                parsed["at_ms"] = round((now - start) * 1000, 2)
                responses.append(parsed)
                if parsed.get("message_type") == 0xF:
                    errors.append(str(parsed.get("payload") or parsed.get("error_code") or "server_error"))
                    break
                text = extract_text(parsed.get("payload"))
                if text and text != final_text:
                    final_text = text
                    if first_text_at is None:
                        first_text_at = now
                    if first_definite_at is None and has_definite_utterance(parsed.get("payload")):
                        first_definite_at = now
                    text_events.append({"at_ms": round((now - start) * 1000, 2), "text": text})
                if parsed.get("flags") == 0x3:
                    final_at = now
                    break
        finally:
            if block_timeout is not None:
                ws.settimeout(old_timeout)

    try:
        ws.send_binary(full_client_request(request_payload))
        recv_available(block_timeout=min(timeout, 3.0), max_messages=1)

        for i, chunk in enumerate(audio_chunks):
            is_last = i == len(audio_chunks) - 1
            tick = time.perf_counter()
            ws.send_binary(audio_only_request(chunk, last=is_last))
            if is_last:
                audio_end_at = time.perf_counter()
            recv_available()
            if pace == "realtime" and not is_last:
                elapsed = time.perf_counter() - tick
                sleep_for = max(0.0, (chunk_ms / 1000) - elapsed)
                if sleep_for:
                    time.sleep(sleep_for)

        deadline = time.perf_counter() + timeout
        while final_at is None and time.perf_counter() < deadline:
            recv_available(block_timeout=0.2)
            if responses and responses[-1].get("flags") == 0x3:
                break
        if final_at is None:
            final_at = time.perf_counter()
    finally:
        try:
            ws.close()
        except Exception:
            pass

    total_wall_ms = (final_at - start) * 1000 if final_at else (time.perf_counter() - start) * 1000
    audio_end = audio_end_at or final_at or time.perf_counter()
    first_text_ms = ((first_text_at - start) * 1000) if first_text_at else None
    first_definite_ms = ((first_definite_at - start) * 1000) if first_definite_at else None
    final_latency_ms = ((final_at - audio_end) * 1000) if final_at else None

    instabilities = [
        prefix_instability(text_events[i - 1]["text"], text_events[i]["text"])
        for i in range(1, len(text_events))
    ]

    return {
        "provider": "volcengine_doubao_asr",
        "endpoint": endpoint_name,
        "resource_id": resource_id,
        "auth_mode": auth_mode,
        "request_id": request_id,
        "connect_id": connect_id,
        "logid": logid,
        "audio": str(audio_path),
        "chunk_ms": chunk_ms,
        "chunks": len(audio_chunks),
        "pace": pace,
        "ok": bool(final_text) and not errors,
        "errors": errors,
        "first_text_ms": round(first_text_ms, 2) if first_text_ms is not None else "",
        "first_definite_ms": round(first_definite_ms, 2) if first_definite_ms is not None else "",
        "final_latency_ms": round(final_latency_ms, 2) if final_latency_ms is not None else "",
        "total_wall_ms": round(total_wall_ms, 2),
        "text_events": len(text_events),
        "revision_count": max(0, len(text_events) - 1),
        "avg_prefix_instability": round(statistics.mean(instabilities), 4) if instabilities else 0.0,
        "final_chars": len(final_text),
        "final_text": final_text,
        "responses": responses,
    }


def write_outputs(rows: list[dict[str, Any]], output_prefix: str) -> tuple[Path, Path, Path]:
    DEFAULT_RESULTS.mkdir(parents=True, exist_ok=True)
    csv_path = DEFAULT_RESULTS / f"{output_prefix}.csv"
    json_path = DEFAULT_RESULTS / f"{output_prefix}.json"
    md_path = DEFAULT_RESULTS / f"{output_prefix}.md"

    csv_fields = [
        "sample_id",
        "duration_sec",
        "ok",
        "endpoint",
        "chunk_ms",
        "pace",
        "chunks",
        "first_text_ms",
        "first_definite_ms",
        "final_latency_ms",
        "total_wall_ms",
        "text_events",
        "revision_count",
        "avg_prefix_instability",
        "cer_norm",
        "final_chars",
        "errors",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            safe = public_row(row)
            writer.writerow({k: safe.get(k, "") for k in csv_fields})
    public_rows = [public_row(row) for row in rows]
    json_path.write_text(json.dumps(public_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    ok_rows = [r for r in rows if r.get("ok")]
    def avg(field: str) -> float:
        vals = [float(r[field]) for r in ok_rows if r.get(field) != ""]
        return sum(vals) / len(vals) if vals else 0.0

    lines = [
        "# Volcengine/Doubao streaming benchmark",
        "",
        f"Samples: {len(rows)}",
        f"OK: {len(ok_rows)}/{len(rows)}",
        "",
        "| Metric | Average |",
        "| --- | ---: |",
        f"| first_text_ms | {avg('first_text_ms'):.1f} |",
        f"| first_definite_ms | {avg('first_definite_ms'):.1f} |",
        f"| final_latency_ms | {avg('final_latency_ms'):.1f} |",
        f"| total_wall_ms | {avg('total_wall_ms'):.1f} |",
        f"| revision_count | {avg('revision_count'):.1f} |",
        f"| avg_prefix_instability | {avg('avg_prefix_instability'):.3f} |",
        f"| CER | {avg('cer_norm'):.3f} |",
        "",
        "| sample | dur s | first text ms | first definite ms | final latency ms | total wall ms | revisions | instability | CER | final chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        safe = public_row(row)
        lines.append(
            f"| {safe['sample_id']} | {safe['duration_sec']} | {safe['first_text_ms']} | "
            f"{safe['first_definite_ms']} | {safe['final_latency_ms']} | {safe['total_wall_ms']} | "
            f"{safe['revision_count']} | {safe['avg_prefix_instability']} | {safe['cer_norm']} | {safe['final_chars']} |"
        )
    lines += [
        "",
        "Definitions:",
        "- `first_text_ms`: time from stream start to first non-empty ASR text.",
        "- `final_latency_ms`: time from final audio packet sent to final response received.",
        "- `avg_prefix_instability`: average non-prefix rewrite ratio between consecutive text events; lower is more stable.",
        "- This is paced 200ms chunk streaming when `pace=realtime`.",
        "- Public CSV/JSON/Markdown omit ASR body text and server payload bodies; only aggregate metrics and character counts are written.",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--audio", type=Path, action="append")
    parser.add_argument("--endpoint", default="async")
    parser.add_argument("--chunk-ms", type=int, default=200)
    parser.add_argument("--pace", choices=["realtime", "fast"], default="realtime")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-prefix", default=time.strftime("m2-volcengine-stream-%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    if args.audio:
        pairs = [(p.stem, p, p.with_suffix(".txt")) for p in args.audio]
    else:
        pairs = sample_pairs(args.raw_dir)
    rows: list[dict[str, Any]] = []
    for sample_id, audio, ref_path in pairs:
        print(f"STREAM {sample_id} {args.endpoint} chunk={args.chunk_ms}ms pace={args.pace}", flush=True)
        row = run_stream_sample(
            args.secrets,
            audio,
            endpoint_name=args.endpoint,
            chunk_ms=args.chunk_ms,
            pace=args.pace,
            timeout=args.timeout,
        )
        ref = ref_path.read_text(encoding="utf-8").strip() if ref_path.exists() else ""
        row["sample_id"] = sample_id
        row["duration_sec"] = duration_sec(audio)
        row["cer_norm"] = round(cer(ref, row["final_text"]), 4) if ref else ""
        rows.append(row)

    csv_path, json_path, md_path = write_outputs(rows, args.output_prefix)
    print(f"CSV={csv_path}")
    print(f"JSON={json_path}")
    print(f"MD={md_path}")
    print(md_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
