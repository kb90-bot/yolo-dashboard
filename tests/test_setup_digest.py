import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_telegram_digest as digest


def item(symbol, structural_rs, **extra):
    row = {"symbol": symbol, "structural_rs": structural_rs, "quality_state": "neutral",
           "extended": False, "laggard_narratives": [], "constructive_reset_narratives": []}
    row.update(extra)
    return row


class SetupDigestTests(unittest.TestCase):
    def test_validation_uses_upstream_opportunities_and_exact_universe_count(self):
        data = {
            "meta": {"as_of_date": "2026-11-05"},
            "market_regime": {"eligible_universe_size": 2},
            "opportunities": {"items": [item("AAA", 90), item("BBB", 80)]},
        }
        as_of, items = digest.validate_dashboard(data, today=date(2026, 11, 5))
        self.assertEqual(as_of, "2026-11-05")
        self.assertEqual([x["symbol"] for x in items], ["AAA", "BBB"])

    def test_validation_rejects_universe_mismatch_and_duplicate_symbols(self):
        mismatch = {
            "meta": {"as_of_date": "2026-11-05"},
            "market_regime": {"eligible_universe_size": 3},
            "opportunities": {"items": [item("AAA", 90), item("BBB", 80)]},
        }
        with self.assertRaisesRegex(ValueError, "Universe validation failed"):
            digest.validate_dashboard(mismatch, today=date(2026, 11, 5))

        duplicate = {
            "meta": {"as_of_date": "2026-11-05"},
            "opportunities": {"items": [item("AAA", 90), item("aaa", 80)]},
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            digest.validate_dashboard(duplicate, today=date(2026, 11, 5))

    def test_extended_sorted_by_structural_rs_descending(self):
        rows = [
            item("LOW", 55, extended=True),
            item("HIGH", 91, extended=True),
            item("MID", 72, extended=True),
        ]
        sections = digest.build_sections(rows)
        self.assertEqual([x["symbol"] for x in sections["extended"]], ["HIGH", "MID", "LOW"])

    def test_laggards_are_the_ten_weakest_by_structural_rs(self):
        rows = [item(f"T{i:02}", i, laggard_narratives=["n1"]) for i in range(20, 0, -1)]
        sections = digest.build_sections(rows)
        self.assertEqual(len(sections["laggards"]), 10)
        self.assertEqual([x["structural_rs"] for x in sections["laggards"]], list(range(1, 11)))

    def test_candidates_only_come_from_opportunities_items(self):
        rows = [
            item("FRESH", 95, quality_state="fresh_leader"),
            item("RESET", 82, constructive_reset_narratives=["n1"]),
            item("NEUTRAL", 70),
        ]
        sections = digest.build_sections(rows)
        self.assertEqual({x["symbol"] for x in sections["opportunities"]}, {"FRESH", "RESET"})


if __name__ == "__main__":
    unittest.main()
