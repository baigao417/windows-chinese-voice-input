from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "windows-typeless-voice-input" / "logs" / "usage.jsonl"
DEFAULT_RESULTS = PROJECT_ROOT / "benchmarks" / "results"
FORBIDDEN_TEXT_KEYS = {"text", "raw_transcript", "transcript"}


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def round_or_none(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[int(pos)]
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def metric_summary(values: list[float], digits: int = 1) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "avg": round(statistics.fmean(values), digits),
        "p50": round(percentile(values, 0.50) or 0, digits),
        "p95": round(percentile(values, 0.95) or 0, digits),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
    }


def read_usage_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        raise FileNotFoundError(f"usage log not found: {path}")
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
        raise ValueError(f"usage log is empty: {path}")
    return rows, forbidden_key_rows


def collect_numbers(rows: list[dict[str, Any]], field: str) -> list[float]:
    return [v for row in rows if (v := as_float(row.get(field))) is not None]


def bool_count(rows: list[dict[str, Any]], field: str, expected: bool) -> int:
    return sum(1 for row in rows if as_bool(row.get(field)) is expected)


def row_token_usage(row: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    usage = row.get("llm_usage")
    if not isinstance(usage, dict):
        return None, None, False
    prompt_tokens = as_float(usage.get("prompt_tokens"))
    completion_tokens = as_float(usage.get("completion_tokens"))
    return prompt_tokens, completion_tokens, prompt_tokens is not None or completion_tokens is not None


def model_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("stt_engine") or ""),
            str(row.get("stt_model") or ""),
            str(row.get("llm_provider") or ""),
            str(row.get("llm_model") or ""),
        )
        groups[key].append(row)

    out: list[dict[str, Any]] = []
    for (stt_engine, stt_model, llm_provider, llm_model), group_rows in sorted(
        groups.items(), key=lambda item: len(item[1]), reverse=True
    ):
        audio_seconds = collect_numbers(group_rows, "audio_seconds")
        total_elapsed = collect_numbers(group_rows, "total_elapsed_ms")
        rtfs = []
        for row in group_rows:
            total_ms = as_float(row.get("total_elapsed_ms"))
            audio_s = as_float(row.get("audio_seconds"))
            if total_ms is not None and audio_s is not None and audio_s > 0:
                rtfs.append(total_ms / 1000 / audio_s)
        out.append(
            {
                "stt_engine": stt_engine,
                "stt_model": stt_model,
                "llm_provider": llm_provider,
                "llm_model": llm_model,
                "runs": len(group_rows),
                "avg_audio_seconds": round_or_none(
                    statistics.fmean(audio_seconds) if audio_seconds else None,
                    3,
                ),
                "avg_total_elapsed_ms": round_or_none(
                    statistics.fmean(total_elapsed) if total_elapsed else None,
                    1,
                ),
                "avg_wall_rtf": round_or_none(statistics.fmean(rtfs) if rtfs else None, 3),
                "rewrite_degraded": bool_count(group_rows, "rewrite_degraded", True),
            }
        )
    return out


