from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "windows-typeless-voice-input" / "logs" / "bad-feedback.jsonl"
DEFAULT_RESULTS = PROJECT_ROOT / "benchmarks" / "results"
FORBIDDEN_TEXT_KEYS = {"text", "raw_transcript", "transcript", "last_result"}


def read_feedback_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        raise FileNotFoundError(f"bad feedback log not found: {path}")
    rows: list[dict[str, Any]] = []
    forbidden_key_rows = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{lineno}: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"invalid JSONL at {path}:{lineno}: expected object")
        if FORBIDDEN_TEXT_KEYS.intersection(obj):
            forbidden_key_rows += 1
        rows.append(obj)
    if not rows:
        raise ValueError(f"bad feedback log is empty: {path}")
    return rows, forbidden_key_rows


def counter_records(counter: Counter[str]) -> list[dict[str, Any]]:
    total = sum(counter.values())
    return [
        {
            "name": name,
            "count": count,
            "share": round(count / total, 4) if total else None,
        }
        for name, count in counter.most_common()
    ]


def summarize(rows: list[dict[str, Any]], forbidden_key_rows: int) -> dict[str, Any]:
    category_counter = Counter(str(row.get("category") or row.get("category_slug") or "unknown") for row in rows)
    slug_counter = Counter(str(row.get("category_slug") or "unknown") for row in rows)
    mode_counter = Counter(str(row.get("mode") or "unknown") for row in rows)
    source_counter = Counter(str(row.get("source") or "unknown") for row in rows)
    rewrite_degraded = sum(1 for row in rows if row.get("rewrite_degraded") is True)
    recent = [
        {
            "created_at": row.get("created_at"),
            "category": row.get("category"),
            "category_slug": row.get("category_slug"),
            "mode": row.get("mode"),
            "chars": row.get("chars"),
            "raw_chars": row.get("raw_chars"),
            "case_path": row.get("case_path"),
        }
        for row in rows[-10:]
    ]
    return {
        "runs": len(rows),
        "forbidden_key_rows": forbidden_key_rows,
        "category_counts": counter_records(category_counter),
        "category_slug_counts": counter_records(slug_counter),
        "mode_counts": counter_records(mode_counter),
        "source_counts": counter_records(source_counter),
        "rewrite_degraded": rewrite_degraded,
        "recent": recent,
    }


def markdown_report(summary: dict[str, Any], input_path: Path, json_path: Path) -> str:
    lines = [
        "# Bad feedback summary",
        "",
        "## Verdict",
        "",
        (
            "PASS: index contains metadata only."
            if summary["forbidden_key_rows"] == 0
            else f"FAIL: {summary['forbidden_key_rows']} row(s) contain forbidden body keys."
        ),
        "",
        "## Inputs",
        "",
        f"- input: `{input_path}`",
        f"- json: `{json_path}`",
        "",
        "## Totals",
        "",
        f"- bad cases: `{summary['runs']}`",
        f"- forbidden body-key rows: `{summary['forbidden_key_rows']}`",
        f"- rewrite degraded: `{summary['rewrite_degraded']}`",
        "",
        "## Category counts",
        "",
        "| category | count | share |",
        "| --- | ---: | ---: |",
    ]
    for item in summary["category_counts"]:
        share = "" if item["share"] is None else f"{item['share']:.1%}"
        lines.append(f"| {item['name']} | {item['count']} | {share} |")
    lines.extend(
        [
            "",
            "## Mode counts",
            "",
            "| mode | count | share |",
            "| --- | ---: | ---: |",
        ]
    )
    for item in summary["mode_counts"]:
        share = "" if item["share"] is None else f"{item['share']:.1%}"
        lines.append(f"| {item['name']} | {item['count']} | {share} |")
    lines.extend(
        [
            "",
            "## Recent cases",
            "",
            "| created_at | category | mode | chars | raw_chars | case_path |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in summary["recent"]:
        lines.append(
            "| {created_at} | {category} | {mode} | {chars} | {raw_chars} | `{case_path}` |".format(
                created_at=item.get("created_at") or "",
                category=item.get("category") or "",
                mode=item.get("mode") or "",
                chars=item.get("chars") if item.get("chars") is not None else "",
                raw_chars=item.get("raw_chars") if item.get("raw_chars") is not None else "",
                case_path=item.get("case_path") or "",
            )
        )
    lines.extend(
        [
            "",
            "## Next use",
            "",
            "- If `ASR 听错` dominates, compare ASR providers or add hotword support.",
            "- If `专有名词` dominates, expand `dictionary.local.jsonl` / Rime dictionary sync.",
            "- If `结构化失败` or `口头禅多` dominates, tune rewrite prompt/model.",
            "- If `标点差` dominates, tune punctuation-specific post-processing.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize local bad-feedback metadata")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_RESULTS / "bad-feedback-summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rows, forbidden_key_rows = read_feedback_rows(args.input)
    summary = summarize(rows, forbidden_key_rows)
    output_prefix = args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_report(summary, args.input, json_path), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
