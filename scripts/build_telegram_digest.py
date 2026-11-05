#!/usr/bin/env python3
"""Build the fork's setup digest from the upstream YOLO dashboard state.

The upstream dashboard is the sole source of candidates.  This script only
fetches data/dashboard_state.json, validates it, and filters/sorts the already
classified opportunities.items.  It never creates candidates from screeners
or narratives and never rebuilds upstream calculations.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_URL = (
    "https://raw.githubusercontent.com/DocYolo77/yolo-dashboard/"
    "main/data/dashboard_state.json"
)
MAX_SOURCE_AGE_DAYS = 7
MAX_TELEGRAM_LENGTH = 3800
LAGGARD_LIMIT = 10
SECTION_LIMIT = 10
LEADER_STATES = {"fresh_leader", "leader", "recent_leader"}


def fail(message: str) -> None:
    raise ValueError(message)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value: Any, digits: int = 1) -> str:
    return f"{value:.{digits}f}" if is_number(value) else "—"


def fetch_dashboard(url: str = SOURCE_URL) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "YOLO-Setup-Digest"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"Upstream dashboard_state.json could not be loaded: {exc}")
    if not isinstance(payload, dict):
        fail("Upstream dashboard_state.json must be a JSON object")
    return payload


def validate_dashboard(data: dict[str, Any], today: date | None = None) -> tuple[str, list[dict[str, Any]]]:
    meta = data.get("meta") or {}
    raw_date = meta.get("as_of_date")
    try:
        as_of = date.fromisoformat(str(raw_date)[:10])
    except (TypeError, ValueError):
        fail("dashboard_state.json: meta.as_of_date is missing or invalid")

    current_date = today or datetime.now(timezone.utc).date()
    age = (current_date - as_of).days
    if age < 0 or age > MAX_SOURCE_AGE_DAYS:
        fail(f"Upstream snapshot is future-dated or stale: {as_of} (age={age} days)")

    opportunities = data.get("opportunities")
    if not isinstance(opportunities, dict) or not isinstance(opportunities.get("items"), list):
        fail("dashboard_state.json: opportunities.items is missing or invalid")

    items = opportunities["items"]
    symbols = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str) or not item["symbol"].strip():
            fail(f"opportunities.items[{index}] has no valid ticker symbol")
        symbols.append(item["symbol"].upper())
    if len(symbols) != len(set(symbols)):
        fail("opportunities.items contains duplicate ticker symbols")

    regime_count = (data.get("market_regime") or {}).get("eligible_universe_size")
    if isinstance(regime_count, int) and regime_count != len(items):
        fail(
            "Universe validation failed: market_regime.eligible_universe_size "
            f"is {regime_count}, but opportunities.items contains {len(items)} unique tickers"
        )
    if not items:
        fail("opportunities.items is empty; refusing to send an empty or fallback digest")
    return as_of.isoformat(), items


def score(item: dict[str, Any]) -> float | None:
    """Use the dashboard's existing Structural RS (0-100), not a new score."""
    value = item.get("structural_rs")
    return float(value) if is_number(value) else None


def score_sort(items: list[dict[str, Any]], descending: bool = True) -> list[dict[str, Any]]:
    # Missing scores always sort last; ticker is a deterministic tie-break.
    return sorted(
        items,
        key=lambda item: (
            score(item) is None,
            -(score(item) or 0.0) if descending else (score(item) or 0.0),
            item["symbol"].upper(),
        ),
    )