def estimate_cost(
    rows: list[dict[str, Any]],
    *,
    asr_price_per_hour: float | None,
    rewrite_price_per_run: float | None,
    rewrite_input_price_per_mtoken: float | None,
    rewrite_output_price_per_mtoken: float | None,
    chars_per_token: float,
    include_char_token_estimates: bool,
    typeless_monthly_price: float | None,
    currency: str,
    typeless_currency: str | None,
) -> dict[str, Any] | None:
    if (
        asr_price_per_hour is None
        and rewrite_price_per_run is None
        and rewrite_input_price_per_mtoken is None
        and rewrite_output_price_per_mtoken is None
        and typeless_monthly_price is None
    ):
        return None

    audio_seconds = collect_numbers(rows, "audio_seconds")
    raw_chars = collect_numbers(rows, "raw_chars")
    chars = collect_numbers(rows, "chars")
    total_audio_hours = sum(audio_seconds) / 3600 if audio_seconds else 0.0
    asr_cost = total_audio_hours * asr_price_per_hour if asr_price_per_hour is not None else 0.0
    rewrite_run_cost = len(rows) * rewrite_price_per_run if rewrite_price_per_run is not None else 0.0
    estimated_input_tokens = 0.0
    estimated_output_tokens = 0.0
    excluded_char_estimated_input_tokens = 0.0
    excluded_char_estimated_output_tokens = 0.0
    token_cost_audio_seconds = 0.0
    rows_with_provider_token_usage = 0
    rows_estimated_from_chars = 0
    rows_excluded_from_token_cost = 0
    token_pricing_enabled = (
        rewrite_input_price_per_mtoken is not None or rewrite_output_price_per_mtoken is not None
    )
    for row in rows:
        prompt_tokens, completion_tokens, has_provider_usage = row_token_usage(row)
        if has_provider_usage:
            rows_with_provider_token_usage += 1
            estimated_input_tokens += prompt_tokens or 0.0
            estimated_output_tokens += completion_tokens or 0.0
            token_cost_audio_seconds += as_float(row.get("audio_seconds")) or 0.0
        else:
            char_input_tokens = (as_float(row.get("raw_chars")) or 0.0) / chars_per_token
            char_output_tokens = (as_float(row.get("chars")) or 0.0) / chars_per_token
            if include_char_token_estimates:
                rows_estimated_from_chars += 1
                estimated_input_tokens += char_input_tokens
                estimated_output_tokens += char_output_tokens
                token_cost_audio_seconds += as_float(row.get("audio_seconds")) or 0.0
            else:
                rows_excluded_from_token_cost += 1
                excluded_char_estimated_input_tokens += char_input_tokens
                excluded_char_estimated_output_tokens += char_output_tokens
    rewrite_input_cost = (
        estimated_input_tokens / 1_000_000 * rewrite_input_price_per_mtoken
        if rewrite_input_price_per_mtoken is not None
        else 0.0
    )
    rewrite_output_cost = (
        estimated_output_tokens / 1_000_000 * rewrite_output_price_per_mtoken
        if rewrite_output_price_per_mtoken is not None
        else 0.0
    )
    rewrite_cost = rewrite_run_cost + rewrite_input_cost + rewrite_output_cost
    observed_cost = asr_cost + rewrite_cost
    cost_covered_audio_hours = total_audio_hours
    if token_pricing_enabled and not include_char_token_estimates and rows_excluded_from_token_cost > 0:
        cost_covered_audio_hours = token_cost_audio_seconds / 3600 if token_cost_audio_seconds > 0 else 0.0
    observed_cost_per_audio_hour = (
        observed_cost / cost_covered_audio_hours if cost_covered_audio_hours > 0 else None
    )
    break_even_audio_hours = (
        typeless_monthly_price / observed_cost_per_audio_hour
        if typeless_monthly_price is not None
        and observed_cost_per_audio_hour is not None
        and observed_cost_per_audio_hour > 0
        else None
    )
    return {
        "currency": currency,
        "pricing_source": "user_supplied_flags",
        "input_prices": {
            "asr_price_per_audio_hour": asr_price_per_hour,
            "rewrite_price_per_run": rewrite_price_per_run,
            "rewrite_input_price_per_mtoken": rewrite_input_price_per_mtoken,
            "rewrite_output_price_per_mtoken": rewrite_output_price_per_mtoken,
            "chars_per_token": chars_per_token,
            "include_char_token_estimates": include_char_token_estimates,
            "typeless_monthly_price": typeless_monthly_price,
            "typeless_currency": typeless_currency or currency,
        },
        "total_audio_hours": round(total_audio_hours, 4),
        "cost_covered_audio_hours": round(cost_covered_audio_hours, 4),
        "estimated_input_tokens": round(estimated_input_tokens, 1),
        "estimated_output_tokens": round(estimated_output_tokens, 1),
        "excluded_char_estimated_input_tokens": round(excluded_char_estimated_input_tokens, 1),
        "excluded_char_estimated_output_tokens": round(excluded_char_estimated_output_tokens, 1),
        "rows_with_provider_token_usage": rows_with_provider_token_usage,
        "rows_estimated_from_chars": rows_estimated_from_chars,
        "rows_excluded_from_token_cost": rows_excluded_from_token_cost,
        "token_cost_coverage": (
            "provider_usage_plus_explicit_char_estimates"
            if include_char_token_estimates and token_pricing_enabled
            else "provider_usage_only"
            if token_pricing_enabled
            else "not_applicable"
        ),
        "estimated_asr_cost": round(asr_cost, 4),
        "estimated_rewrite_run_cost": round(rewrite_run_cost, 4),
        "estimated_rewrite_input_cost": round(rewrite_input_cost, 4),
        "estimated_rewrite_output_cost": round(rewrite_output_cost, 4),
        "estimated_rewrite_cost": round(rewrite_cost, 4),
        "estimated_total_cost": round(observed_cost, 4),
        "estimated_cost_per_audio_hour": round_or_none(observed_cost_per_audio_hour, 4),
        "typeless_break_even_audio_hours_per_month": round_or_none(break_even_audio_hours, 2),
        "note": "Cost is only an estimate from caller-supplied prices. Token-priced rewrite costs default to provider-reported token usage only; rows without provider usage are excluded from token cost unless --include-char-token-estimates is set. Verify with provider bills before relying on it.",
    }


