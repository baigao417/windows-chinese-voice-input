from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Typeless/plain word list into dictionary.local.jsonl."
    )
    parser.add_argument("--input", required=True, help="Input txt/json/jsonl dictionary path")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument(
        "--notes",
        default="Imported from Typeless/plain dictionary",
        help="Notes field to attach to imported entries",
    )
    return parser.parse_args()


def read_entries(path: Path, notes: str) -> list[dict[str, object]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
    else:
        rows = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]

    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            phrase = row.strip()
            aliases: list[str] = []
            pronunciation = ""
            row_notes = notes
        elif isinstance(row, dict):
            phrase = str(row.get("phrase") or row.get("word") or row.get("term") or "").strip()
            raw_aliases = row.get("aliases") or []
            aliases = (
                [item.strip() for item in raw_aliases.split(",") if item.strip()]
                if isinstance(raw_aliases, str)
                else [str(item).strip() for item in raw_aliases if str(item).strip()]
            )
            pronunciation = str(row.get("pronunciation") or "").strip()
            row_notes = str(row.get("notes") or row.get("note") or notes).strip()
        else:
            continue
        if not phrase or phrase.startswith("#"):
            continue
        key = phrase.lower()
        if key in seen:
            continue
        entries.append(
            {
                "phrase": phrase,
                "aliases": aliases,
                "pronunciation": pronunciation,
                "notes": row_notes,
            }
        )
        seen.add(key)
    return entries


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    entries = read_entries(input_path, args.notes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    print(f"Imported {len(entries)} entries -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
