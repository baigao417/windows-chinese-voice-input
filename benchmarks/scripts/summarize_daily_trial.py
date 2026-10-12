from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import summarize_bad_feedback  # noqa: E402
import summarize_usage  # noqa: E402


DEFAULT_USAGE_INPUT = PROJECT_ROOT / "windows-typeless-voice-input" / "logs" / "usage.jsonl"
DEFAULT_BAD_FEEDBACK_INPUT = PROJECT_ROOT / "windows-typeless-voice-input" / "logs" / "bad-feedback.jsonl"
DEFAULT_RESULTS = PROJECT_ROOT / "benchmarks" / "results"


def empty_bad_feedback_summary() -> dict[str, Any]:
    return {
        "runs": 0,
        "forbidden_key_rows": 0,
        "category_counts": [],
        "category_slug_counts": [],
        "mode_counts": [],
        "source_counts": [],
        "rewrite_degraded": 0,
        "recent": [],
    }


def usage_args(input_path: Path, args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        input=input_path,
        asr_price_per_hour=args.asr_price_per_hour,
        rewrite_price_per_run=args.rewrite_price_per_run,
        rewrite_input_price_per_mtoken=args.rewrite_input_price_per_mtoken,
        rewrite_output_price_per_mtoken=args.rewrite_output_price_per_mtoken,
        chars_per_token=args.chars_per_token,
        include_char_token_estimates=args.include_char_token_estimates,
        typeless_monthly_price=args.typeless_monthly_price,
        typeless_currency=args.typeless_currency,
        currency=args.currency,
    )


