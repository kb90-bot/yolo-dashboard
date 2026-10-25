#!/usr/bin/env python3
"""Build an additive Multi-Timeframe Leaderboard from the upstream YOLO data.

The source of truth is DocYolo77/yolo-dashboard's market_features.json.
This script never writes to that repository or changes any upstream data.
It writes a local derived JSON file and sends a separate Telegram message.
"""
from __future__ import annotations

import argparse
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
    "main/data/market_features.json"
)
WEIGHTS = {"1w": 0.20, "1m": 0.30, "3m": 0.30, "6m": 0.20}
RS_FIELDS = {
    "1w": "rs_percentile_1w",
    "1m": "rs_percentile_1m",
    "3m": "rs_percentile_3m",
    "6m": "rs_percentile_6m",
}
THRUST_FIELD = "thrust_percentile_1w"
MAX_SOURCE_AGE_DAYS = 7
MAX_TELEGRAM_LENGTH = 3800


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def parse_source_date(data: dict[str, Any]) -> date:
    meta = data.get("meta") or {}
    date_range = meta.get("date_range")
    raw = date_range[-1] if isinstance(date_range, list) and date_range else None
    if not raw:
        raw = meta.get("updated_at")
    if isinstance(raw, str):
        raw = raw[:10]
    try:
        result = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("market_features.json has no valid meta.date_range end date") from exc

    today = datetime.now(timezone.utc).date()
    age = (today - result).days
    if age < 0 or age > MAX_SOURCE_AGE_DAYS:
        raise ValueError(f"Source data is future-dated or stale ({result}, age={age} days)")
    return result


def fetch_source(url: str = SOURCE_URL) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "YOLO-MTF-Leaderboard"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not fetch upstream market_features.json: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), dict):
        raise ValueError("market_features.json must contain a tickers object")
    return data


