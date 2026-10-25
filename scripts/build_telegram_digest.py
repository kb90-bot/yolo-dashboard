#!/usr/bin/env python3
"""Build one concise, signal-focused YOLO Telegram digest from upstream data."""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

BASE = "https://raw.githubusercontent.com/DocYolo77/yolo-dashboard/main/data/"
MAX_AGE_DAYS = 7
MAX_MESSAGE_LENGTH = 3800


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch_json(filename: str) -> dict:
    request = urllib.request.Request(BASE + filename, headers={"User-Agent": "YOLO-Telegram-Digest"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"Could not load {filename}: {exc}")
    if not isinstance(data, dict):
        fail(f"{filename}: expected a JSON object")
    return data


def isnum(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value, digits=1) -> str:
    return f"{value:.{digits}f}" if isnum(value) else "n/a"


def source_date(data: dict, filename: str) -> date:
    meta = data.get("meta") or {}
    raw = meta.get("as_of_date") or meta.get("updated_at")
    if not raw and isinstance(meta.get("date_range"), list) and meta["date_range"]:
        raw = meta["date_range"][-1]
    if isinstance(raw, str):
        raw = raw[:10]
    try:
        result = date.fromisoformat(raw)
    except (TypeError, ValueError):
        fail(f"{filename}: missing or invalid data date")
    age = (datetime.now(timezone.utc).date() - result).days
    if age < 0 or age > MAX_AGE_DAYS:
        fail(f"{filename}: source data is stale or future-dated ({result})")
    return result


def regime_lines(dashboard: dict) -> list[str]:
    regime = dashboard.get("market_regime") or {}
    state = str(regime.get("state") or "UNKNOWN").upper().replace("_", " ")
    score, delta = regime.get("score"), regime.get("score_delta")
    recs = {
        "STRONG OFFENSIVE": "Offensiver handeln, aber nur mit bestätigtem Follow-through.",
        "OFFENSIVE": "Neue Risiken selektiv und mit bestätigtem Follow-through eingehen.",
        "SELECTIVE": "Selektiv handeln; Exposition erst bei positivem Feedback erhöhen.",
        "DEFENSIVE": "Risiko reduzieren; selektiv handeln und nur bei positivem Feedback erhöhen.",
        "RISK OFF": "Kapitalerhalt priorisieren; neue Risiken stark begrenzen.",
    }
    line = f"Marktregime: {state}"
    if isnum(score):
        line += f" | Score {fmt(score)}"
    if isnum(delta):
        line += f" ({delta:+.1f})"
    lines = [line, recs.get(state, "Risiko nicht allein anhand dieses Feeds erhöhen.")]
    qqq = dashboard.get("qqq_health") or {}
    qstate = qqq.get("base_state")
    qscore, qdelta = qqq.get("score"), qqq.get("score_delta")
    if qstate:
        qline = f"QQQ: {qstate}"
        if isnum(qscore):
            qline += f" | {fmt(qscore)}"
        # The score delta is already highlighted in the change section.
        lines.append(qline)
    return lines


def change_lines(dashboard: dict, limit: int = 6) -> list[str]:
    events = ((dashboard.get("what_changed") or {}).get("events") or [])
    if not isinstance(events, list) or not events:
        return ["Keine belastbaren neuen Änderungen gemeldet."]

    # Group stock events by ticker to avoid repeating one ticker across event types.
    stock: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("category") != "stock":
            continue
        symbol, typ = event.get("symbol"), event.get("type")
        if isinstance(symbol, str) and isinstance(typ, str):
            stock.setdefault(symbol.upper(), set()).add(typ)

    result: list[str] = []
    # Meaningful QQQ deterioration/improvement, rendered from actual values.
    qqq = dashboard.get("qqq_health") or {}
    if qqq.get("base_state") and isnum(qqq.get("score_delta")) and abs(qqq["score_delta"]) >= 5:
        direction = "gefallen" if qqq["score_delta"] < 0 else "gestiegen"
        result.append(f"QQQ-Health: {qqq.get('base_state')} — Score {direction} um {abs(qqq['score_delta']):.1f} auf {fmt(qqq.get('score'))}.")

    # Summarize narrative events by theme so rank, score and modifier changes do not spam.
    narrative_groups: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("category") != "narrative":
            continue
        name = event.get("narrative_name") or event.get("narrative_id")
        typ = event.get("type")
        if not isinstance(name, str) or not isinstance(typ, str):
            continue
        group = narrative_groups.setdefault(name, {})
        group[typ] = event

    narrative_lines = []
    for name, group in narrative_groups.items():
        parts = []
        surge = group.get("structural_score_surge")
        if surge and isnum(surge.get("from_score")) and isnum(surge.get("to_score")):
            parts.append(f"structuraler Score {surge['from_score']:.1f} → {surge['to_score']:.1f}")
        rank = group.get("rank_improved")
        if rank and isinstance(rank.get("from_rank"), int) and isinstance(rank.get("to_rank"), int):
            parts.append(f"Rang {rank['from_rank']} → {rank['to_rank']}")
        if "new_active" in group:
            parts.append("neu aktiv")
        if "new_fading" in group:
            parts.append("fällt zurück")
        gained = sorted({
            str(ev.get("modifier")) for typ, ev in group.items()
            if typ == "modifier_gained" and ev.get("modifier")
        })
        lost = sorted({
            str(ev.get("modifier")) for typ, ev in group.items()
            if typ == "modifier_lost" and ev.get("modifier")
        })
        if gained:
            parts.append("Momentum: " + ", ".join(gained) + " hinzu")
        if lost:
            parts.append("Momentum: " + ", ".join(lost) + " verloren")
        if parts:
            priority = 100 if surge else (60 if rank else 20)
            narrative_lines.append((priority, name, f"{name}: " + "; ".join(parts) + "."))

    for _, _, line in sorted(narrative_lines, key=lambda row: (-row[0], row[1])):
        if line not in result and len(result) < limit - 3:
            result.append(line)

    stock_lines = []
    for symbol, types in stock.items():
        if "new_fresh_leader" in types:
            extra = " (gleichzeitig überdehnt)" if "new_extended" in types else ""
            if "new_constructive_reset" in types:
                extra += " + konstruktiver Reset"
            stock_lines.append(f"{symbol}: neuer Leader{extra}.")
        elif "new_constructive_reset" in types:
            stock_lines.append(f"{symbol}: konstruktiver Rücksetzer.")
        elif "extension_ended" in types:
            stock_lines.append(f"{symbol}: Überdehnung beendet.")
        elif "recent_leader_expired" in types:
            stock_lines.append(f"{symbol}: Leader-Status ausgelaufen.")
    result.extend(stock_lines[:max(0, limit - len(result))])
    return result[:limit] if result else ["Keine Änderungen mit ausreichend konkretem Signalwert."]


def narrative_consensus(narrative_data: dict, limit: int = 5) -> list[str]:
    narratives = narrative_data.get("narratives") or []
    if not isinstance(narratives, list):
        return ["Narrative-Daten nicht verfügbar."]
    horizons = ("1w", "1m", "3m", "6m")
    rows = []
    for item in narratives:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("label") or item.get("id")
        rs = item.get("narrative_rs") or {}
        values = [rs.get(h) for h in horizons] if isinstance(rs, dict) else []
        valid = [float(v) for v in values if isnum(v)]
        if isinstance(name, str) and name.strip() and len(valid) >= 3:
            rows.append((sum(valid) / len(valid), sum(v >= 70 for v in valid), name.strip(), values, item))
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    lines = []
    for avg, strong_count, name, values, item in rows[:limit]:
        labels = "/".join(f"{v:.0f}" if isnum(v) else "—" for v in values)
        status = item.get("status")
        status_text = f" | {status}" if isinstance(status, str) and status else ""
        lines.append(f"• {name}: ØRS {avg:.1f}; ≥70 in {strong_count}/4 TF (1W/1M/3M/6M {labels}){status_text}")
    return lines or ["Keine vergleichbaren Mehrzeitfenster-Narrative."]


def screener_rows(screener_data: dict, limit: int = 4) -> list[str]:
    screeners = screener_data.get("screeners") or {}
    sections = [
        ("1W", "weekly_strength", "rs_percentile_1w"),
        ("1M", "monthly_strength", "rs_percentile_1m"),
        ("3M", "three_month_strength", "rs_percentile_3m"),
        ("6M", "six_month_strength", "rs_percentile_6m"),
    ]
    # Select high-ranking names that add a timeframe-specific view; suppress repeats
    # across lists after their first appearance to keep the section genuinely additive.
    seen: set[str] = set()
    lines = []
    for label, key, metric in sections:
        screener = screeners.get(key) if isinstance(screeners, dict) else None
        tickers = screener.get("tickers", []) if isinstance(screener, dict) else []
        ranked = []
        if isinstance(tickers, list):
            for ticker in tickers:
                if not isinstance(ticker, dict):
                    continue
                symbol, value = ticker.get("symbol"), ticker.get(metric)
                if isinstance(symbol, str) and isnum(value):
                    ranked.append((float(value), symbol.upper(), ticker))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        chosen = [row for row in ranked if row[1] not in seen][:1]
        if chosen:
            lines.append(f"{label} — zusätzliche Zeitfenster-Kandidaten:")
            for value, symbol, ticker in chosen:
                seen.add(symbol)
                price = ticker.get("close")
                lines.append(f"• {symbol} RS {value:.1f}" + (f" | Kurs {price:.2f}" if isnum(price) else ""))
    return lines or ["Keine zusätzlichen Zeitfenster-Kandidaten außerhalb der bereits gezeigten Leader."]


def leaderboard_lines() -> list[str]:
    path = "data/optimization_leaders.json"
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["MTF-/Thrust-Ranglisten konnten nicht aufgebaut werden."]
    lists = data.get("leaderboards") or {}
    lines = []
    mtf = lists.get("mtf") or []
    if mtf:
        lines.append("MTF LEADERS — Stärke über 1W/1M/3M/6M")
        for row in mtf[:5]:
            lines.append(f"• {row.get('symbol')} MTF {fmt(row.get('mtf_score'))} | 1W {fmt(row.get('rs_percentile_1w'),0)} · 1M {fmt(row.get('rs_percentile_1m'),0)} · 3M {fmt(row.get('rs_percentile_3m'),0)} · 6M {fmt(row.get('rs_percentile_6m'),0)}")
    thrust = lists.get("thrust") or []
    if thrust:
        mtf_symbols = {r.get("symbol") for r in mtf[:5] if isinstance(r, dict)}
        unique = [r for r in thrust if isinstance(r, dict) and r.get("symbol") not in mtf_symbols][:3]
        if unique:
            lines.append("WEEKLY THRUST — Beschleunigung, nicht Langfrist-Stärke")
            for row in unique:
                lines.append(f"• {row.get('symbol')} Thrust {fmt(row.get('thrust_percentile_1w'))} | Weekly RS {fmt(row.get('rs_percentile_1w'))}")
    return lines or ["Keine Leaderboard-Daten verfügbar."]


def send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        fail("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret")
    payload = json.dumps({"chat_id": chat_id, "text": message, "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        fail(f"Telegram request failed: {exc}")
    if not result.get("ok"):
        fail(f"Telegram API error: {result}")


def send_chunks(lines: list[str]) -> None:
    # Split only between lines; each Telegram message remains below the API limit.
    chunk = ""
    for line in lines:
        candidate = line if not chunk else chunk + "\n" + line
        if len(candidate) > MAX_MESSAGE_LENGTH and chunk:
            send_telegram(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk.strip():
        send_telegram(chunk)


def simple_digest_lines(dashboard: dict, narrative_data: dict, screener_data: dict) -> list[str]:
    lines = ["YOLO DAILY", f"Datenstand: {source_date(dashboard, 'dashboard_state.json').isoformat()}", "",
             "MARKTSTATUS", str((dashboard.get("market_regime") or {}).get("state") or "UNKNOWN").upper().replace("_", " ")]
    qqq = dashboard.get("qqq_health") or {}
    if qqq.get("base_state"):
        lines.extend(["", "QQQ", str(qqq["base_state"])])

    events = ((dashboard.get("what_changed") or {}).get("events") or [])
    lines.extend(["", "NARRATIVE-ÄNDERUNGEN"])
    changes = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("category") != "narrative":
            continue
        name = event.get("narrative_name") or event.get("narrative_id") or "Narrative"
        typ = str(event.get("type") or "").replace("_", " ")
        detail = ""
        if isnum(event.get("from_score")) and isnum(event.get("to_score")):
            detail = f" | {fmt(event['from_score'])} → {fmt(event['to_score'])}"
        elif isinstance(event.get("from_rank"), int) and isinstance(event.get("to_rank"), int):
            detail = f" | Rang {event['from_rank']} → {event['to_rank']}"
        elif event.get("modifier"):
            detail = f" | {event['modifier']}"
        changes.append(f"{name} | {typ}{detail}")
    lines.extend(changes[:6] or ["Keine Narrativ-Änderungen gemeldet."])

    narratives = [n for n in (narrative_data.get("narratives") or []) if isinstance(n, dict) and isinstance(n.get("name"), str)]
    horizons = [("1W", "1w"), ("1M", "1m"), ("3M", "3m"), ("6M", "6m")]
    lines.extend(["", "NARRATIVES"])
    for label, key in horizons:
        strength_rows, thrust_rows = [], []
        for item in narratives:
            strength_map = item.get("strength_rsp") or item.get("narrative_rs") or {}
            strength = strength_map.get(key) if isinstance(strength_map, dict) else None
            score = (item.get("scores") or {}).get(key) or {}
            thrust = score.get("thrust") if isinstance(score, dict) else None
            if isnum(strength):
                strength_rows.append((float(strength), item["name"], float(thrust) if isnum(thrust) else None))
            if isnum(thrust):
                thrust_rows.append((float(thrust), item["name"], float(strength) if isnum(strength) else None))
        strength_rows.sort(key=lambda x: (-x[0], x[1]))
        thrust_rows.sort(key=lambda x: (-x[0], x[1]))
        lines.extend(["", label, "Strength"])
        for value, name, thrust in strength_rows[:10]:
            # Convert the horizon's raw thrust score to a cross-sectional percentile.
            rank = next((i for i, row in enumerate(thrust_rows) if row[1] == name), None)
            pct = 100 * (len(thrust_rows) - rank - 1) / max(1, len(thrust_rows) - 1) if rank is not None else None
            lines.append(f"{name} | {fmt(value)}% | {fmt(pct)}%")
        lines.extend(["", "Thrust"])
        for rank, (value, name, strength) in enumerate(thrust_rows[:10]):
            pct = 100 * (len(thrust_rows) - rank - 1) / max(1, len(thrust_rows) - 1)
            lines.append(f"{name} | {fmt(strength)}% | {fmt(pct)}%")

    screeners = screener_data.get("screeners") or {}
    defs = [("1W", "weekly_strength", "rs_percentile_1w"), ("1M", "monthly_strength", "rs_percentile_1m"),
            ("3M", "three_month_strength", "rs_percentile_3m"), ("6M", "six_month_strength", "rs_percentile_6m")]
    def ranked_lines(title: str, exclude_extended: bool = False) -> list[str]:
        result = [title]
        for label, key, metric in defs:
            section = screeners.get(key) if isinstance(screeners, dict) else None
            tickers = section.get("tickers", []) if isinstance(section, dict) else []
            ranked = []
            for ticker in tickers if isinstance(tickers, list) else []:
                if not isinstance(ticker, dict) or not isinstance(ticker.get("symbol"), str) or not isnum(ticker.get(metric)):
                    continue
                if exclude_extended and isnum(ticker.get("atr_extension")) and ticker["atr_extension"] >= 2.0:
                    continue
                ranked.append((float(ticker[metric]), ticker["symbol"].upper(), ticker))
            ranked.sort(key=lambda x: (-x[0], x[1]))
            result.extend(["", label])
            result.extend(f"{symbol} | {fmt(value)}% | {fmt(ticker.get('stock_thrust_rs'))}%" for value, symbol, ticker in ranked[:10])
        return result

    lines.extend(["", "OPPORTUNITIES", "", "FRESH LEADERS"])
    stock_events = {}
    for event in events if isinstance(events, list) else []:
        if isinstance(event, dict) and event.get("category") == "stock" and event.get("symbol"):
            stock_events.setdefault(str(event["symbol"]).upper(), set()).add(event.get("type"))
    fresh = [symbol for symbol, types in stock_events.items() if "new_fresh_leader" in types and "new_extended" not in types]
    lines.extend(fresh[:10] or ["Keine frischen Leader gemeldet."])
    lines.extend(["", "LEADERS"])
    lines.extend(ranked_lines("LEADERS", exclude_extended=True)[1:])
    lines.extend(["", "SCREENER-ERGEBNISSE"])
    lines.extend(ranked_lines("SCREENER-ERGEBNISSE")[1:])
    lines.extend(["", "Relative Stärke ist kein Kaufsignal.", "Quelle: DocYolo77/yolo-dashboard"])
    return lines


def main() -> None:
    dashboard = fetch_json("dashboard_state.json")
    narrative_data = fetch_json("narratives.json")
    screener_data = fetch_json("screeners.json")
    dates = [
        source_date(dashboard, "dashboard_state.json"),
        source_date(narrative_data, "narratives.json"),
        source_date(screener_data, "screeners.json"),
    ]
    if len(set(dates)) != 1:
        fail(f"Snapshot dates do not match: {dates}")
    as_of = dates[0].isoformat()
    lines = simple_digest_lines(dashboard, narrative_data, screener_data)
    send_chunks(lines)
    print(f"Simple Telegram digest sent. Snapshot: {as_of}")


if __name__ == "__main__":
    main()