def load_usage_summary(path: Path, args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    try:
        rows, forbidden_key_rows = summarize_usage.read_usage_rows(path)
        return summarize_usage.summarize(rows, forbidden_key_rows, usage_args(path, args)), None
    except (FileNotFoundError, ValueError) as exc:
        return None, str(exc)


def load_bad_feedback_summary(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        rows, forbidden_key_rows = summarize_bad_feedback.read_feedback_rows(path)
        return summarize_bad_feedback.summarize(rows, forbidden_key_rows), None
    except FileNotFoundError:
        return empty_bad_feedback_summary(), f"bad feedback log not found: {path}"
    except ValueError as exc:
        if "is empty" in str(exc):
            return empty_bad_feedback_summary(), str(exc)
        return empty_bad_feedback_summary(), str(exc)


def top_category(bad_feedback: dict[str, Any]) -> dict[str, Any] | None:
    categories = bad_feedback.get("category_counts") or []
    return categories[0] if categories else None


def recommendation_lines(
    usage: dict[str, Any] | None,
    usage_error: str | None,
    bad_feedback: dict[str, Any],
    bad_feedback_note: str | None,
    *,
    max_bad_rate: float,
    max_p95_total_ms: float,
) -> list[str]:
    lines: list[str] = []
    if usage is None:
        lines.append("- P1: usage metadata is missing. Start `--daily` and collect at least 10 real runs.")
        if usage_error:
            lines.append(f"  - evidence: `{usage_error}`")
        return lines

    runs = int(usage.get("runs") or 0)
    bad_runs = int(bad_feedback.get("runs") or 0)
    forbidden_usage = int(usage.get("forbidden_text_key_rows") or 0)
    forbidden_bad = int(bad_feedback.get("forbidden_key_rows") or 0)
    if forbidden_usage or forbidden_bad:
        lines.append(
            f"- P0: metadata logs contain forbidden body-key rows. usage={forbidden_usage}; bad_feedback={forbidden_bad}."
        )

    if runs < 10:
        lines.append(f"- P1: sample size is still small: `{runs}` usage run(s). Keep daily mode until at least 10-20 runs.")

    bad_rate = bad_runs / runs if runs else 0.0
    if bad_runs:
        top = top_category(bad_feedback)
        if top:
            lines.append(
                f"- P1: top bad category is `{top['name']}` ({top['count']} case(s), {top['share']:.1%})."
            )
            name = str(top["name"])
            if "ASR" in name:
                lines.append("  - next: compare ASR providers or add hotword support.")
            elif "专有名词" in name:
                lines.append("  - next: update personal dictionary and prompt vocabulary injection.")
            elif "结构化" in name or "口头禅" in name:
                lines.append("  - next: tune rewrite prompt/model.")
            elif "标点" in name:
                lines.append("  - next: tune punctuation-specific post-processing.")
    if bad_rate > max_bad_rate:
        lines.append(f"- P1: marked-bad rate is high: {bad_rate:.1%}; do not switch to auto-paste yet.")
    elif runs >= 10:
        lines.append(f"- PASS: marked-bad rate is within threshold: {bad_rate:.1%}.")

    total_latency = usage.get("latency_ms", {}).get("total", {})
    p95_total = total_latency.get("p95")
    if isinstance(p95_total, (int, float)) and p95_total > max_p95_total_ms:
        lines.append(f"- P1: total latency p95 is high: {p95_total}ms; prioritize speed optimization.")

    safety = usage.get("quality_safety", {})
    degraded_rate = safety.get("rewrite_degraded_rate")
    if isinstance(degraded_rate, (int, float)) and degraded_rate > 0.05:
        lines.append(f"- P1: rewrite degraded rate is {degraded_rate:.1%}; inspect LLM timeout/fallback.")

    paste = usage.get("paste", {})
    pasted_true = int(paste.get("pasted_true") or 0)
    copied_true = int(paste.get("copied_true") or 0)
    if pasted_true:
        lines.append(f"- P2: auto-paste happened in `{pasted_true}` run(s); daily trial should prefer clipboard-only.")
    if runs and copied_true < runs:
        lines.append(f"- P2: copied count `{copied_true}` is lower than runs `{runs}`; inspect paste/clipboard path.")

    if bad_feedback_note and not bad_runs:
        lines.append(f"- Info: no bad-feedback rows yet. `{bad_feedback_note}`")
    if not lines:
        lines.append("- PASS: no immediate P0/P1 issue found in current metadata.")
    return lines


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    usage, usage_error = load_usage_summary(args.usage_input, args)
    bad_feedback, bad_feedback_note = load_bad_feedback_summary(args.bad_feedback_input)
    runs = int(usage.get("runs") or 0) if usage else 0
    bad_runs = int(bad_feedback.get("runs") or 0)
    bad_rate = round(bad_runs / runs, 4) if runs else None
    recommendations = recommendation_lines(
        usage,
        usage_error,
        bad_feedback,
        bad_feedback_note,
        max_bad_rate=args.max_bad_rate,
        max_p95_total_ms=args.max_p95_total_ms,
    )
    has_p0 = any(line.startswith("- P0") for line in recommendations)
    has_p1 = any(line.startswith("- P1") for line in recommendations)
    return {
        "inputs": {
            "usage": str(args.usage_input),
            "bad_feedback": str(args.bad_feedback_input),
        },
        "status": "FAIL" if has_p0 else "ATTENTION" if has_p1 else "PASS",
        "usage_error": usage_error,
        "bad_feedback_note": bad_feedback_note,
        "usage": usage,
        "bad_feedback": bad_feedback,
        "bad_rate": bad_rate,
        "thresholds": {
            "max_bad_rate": args.max_bad_rate,
            "max_p95_total_ms": args.max_p95_total_ms,
        },
        "recommendations": recommendations,
    }


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def render_markdown(summary: dict[str, Any]) -> str:
    usage = summary.get("usage")
    bad_feedback = summary["bad_feedback"]
    lines = [
        "# Daily voice input trial report",
        "",
        "This report is generated from local metadata only. It must not include dictated body text.",
        "",
        f"Status: **{summary['status']}**",
        "",
        "## Inputs",
        "",
        f"- usage: `{summary['inputs']['usage']}`",
        f"- bad feedback: `{summary['inputs']['bad_feedback']}`",
        "",
        "## Summary",
        "",
    ]
    if usage is None:
        lines.append(f"- Usage: unavailable (`{summary['usage_error']}`)")
    else:
        audio = usage["audio"]
        latency = usage["latency_ms"]
        paste = usage["paste"]
        safety = usage["quality_safety"]
        lines.extend(
            [
                f"- Runs: `{usage['runs']}`",
                f"- Audio total: `{fmt(audio['total_seconds'], 's')}`",
                f"- Avg wall RTF: `{fmt(usage['wall_rtf']['avg'])}`",
                f"- Total latency p50/p95: `{fmt(latency['total']['p50'], 'ms')}` / `{fmt(latency['total']['p95'], 'ms')}`",
                f"- Rewrite degraded: `{safety['rewrite_degraded']}` ({safety['rewrite_degraded_rate']:.1%})",
                f"- Copied / pasted: `{paste['copied_true']}` / `{paste['pasted_true']}`",
                f"- Usage body-key audit rows: `{usage['forbidden_text_key_rows']}`",
            ]
        )
    lines.extend(
        [
            f"- Marked bad: `{bad_feedback['runs']}`",
            f"- Marked-bad rate: `{fmt(summary['bad_rate'])}`",
            f"- Bad-feedback body-key audit rows: `{bad_feedback['forbidden_key_rows']}`",
            "",
            "## Recommendations",
            "",
            *summary["recommendations"],
            "",
            "## Bad category counts",
            "",
            "| category | count | share |",
            "| --- | ---: | ---: |",
        ]
    )
    for item in bad_feedback.get("category_counts") or []:
        share = "" if item.get("share") is None else f"{item['share']:.1%}"
        lines.append(f"| {item['name']} | {item['count']} | {share} |")
    if not bad_feedback.get("category_counts"):
        lines.append("| n/a | 0 |  |")
    lines.extend(["", "## Model routes", "", "| STT | LLM | runs | avg total ms | avg RTF | degraded |", "| --- | --- | ---: | ---: | ---: | ---: |"])
    if usage:
        for model in usage.get("models") or []:
            stt = f"{model.get('stt_engine')}/{model.get('stt_model')}"
            llm = f"{model.get('llm_provider')}/{model.get('llm_model')}"
            lines.append(
                f"| {stt} | {llm} | {model.get('runs')} | {fmt(model.get('avg_total_elapsed_ms'))} | {fmt(model.get('avg_wall_rtf'))} | {model.get('rewrite_degraded')} |"
            )
    else:
        lines.append("| n/a | n/a | 0 |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily metadata-only voice input trial report")
    parser.add_argument("--usage-input", type=Path, default=DEFAULT_USAGE_INPUT)
    parser.add_argument("--bad-feedback-input", type=Path, default=DEFAULT_BAD_FEEDBACK_INPUT)
    parser.add_argument("--output-prefix", type=Path, default=DEFAULT_RESULTS / "daily-trial-report")
    parser.add_argument("--max-bad-rate", type=float, default=0.10)
    parser.add_argument("--max-p95-total-ms", type=float, default=15_000)
    parser.add_argument("--asr-price-per-hour", type=float, default=None)
    parser.add_argument("--rewrite-price-per-run", type=float, default=None)
    parser.add_argument("--rewrite-input-price-per-mtoken", type=float, default=None)
    parser.add_argument("--rewrite-output-price-per-mtoken", type=float, default=None)
    parser.add_argument("--chars-per-token", type=float, default=1.0)
    parser.add_argument("--include-char-token-estimates", action="store_true")
    parser.add_argument("--typeless-monthly-price", type=float, default=None)
    parser.add_argument("--typeless-currency", default=None)
    parser.add_argument("--currency", default="CNY")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.chars_per_token <= 0:
        print("ERROR: --chars-per-token must be greater than 0", file=sys.stderr)
        return 2
    if args.typeless_currency and args.typeless_currency != args.currency:
        print("ERROR: --typeless-currency must match --currency", file=sys.stderr)
        return 2
    summary = build_summary(args)
    output_prefix = args.output_prefix.with_suffix("") if args.output_prefix.suffix else args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"status={summary['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