def summarize(rows: list[dict[str, Any]], forbidden_key_rows: int, args: argparse.Namespace) -> dict[str, Any]:
    audio_seconds = collect_numbers(rows, "audio_seconds")
    stt_elapsed = collect_numbers(rows, "stt_elapsed_ms")
    rewrite_elapsed = collect_numbers(rows, "rewrite_elapsed_ms")
    paste_elapsed = collect_numbers(rows, "paste_elapsed_ms")
    total_elapsed = collect_numbers(rows, "total_elapsed_ms")
    raw_chars = collect_numbers(rows, "raw_chars")
    chars = collect_numbers(rows, "chars")

    rtfs = []
    for row in rows:
        total_ms = as_float(row.get("total_elapsed_ms"))
        audio_s = as_float(row.get("audio_seconds"))
        if total_ms is not None and audio_s is not None and audio_s > 0:
            rtfs.append(total_ms / 1000 / audio_s)

    rewrite_degraded = bool_count(rows, "rewrite_degraded", True)
    copied_true = bool_count(rows, "copied", True)
    pasted_true = bool_count(rows, "pasted", True)

    cost = estimate_cost(
        rows,
        asr_price_per_hour=args.asr_price_per_hour,
        rewrite_price_per_run=args.rewrite_price_per_run,
        rewrite_input_price_per_mtoken=args.rewrite_input_price_per_mtoken,
        rewrite_output_price_per_mtoken=args.rewrite_output_price_per_mtoken,
        chars_per_token=args.chars_per_token,
        include_char_token_estimates=args.include_char_token_estimates,
        typeless_monthly_price=args.typeless_monthly_price,
        currency=args.currency,
        typeless_currency=args.typeless_currency,
    )

    return {
        "source": str(args.input),
        "runs": len(rows),
        "forbidden_text_key_rows": forbidden_key_rows,
        "audio": {
            "total_seconds": round_or_none(sum(audio_seconds), 3) if audio_seconds else None,
            "avg_seconds": round_or_none(statistics.fmean(audio_seconds), 3) if audio_seconds else None,
            "count_with_audio_seconds": len(audio_seconds),
        },
        "latency_ms": {
            "stt": metric_summary(stt_elapsed),
            "rewrite": metric_summary(rewrite_elapsed),
            "paste": metric_summary(paste_elapsed),
            "total": metric_summary(total_elapsed),
        },
        "wall_rtf": metric_summary(rtfs, digits=3),
        "chars": {
            "avg_raw_chars": round_or_none(statistics.fmean(raw_chars), 1) if raw_chars else None,
            "avg_output_chars": round_or_none(statistics.fmean(chars), 1) if chars else None,
            "total_raw_chars": int(sum(raw_chars)) if raw_chars else 0,
            "total_output_chars": int(sum(chars)) if chars else 0,
        },
        "quality_safety": {
            "rewrite_degraded": rewrite_degraded,
            "rewrite_degraded_rate": round(rewrite_degraded / len(rows), 4),
        },
        "paste": {
            "copied_true": copied_true,
            "pasted_true": pasted_true,
            "copied_rate": round(copied_true / len(rows), 4),
            "pasted_rate": round(pasted_true / len(rows), 4),
        },
        "models": model_breakdown(rows),
        "cost": cost,
    }


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value}{suffix}"


