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
        if isnum(qdelta):
            qline += f" ({qdelta:+.1f})"
        lines.append(qline)
    return lines


def change_lines(dashboard: dict, limit: int = 8) -> list[str]:
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

    # Use the current narrative state snapshot, not raw event-code strings.
    narratives = ((dashboard.get("narratives") or {}).get("items") or {})
    narrative_candidates = []
    if isinstance(narratives, dict):
        for item in narratives.values():
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("narrative_name")
            delta5 = item.get("structural_score_delta5d")
            rank_delta = item.get("rank_delta")
            state = str(item.get("lifecycle_state") or "").upper()
            modifier = str(item.get("momentum_modifier") or "").upper()
            if not isinstance(name, str):
                continue
            if isnum(delta5) and delta5 >= 8:
                narrative_candidates.append((delta5, f"{name}: struktureller Score {delta5:+.1f} in 5 Tagen" + (f", {modifier.lower()}" if modifier else "") + "."))
            elif isnum(rank_delta) and rank_delta >= 3 and state in {"ACTIVE", "EMERGING", "MATURE"}:
                narrative_candidates.append((rank_delta * 2, f"{name}: im Themenranking {int(rank_delta)} Plätze gestiegen ({state.lower()})."))
        seen = set()
        for _, line in sorted(narrative_candidates, reverse=True):
            if line not in seen:
                result.append(line)
                seen.add(line)
            if len(result) >= limit - 3:
                break

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


def narrative_consensus(narrative_data: dict, limit: int = 8) -> list[str]:
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


def screener_rows(screener_data: dict, limit: int = 6) -> list[str]:
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
        chosen = [row for row in ranked if row[1] not in seen][:3]
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
        for row in mtf[:8]:
            lines.append(f"• {row.get('symbol')} MTF {fmt(row.get('mtf_score'))} | 1W {fmt(row.get('rs_percentile_1w'),0)} · 1M {fmt(row.get('rs_percentile_1m'),0)} · 3M {fmt(row.get('rs_percentile_3m'),0)} · 6M {fmt(row.get('rs_percentile_6m'),0)}")
    thrust = lists.get("thrust") or []
    if thrust:
        mtf_symbols = {r.get("symbol") for r in mtf[:8] if isinstance(r, dict)}
        unique = [r for r in thrust if isinstance(r, dict) and r.get("symbol") not in mtf_symbols][:6]
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

    lines = [
        "YOLO — MOMENTUM DASHBOARD",
        f"Datenstand: {as_of}",
        "",
        "1. DIE WICHTIGSTEN VERÄNDERUNGEN",
        *change_lines(dashboard),
        "",
        "2. MARKTREGIME",
        *regime_lines(dashboard),
        "",
        "3. THEMENSTÄRKE ÜBER ZEITFENSTER",
        *narrative_consensus(narrative_data),
        "",
        "4. ZUSÄTZLICHE ZEITFENSTER-KANDIDATEN",
        *screener_rows(screener_data),
        "",
        "5. MULTI-TIMEFRAME LEADERS & THRUST",
        *leaderboard_lines(),
        "",
        "Hinweis: Rankings sind relative Stärke-Screenings, keine Kaufsignale. MTF-Score ist experimentell.",
        "Quelle: DocYolo77/yolo-dashboard",
    ]
    send_chunks(lines)
    print(f"Concise Telegram digest sent. Snapshot: {as_of}")


if __name__ == "__main__":
    main()
