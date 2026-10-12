from __future__ import annotations

import argparse
import gzip
import json
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any



ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / "secrets.local.json"
DEFAULT_RAW = ROOT.parent / "examples" / "audio"
DEFAULT_RESULTS = ROOT / "results"

ENDPOINTS = {
    "bigmodel": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    "nostream": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
    "async": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_set(value: Any) -> bool:
    return isinstance(value, str) and value.strip() and "填到" not in value and "不要填" not in value


def choose_audio(raw_dir: Path) -> Path:
    audios = sorted(
        [p for p in raw_dir.iterdir() if p.suffix.lower() in {".wav", ".m4a", ".mp3", ".ogg", ".flac"}],
        key=lambda p: p.stat().st_size,
    )
    if not audios:
        raise SystemExit(f"No audio files found in {raw_dir}")
    return audios[0]


def make_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    # Protocol v1, header size = 4 bytes.
    return bytes([0x11, (message_type << 4) | flags, (serialization << 4) | compression, 0x00])


def frame_payload(header: bytes, payload: bytes) -> bytes:
    return header + struct.pack(">I", len(payload)) + payload


def full_client_request(payload: dict[str, Any]) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return frame_payload(make_header(0x1, 0x0, 0x1, 0x1), body)


def audio_only_request(audio_bytes: bytes, *, last: bool) -> bytes:
    body = gzip.compress(audio_bytes)
    return frame_payload(make_header(0x2, 0x2 if last else 0x0, 0x0, 0x1), body)


def parse_server_message(message: bytes | str) -> dict[str, Any]:
    if isinstance(message, str):
        return {"message_type": "text", "payload": message, "text": message}
    if len(message) < 8:
        return {"message_type": "unknown", "error": f"short frame: {len(message)} bytes"}

    version = message[0] >> 4
    header_size = (message[0] & 0x0F) * 4
    message_type = message[1] >> 4
    flags = message[1] & 0x0F
    serialization = message[2] >> 4
    compression = message[2] & 0x0F
    offset = header_size

    parsed: dict[str, Any] = {
        "version": version,
        "header_size": header_size,
        "message_type": message_type,
        "flags": flags,
        "serialization": serialization,
        "compression": compression,
    }

    if message_type == 0xF:
        if len(message) < offset + 8:
            parsed["error"] = "short error frame"
            return parsed
        parsed["error_code"] = struct.unpack(">I", message[offset : offset + 4])[0]
        offset += 4
        size = struct.unpack(">I", message[offset : offset + 4])[0]
        offset += 4
        parsed["payload_size"] = size
        payload = message[offset : offset + size]
        try:
            parsed["payload"] = payload.decode("utf-8")
        except UnicodeDecodeError:
            parsed["payload_hex"] = payload.hex()
        return parsed

    if flags in (0x1, 0x3):
        if len(message) < offset + 4:
            parsed["error"] = "short sequence frame"
            return parsed
        parsed["sequence"] = struct.unpack(">i", message[offset : offset + 4])[0]
        offset += 4

    if len(message) < offset + 4:
        parsed["error"] = "short payload-size frame"
        return parsed
    size = struct.unpack(">I", message[offset : offset + 4])[0]
    offset += 4
    parsed["payload_size"] = size
    payload = message[offset : offset + size]

    if compression == 0x1:
        try:
            payload = gzip.decompress(payload)
        except Exception as e:  # noqa: BLE001 - keep diagnostic in result file
            parsed["decompress_error"] = str(e)
            parsed["payload_hex"] = payload[:128].hex()
            return parsed

    if serialization == 0x1:
        try:
            parsed["payload"] = json.loads(payload.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            parsed["decode_error"] = str(e)
            parsed["payload_text"] = payload[:500].decode("utf-8", errors="replace")
    else:
        parsed["payload_bytes"] = len(payload)
    return parsed


def extract_text(payload: Any) -> str:
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            return result["text"]
        if isinstance(result, list):
            parts = [item.get("text", "") for item in result if isinstance(item, dict)]
            return "".join(parts)
        if isinstance(payload.get("text"), str):
            return payload["text"]
    return ""


def convert_to_wav_16k_mono(input_path: Path, output_path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            str(output_path),
        ],
        check=True,
    )


def run_smoke(
    secrets_path: Path,
    raw_dir: Path,
    audio_path: Path | None,
    endpoint_name: str,
    timeout: float,
) -> dict[str, Any]:
    import websocket  # websocket-client; not needed for offline helpers / --help.

    secrets = load_json(secrets_path)
    cfg = secrets.get("volcengine_doubao_asr", {})
    api_key = cfg.get("api_key")
    app_key = cfg.get("app_key")
    access_key = cfg.get("access_key")
    resource_id = cfg.get("resource_id") or "volc.bigasr.sauc.duration"
    if not is_set(resource_id):
        resource_id = "volc.bigasr.sauc.duration"
    if not is_set(api_key) and not (is_set(app_key) and is_set(access_key)):
        raise SystemExit("volcengine_doubao_asr credentials are missing")

    source_audio = audio_path or choose_audio(raw_dir)
    endpoint = ENDPOINTS.get(endpoint_name, endpoint_name)
    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())
    headers = [
        f"X-Api-Resource-Id: {resource_id}",
        f"X-Api-Request-Id: {request_id}",
        f"X-Api-Connect-Id: {connect_id}",
        "X-Api-Sequence: -1",
    ]
    if is_set(api_key):
        headers.append(f"X-Api-Key: {api_key}")
        auth_mode = "api_key"
    else:
        headers.append(f"X-Api-App-Key: {app_key}")
        headers.append(f"X-Api-Access-Key: {access_key}")
        auth_mode = "app_access_key"

    with tempfile.TemporaryDirectory(prefix="codex-volc-asr-") as tmp:
        wav_path = Path(tmp) / "input.wav"
        convert_to_wav_16k_mono(source_audio, wav_path)
        wav_bytes = wav_path.read_bytes()

        request_payload = {
            "user": {"uid": "codex-smoke"},
            "audio": {
                "format": "wav",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN",
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": False,
                "show_utterances": True,
            },
        }

        start = time.perf_counter()
        messages: list[dict[str, Any]] = []
        text = ""
        logid = ""
        try:
            ws = websocket.create_connection(
                endpoint,
                header=headers,
                timeout=timeout,
                enable_multithread=True,
            )
            response_headers = getattr(ws, "headers", {}) or {}
            if isinstance(response_headers, dict):
                logid = response_headers.get("x-tt-logid") or response_headers.get("X-Tt-Logid") or ""

            ws.send_binary(full_client_request(request_payload))
            first = parse_server_message(ws.recv())
            messages.append(first)

            ws.send_binary(audio_only_request(wav_bytes, last=True))
            for _ in range(10):
                msg = parse_server_message(ws.recv())
                messages.append(msg)
                payload_text = extract_text(msg.get("payload"))
                if payload_text:
                    text = payload_text
                if msg.get("message_type") == 0xF:
                    break
                if msg.get("flags") == 0x3:
                    break
            ws.close()
            elapsed_ms = (time.perf_counter() - start) * 1000
            ok = bool(text)
            status = "ok" if ok else "no_text"
            err = ""
        except Exception as e:  # noqa: BLE001 - result file should contain actionable failure
            elapsed_ms = (time.perf_counter() - start) * 1000
            ok = False
            status = "error"
            err = f"{type(e).__name__}: {e}"

    return {
        "provider": "volcengine_doubao_asr",
        "endpoint": endpoint_name,
        "endpoint_url": endpoint,
        "resource_id": resource_id,
        "auth_mode": auth_mode,
        "request_id": request_id,
        "connect_id": connect_id,
        "logid": logid,
        "audio": str(source_audio),
        "ok": ok,
        "status": status,
        "elapsed_ms": round(elapsed_ms, 2),
        "text": text,
        "text_preview": text[:120],
        "chars": len(text),
        "error": err,
        "messages": messages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--endpoint", default="nostream", help="bigmodel, nostream, async, or full wss URL")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = run_smoke(args.secrets, args.raw_dir, args.audio, args.endpoint, args.timeout)
    DEFAULT_RESULTS.mkdir(parents=True, exist_ok=True)
    output = args.output or DEFAULT_RESULTS / f"volcengine-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": result["provider"],
                "endpoint": result["endpoint"],
                "ok": result["ok"],
                "status": result["status"],
                "elapsed_ms": result["elapsed_ms"],
                "chars": result["chars"],
                "text_preview": result["text_preview"],
                "error": result["error"][:300],
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