def render_markdown(summary: dict[str, Any]) -> str:
    latency = summary["latency_ms"]
    audio = summary["audio"]
    chars = summary["chars"]
    safety = summary["quality_safety"]
    paste = summary["paste"]
    wall_rtf = summary["wall_rtf"]

    lines = [
        "# Local voice input usage scorecard",
        "",
        f"Source: `{summary['source']}`",
        "",
        "This report is generated from private usage metadata only. It must not contain dictated text.",
        "",
        "## Summary",
        "",
        f"- Runs: {summary['runs']}",
        f"- Audio: total {fmt(audio['total_seconds'], 's')}; avg {fmt(audio['avg_seconds'], 's')}; rows with audio duration {audio['count_with_audio_seconds']}",
        f"- Avg wall RTF: {fmt(wall_rtf['avg'])}",
        f"- Rewrite degraded: {safety['rewrite_degraded']} ({safety['rewrite_degraded_rate']:.1%})",
        f"- Copied: {paste['copied_true']} ({paste['copied_rate']:.1%}); pasted: {paste['pasted_true']} ({paste['pasted_rate']:.1%})",
        f"- Forbidden text-key rows detected: {summary['forbidden_text_key_rows']}",
        "",
        "## Latency",
        "",
        "| stage | avg ms | p50 ms | p95 ms | min ms | max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for stage in ["stt", "rewrite", "paste", "total"]:
        m = latency[stage]
        lines.append(
            f"| {stage} | {fmt(m['avg'])} | {fmt(m['p50'])} | {fmt(m['p95'])} | {fmt(m['min'])} | {fmt(m['max'])} |"
        )

    lines.extend(
        [
            "",
            "## Character counts",
            "",
            f"- Total raw chars: {chars['total_raw_chars']}",
            f"- Total output chars: {chars['total_output_chars']}",
            f"- Avg raw chars: {fmt(chars['avg_raw_chars'])}",
            f"- Avg output chars: {fmt(chars['avg_output_chars'])}",
            "",
            "## Model breakdown",
            "",
            "| runs | STT | LLM | avg audio s | avg total ms | avg RTF | degraded |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary["models"]:
        stt = " / ".join(x for x in [row["stt_engine"], row["stt_model"]] if x)
        llm = " / ".join(x for x in [row["llm_provider"], row["llm_model"]] if x)
        lines.append(
            f"| {row['runs']} | {stt or 'n/a'} | {llm or 'n/a'} | "
            f"{fmt(row['avg_audio_seconds'])} | {fmt(row['avg_total_elapsed_ms'])} | "
            f"{fmt(row['avg_wall_rtf'])} | {row['rewrite_degraded']} |"
        )

    if summary.get("cost") is not None:
        cost = summary["cost"]
        lines.extend(
            [
                "",
                "## Optional cost estimate",
                "",
                f"- Pricing source: {cost['pricing_source']}",
                f"- Currency: {cost['currency']}",
                f"- Estimated ASR cost: {cost['estimated_asr_cost']}",
                f"- Cost-covered audio hours: {cost['cost_covered_audio_hours']} (total observed: {cost['total_audio_hours']})",
                f"- Estimated input/output tokens: {cost['estimated_input_tokens']} / {cost['estimated_output_tokens']}",
                f"- Token cost coverage: {cost['token_cost_coverage']}",
                f"- Token source rows: provider usage {cost['rows_with_provider_token_usage']}; char estimate included {cost['rows_estimated_from_chars']}; char estimate excluded {cost['rows_excluded_from_token_cost']}",
                f"- Excluded char-estimated input/output tokens: {cost['excluded_char_estimated_input_tokens']} / {cost['excluded_char_estimated_output_tokens']}",
                f"- Estimated rewrite run cost: {cost['estimated_rewrite_run_cost']}",
                f"- Estimated rewrite input cost: {cost['estimated_rewrite_input_cost']}",
                f"- Estimated rewrite output cost: {cost['estimated_rewrite_output_cost']}",
                f"- Estimated rewrite cost: {cost['estimated_rewrite_cost']}",
                f"- Estimated total cost: {cost['estimated_total_cost']}",
                f"- Estimated cost per audio hour: {fmt(cost['estimated_cost_per_audio_hour'])}",
                f"- Typeless monthly break-even audio hours: {fmt(cost['typeless_break_even_audio_hours_per_month'])}",
                f"- Note: {cost['note']}",
            ]
        )

    lines.extend(
        [
            "",
            "## Privacy check",
            "",
            "- This scorecard only aggregates metadata fields.",
            "- It does not render any dictated text fields.",
            "- If `forbidden_text_key_rows` is greater than 0, inspect the private JSONL locally and fix the logger before using the data for sharing.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Windows voice-input usage metadata.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DEFAULT_RESULTS / "usage-scorecard",
        help="Output path prefix; .json and .md will be appended.",
    )
    parser.add_argument("--asr-price-per-hour", type=float, default=None)
    parser.add_argument("--rewrite-price-per-run", type=float, default=None)
    parser.add_argument("--rewrite-input-price-per-mtoken", type=float, default=None)
    parser.add_argument("--rewrite-output-price-per-mtoken", type=float, default=None)
    parser.add_argument(
        "--chars-per-token",
        type=float,
        default=1.0,
        help="Approximation for explicit char-token estimates; ignored for token-priced rows unless --include-char-token-estimates is set.",
    )
    parser.add_argument(
        "--include-char-token-estimates",
        action="store_true",
        help="Include legacy rows without provider token usage by estimating tokens from character counts. Disabled by default because it misses prompt/dictionary input tokens.",
    )
    parser.add_argument("--typeless-monthly-price", type=float, default=None)
    parser.add_argument(
        "--typeless-currency",
        default=None,
        help="Currency of --typeless-monthly-price; must match --currency when provided.",
    )
    parser.add_argument("--currency", default="CNY")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.chars_per_token <= 0:
        print("ERROR: --chars-per-token must be greater than 0", file=sys.stderr)
        return 2
    if args.typeless_currency and args.typeless_currency != args.currency:
        print(
            "ERROR: --typeless-currency must match --currency; convert prices before comparing break-even",
            file=sys.stderr,
        )
        return 2
    try:
        rows, forbidden_key_rows = read_usage_rows(args.input)
        summary = summarize(rows, forbidden_key_rows, args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    output_prefix = args.output_prefix
    if output_prefix.suffix:
        output_prefix = output_prefix.with_suffix("")
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
