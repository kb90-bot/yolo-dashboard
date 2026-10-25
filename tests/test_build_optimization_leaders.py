import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_optimization_leaders import (
    add_rank_changes,
    build_ranked_lists,
    eligible_tickers,
    format_message,
    parse_source_date,
    snapshot_from_lists,
)


def sample_data():
    return {
        "meta": {"date_range": ["2026-10-22", "2026-10-23"]},
        "tickers": {
            "AAA": {
                "eligible": True,
                "rs_percentile_1w": 90,
                "rs_percentile_1m": 80,
                "rs_percentile_3m": 70,
                "rs_percentile_6m": 60,
                "thrust_percentile_1w": 95,
            },
            "BBB": {
                "eligible": True,
                "rs_percentile_1w": 80,
                "rs_percentile_1m": 90,
                "rs_percentile_3m": 90,
                "rs_percentile_6m": 90,
                "thrust_percentile_1w": 70,
            },
            "INELIGIBLE": {
                "eligible": False,
                "rs_percentile_1w": 100,
                "rs_percentile_1m": 100,
                "rs_percentile_3m": 100,
                "rs_percentile_6m": 100,
                "thrust_percentile_1w": 100,
            },
            "MISSING": {
                "eligible": True,
                "rs_percentile_1w": 99,
                "rs_percentile_1m": None,
                "rs_percentile_3m": 99,
                "rs_percentile_6m": 99,
                "thrust_percentile_1w": None,
            },
        },
    }


def test_uses_upstream_eligibility_flag_without_recreating_filters():
    tickers = eligible_tickers(sample_data())
    assert set(tickers) == {"AAA", "BBB", "MISSING"}


def test_mtf_requires_all_four_timeframes_and_uses_weekly_weight():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    assert [row["symbol"] for row in lists["mtf"]] == ["BBB", "AAA"]
    aaa = next(row for row in lists["mtf"] if row["symbol"] == "AAA")
    assert aaa["mtf_score"] == 75.0  # .2*90 + .3*80 + .3*70 + .2*60
    assert all(row["symbol"] != "MISSING" for row in lists["mtf"])


def test_weekly_strength_and_thrust_are_separate_lists():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    assert [row["symbol"] for row in lists["weekly"]] == ["MISSING", "AAA", "BBB"]
    assert [row["symbol"] for row in lists["thrust"]] == ["AAA", "BBB"]


def test_rank_change_is_positive_when_rank_improves():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    prior = [{
        "date": "2026-10-22",
        "ranks": {
            "weekly": {"AAA": 8, "BBB": 1},
            "mtf": {"AAA": 1, "BBB": 2},
            "thrust": {"AAA": 2, "BBB": 1},
        },
    }]
    add_rank_changes(lists, prior)
    aaa = next(row for row in lists["weekly"] if row["symbol"] == "AAA")
    bbb = next(row for row in lists["weekly"] if row["symbol"] == "BBB")
    assert aaa["rank_change_1d"] == 6
    assert bbb["rank_change_1d"] == -2


def test_no_prior_history_is_explicitly_unavailable():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    add_rank_changes(lists, [])
    assert all(row["rank_change_1d"] is None for rows in lists.values() for row in rows)
    assert all(row["rank_change_5d"] is None for rows in lists.values() for row in rows)


def test_snapshot_has_independent_rank_maps():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    snap = snapshot_from_lists("2026-10-23", lists)
    assert snap["date"] == "2026-10-23"
    assert snap["ranks"]["weekly"]["AAA"] == 2
    assert snap["ranks"]["mtf"]["BBB"] == 1


def test_message_includes_weekly_monthly_multitimeframe_and_thrust():
    lists = build_ranked_lists(eligible_tickers(sample_data()))
    message = format_message("2026-10-23", lists, limit=3)
    assert "WEEKLY STRENGTH" in message
    assert "MULTI-TIMEFRAME LEADERS" in message
    assert "WEEKLY THRUST" in message
    assert "Gewichtung: 1W 20%" in message
    assert "━━━━━━━━━━━━━━━━━━━━" in message
    assert "Rangänderung:" in message


def test_source_date_parsing_uses_latest_market_session():
    assert parse_source_date(sample_data()).isoformat() == "2026-10-23"