def build_sections(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    # The candidate universe is exclusively upstream opportunities.items.
    # Narrative fields only explain the upstream classification; they never
    # introduce symbols into the candidate pool.
    opportunities = [
        item for item in items
        if item.get("quality_state") in LEADER_STATES
        or bool(item.get("constructive_reset_narratives"))
    ]
    opportunities = score_sort(opportunities)[:SECTION_LIMIT]

    # "Extended" is the upstream hysteresis-based Opportunities tab flag.
    # Extended_v2 is a separate badge and is intentionally not substituted.
    extended = score_sort([item for item in items if item.get("extended") is True])

    # A ticker is a Laggard only when the upstream Opportunity Engine marked
    # at least one laggard narrative.  Sort by weakest Structural RS first.
    laggards = score_sort(
        [item for item in items if item.get("laggard_narratives")],
        descending=False,
    )[:LAGGARD_LIMIT]
    return {"opportunities": opportunities, "extended": extended, "laggards": laggards}


def market_context_lines(data: dict[str, Any]) -> list[str]:
    regime = data.get("market_regime") or {}
    qqq = data.get("qqq_health") or {}
    regime_state = str(regime.get("state") or "UNAVAILABLE").replace("_", " ")
    qqq_state = str(qqq.get("base_state") or "UNAVAILABLE").replace("_", " ")
    lines = [f"Market Regime: {regime_state} | Score {fmt(regime.get('score'))}"]
    modifiers = qqq.get("modifiers") or []
    lines.append(f"QQQ Health: {qqq_state} | Score {fmt(qqq.get('score'))}")
    if modifiers:
        lines.append("QQQ Modifier: " + ", ".join(str(x) for x in modifiers))
    return lines


def ticker_line(item: dict[str, Any], rank: int | None = None) -> str:
    prefix = f"{rank:02d}. " if rank is not None else "• "
    symbol = item["symbol"].upper()
    quality = str(item.get("quality_state") or item.get("quality_v2") or "unknown").upper()
    values = (
        f"Structural RS {fmt(item.get('structural_rs'))}"
        f" | Trend {fmt(item.get('trend_strength'))}"
        f" | 1W/1M/3M/6M "
        f"{fmt(item.get('rs_1w'), 0)}/{fmt(item.get('rs_1m'), 0)}/"
        f"{fmt(item.get('rs_3m'), 0)}/{fmt(item.get('rs_6m'), 0)}"
        f" | ATR Ext. {fmt(item.get('atr_extension'))}"
    )
    return f"{prefix}{symbol} [{quality}] — {values}"


def change_lines(data: dict[str, Any], limit: int = 8) -> list[str]:
    events = ((data.get("what_changed") or {}).get("events") or [])
    if not isinstance(events, list) or not events:
        return ["Keine neuen Upstream-Änderungen gemeldet."]
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        category = str(event.get("category") or "event").replace("_", " ").upper()
        typ = str(event.get("type") or "change").replace("_", " ")
        symbol = event.get("symbol")
        name = event.get("narrative_name") or event.get("narrative_id")
        detail = f"{symbol}: " if symbol else (f"{name}: " if name else "")
        if event.get("from_score") is not None or event.get("to_score") is not None:
            detail += f"{fmt(event.get('from_score'))} → {fmt(event.get('to_score'))}"
        elif event.get("from_rank") is not None or event.get("to_rank") is not None:
            detail += f"Rang {event.get('from_rank', '—')} → {event.get('to_rank', '—')}"
        elif event.get("modifier"):
            detail += str(event["modifier"])
        result.append(f"• {category} | {detail}{typ}")
        if len(result) >= limit:
            break
    return result or ["Keine neuen Upstream-Änderungen gemeldet."]


def render_digest(data: dict[str, Any], as_of: str, sections: dict[str, list[dict[str, Any]]], items: list[dict[str, Any]]) -> str:
    lines = [
        "YOLO SETUP WATCH",
        f"Datenstand: {as_of}",
        f"Eligible Opportunities: {len(items)} eindeutige Ticker",
        "",
        "MARKET CONTEXT",
        *market_context_lines(data),
        "",
        f"NEW OPPORTUNITIES ({len(sections['opportunities'])} gezeigt)",
    ]
    if sections["opportunities"]:
        lines.extend(ticker_line(row, i) for i, row in enumerate(sections["opportunities"], 1))
    else:
        lines.append("Keine Leader/Constructive-Reset-Kandidaten im Upstream-Opportunity-Universum.")

    lines.extend(["", f"EXTENDED ACCESS ({len(sections['extended'])} Upstream-markierte Ticker)"])
    if sections["extended"]:
        lines.extend(ticker_line(row, i) for i, row in enumerate(sections["extended"][:SECTION_LIMIT], 1))
        if len(sections["extended"]) > SECTION_LIMIT:
            lines.append(f"+ {len(sections['extended']) - SECTION_LIMIT} weitere Extended-Ticker")
    else:
        lines.append("Keine Extended-Ticker.")

    lines.extend(["", "LAGGARDS — 10 SCHWÄCHSTE"])
    if sections["laggards"]:
        lines.extend(ticker_line(row, i) for i, row in enumerate(sections["laggards"], 1))
    else:
        lines.append("Keine Upstream-markierten Laggards.")

    lines.extend(["", "CHANGES & ALERTS", *change_lines(data), "", "Quelle: DocYolo77/yolo-dashboard · data/dashboard_state.json"])
    return "\n".join(lines)


def write_csv(path: Path, sections: dict[str, list[dict[str, Any]]]) -> None:
    fields = [
        "section", "rank", "symbol", "quality_state", "quality_v2", "structural_rs",
        "trend_strength", "rs_1w", "rs_1m", "rs_3m", "rs_6m", "atr_extension",
        "extended", "extended_v2", "ema10_pullback", "ema20_pullback", "resetting",
        "primary_narrative_name", "secondary_narrative_names", "laggard_narratives",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for section_name, rows in sections.items():
            ordered = rows if section_name != "laggards" else rows[:LAGGARD_LIMIT]
            for rank, item in enumerate(ordered, 1):
                row = {key: item.get(key) for key in fields if key in item}
                row.update(section=section_name, rank=rank, symbol=item["symbol"].upper())
                for key in ("secondary_narrative_names", "laggard_narratives"):
                    if key in row and isinstance(row[key], list):
                        row[key] = "; ".join(str(value) for value in row[key])
                writer.writerow(row)


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        fail("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret")
    payload = json.dumps({"chat_id": chat_id, "text": message, "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        fail(f"Telegram request failed: {exc}")
    if not result.get("ok"):
        fail(f"Telegram API error: {result}")


def send_chunks(message: str) -> None:
    lines = message.splitlines()
    chunk = ""
    for line in lines:
        candidate = line if not chunk else chunk + "\n" + line
        if len(candidate) > MAX_TELEGRAM_LENGTH and chunk:
            send_telegram(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk.strip():
        send_telegram(chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter upstream YOLO Opportunities into a setup digest")
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--output-md", default="data/setup_digest.md")
    parser.add_argument("--output-csv", default="data/setup_candidates.csv")
    parser.add_argument("--no-send", action="store_true", help="Build files and print the digest without sending Telegram")
    args = parser.parse_args()

    dashboard = fetch_dashboard(args.source_url)
    as_of, items = validate_dashboard(dashboard)
    sections = build_sections(items)
    message = render_digest(dashboard, as_of, sections, items)

    md_path, csv_path = Path(args.output_md), Path(args.output_csv)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(message + "\n", encoding="utf-8")
    write_csv(csv_path, sections)

    print(f"Validated upstream snapshot {as_of}: {len(items)} unique opportunities")
    print(
        f"Filtered: new_opportunities={len(sections['opportunities'])}; "
        f"extended={len(sections['extended'])}; laggards_top10={len(sections['laggards'])}"
    )
    print(f"Markdown: {md_path} | CSV: {csv_path}")
    print(message)
    if not args.no_send:
        send_chunks(message)
        print("Telegram setup digest sent.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