def eligible_tickers(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Use the upstream eligibility decision; never recreate the base filters."""
    return {
        str(symbol).upper(): row
        for symbol, row in data["tickers"].items()
        if isinstance(row, dict) and row.get("eligible") is True
    }


def rank_symbols(tickers: dict[str, dict[str, Any]], field: str) -> list[tuple[str, float]]:
    rows = [
        (symbol, row[field])
        for symbol, row in tickers.items()
        if is_number(row.get(field))
    ]
    rows.sort(key=lambda item: (-float(item[1]), item[0]))
    return rows


def build_ranked_lists(tickers: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    weekly = rank_symbols(tickers, RS_FIELDS["1w"])
    thrust = rank_symbols(tickers, THRUST_FIELD)

    mtf_rows: list[tuple[str, float]] = []
    for symbol, row in tickers.items():
        values = {horizon: row.get(field) for horizon, field in RS_FIELDS.items()}
        # Do not silently redistribute weights when a timeframe is missing.
        if not all(is_number(value) for value in values.values()):
            continue
        score = sum(WEIGHTS[horizon] * float(values[horizon]) for horizon in WEIGHTS)
        mtf_rows.append((symbol, score))
    mtf_rows.sort(key=lambda item: (-item[1], item[0]))

    def materialize(ranked: list[tuple[str, float]], metric_name: str) -> list[dict[str, Any]]:
        result = []
        for rank, (symbol, score) in enumerate(ranked, 1):
            row = tickers[symbol]
            result.append({
                "symbol": symbol,
                "rank": rank,
                metric_name: round(float(score), 2),
                **{field: row.get(field) for field in RS_FIELDS.values()},
                "thrust_percentile_1w": row.get(THRUST_FIELD),
                "stock_thrust_rs": row.get("stock_thrust_rs"),
                "structural_rs": row.get("structural_rs"),
                "trend_strength": row.get("trend_strength"),
                "close": row.get("close"),
                "rvol_50": row.get("rvol_50"),
                "atr_extension": row.get("atr_extension"),
            })
        return result

    return {
        "weekly": materialize(weekly, "weekly_rs"),
        "mtf": materialize(mtf_rows, "mtf_score"),
        "thrust": materialize(thrust, "thrust_percentile_1w"),
    }


def snapshot_from_lists(as_of: str, lists: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "date": as_of,
        "ranks": {
            name: {row["symbol"]: row["rank"] for row in rows}
            for name, rows in lists.items()
        },
    }


def add_rank_changes(
    lists: dict[str, list[dict[str, Any]]],
    prior_snapshots: list[dict[str, Any]],
) -> None:
    """Positive change means a ticker moved UP (e.g. rank 40 -> 25 = +15)."""
    prior = sorted(
        [s for s in prior_snapshots if isinstance(s, dict) and isinstance(s.get("date"), str)],
        key=lambda s: s["date"],
    )
    prior_1 = prior[-1] if len(prior) >= 1 else None
    prior_5 = prior[-5] if len(prior) >= 5 else None

    for list_name, rows in lists.items():
        ranks_1 = ((prior_1 or {}).get("ranks") or {}).get(list_name) or {}
        ranks_5 = ((prior_5 or {}).get("ranks") or {}).get(list_name) or {}
        for row in rows:
            symbol = row["symbol"]
            current = row["rank"]
            old_1 = ranks_1.get(symbol)
            old_5 = ranks_5.get(symbol)
            row["rank_change_1d"] = old_1 - current if isinstance(old_1, int) else None
            row["rank_change_5d"] = old_5 - current if isinstance(old_5, int) else None


def load_history(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        snapshots = value.get("snapshots", []) if isinstance(value, dict) else []
        return snapshots if isinstance(snapshots, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_history(path: Path, as_of: str, snapshot: dict[str, Any], prior: list[dict[str, Any]]) -> None:
    # Keep enough observations to compare the latest ranking with five sessions ago.
    by_date = {
        item["date"]: item
        for item in prior
        if isinstance(item, dict) and isinstance(item.get("date"), str)
    }
    by_date[as_of] = snapshot
    ordered = [by_date[key] for key in sorted(by_date)][-12:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "snapshots": ordered}, ensure_ascii=False), encoding="utf-8")


def signed_change(value: Any) -> str:
    if not isinstance(value, int):
        return "neu"
    return f"{value:+d}"


def format_message(as_of: str, lists: dict[str, list[dict[str, Any]]], limit: int = 10) -> str:
    lines = [
        "YOLO — MULTI-TIMEFRAME LEADERS",
        f"Datenquelle: DocYolo77/yolo-dashboard | Stand: {as_of}",
        "",
        "1) WEEKLY STRENGTH — frische relative Stärke",
    ]
    for row in lists["weekly"][:limit]:
        lines.append(
            f'{row["rank"]:>2}. {row["symbol"]:<6} RS {row["weekly_rs"]:.1f}'
            f' | Rang 1D {signed_change(row.get("rank_change_1d"))}'
            f' | Rang 5D {signed_change(row.get("rank_change_5d"))}'
        )

    lines += ["", "2) MULTI-TIMEFRAME LEADERS — 20% 1W / 30% 1M / 30% 3M / 20% 6M"]
    for row in lists["mtf"][:limit]:
        lines.append(
            f'{row["rank"]:>2}. {row["symbol"]:<6} MTF {row["mtf_score"]:.1f}'
            f' | 1W {row["rs_percentile_1w"]:.0f}'
            f' 1M {row["rs_percentile_1m"]:.0f}'
            f' 3M {row["rs_percentile_3m"]:.0f}'
            f' 6M {row["rs_percentile_6m"]:.0f}'
            f' | Rang 5D {signed_change(row.get("rank_change_5d"))}'
        )

    lines += ["", "3) WEEKLY THRUST — Momentum-Beschleunigung, nicht langfristige Stärke"]
    for row in lists["thrust"][:limit]:
        lines.append(
            f'{row["rank"]:>2}. {row["symbol"]:<6} Thrust {row["thrust_percentile_1w"]:.1f}'
            f' | Weekly RS {row["rs_percentile_1w"]:.1f}'
            f' | Rang 5D {signed_change(row.get("rank_change_5d"))}'
        )

    lines += [
        "",
        "Hinweis: Rangänderungen erscheinen erst, wenn Vergleichs-Snapshots vorliegen.",
        "MTF-Score ist ein neuer, noch nicht historisch validierter Ranking-Score — kein Kaufsignal.",
    ]
    message = "\n".join(lines)
    if len(message) <= MAX_TELEGRAM_LENGTH:
        return message

    # Keep the message under Telegram's safe length even if symbols/data expand.
    return message[: MAX_TELEGRAM_LENGTH - 20].rstrip() + "\n… gekürzt"


def send_telegram(message: str) -> None:
    import urllib.error
    import urllib.request

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret")

    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")
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
        raise RuntimeError(f"Telegram request failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the additive YOLO MTF leaderboard from upstream data")
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--history", default=".cache/optimization_history.json")
    parser.add_argument("--output", default="data/optimization_leaders.json")
    parser.add_argument("--no-send", action="store_true", help="Build output without sending Telegram")
    args = parser.parse_args()

    data = fetch_source(args.source_url)
    as_of_date = parse_source_date(data)
    as_of = as_of_date.isoformat()
    tickers = eligible_tickers(data)
    lists = build_ranked_lists(tickers)

    history_path = Path(args.history)
    prior = [s for s in load_history(history_path) if s.get("date") != as_of]
    add_rank_changes(lists, prior)
    snapshot = snapshot_from_lists(as_of, lists)
    save_history(history_path, as_of, snapshot, prior)

    output = {
        "meta": {
            "as_of_date": as_of,
            "source": SOURCE_URL,
            "eligible_universe_size": len(tickers),
            "weights": WEIGHTS,
            "rank_change_5d_available": len(prior) >= 5,
        },
        "leaderboards": {name: rows[:50] for name, rows in lists.items()},
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    message = format_message(as_of, lists)
    print(f"Built {output_path}; eligible={len(tickers)}; weekly={len(lists['weekly'])}; "
          f"mtf={len(lists['mtf'])}; thrust={len(lists['thrust'])}; prior_snapshots={len(prior)}")
    if not args.no_send:
        send_telegram(message)
        print("Telegram MTF leaderboard sent.")
    else:
        print(message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
