"""
Tests for FeedbackProcessor.

All tests use an in-memory SQLite database (no API calls).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.digest import DigestStore
from src.intelligence.feedback import FeedbackProcessor


def _make_store() -> DigestStore:
    """Create an in-memory DigestStore for testing."""
    return DigestStore(":memory:")


def _seed_item(store, run_id, verdict, score, item_type="article",
               name="Test Item", url="https://example.com/test",
               action="propose", target_database="Articles & Reads"):
    """Insert a minimal item and return its id."""
    decision = {
        "url": url,
        "link_text": name,
        "score": score,
        "verdict": verdict,
        "item_type": item_type,
        "description": "Test description",
        "reasoning": "Test reasoning",
        "signals": [],
        "suggested_name": name,
        "suggested_category": "Test",
        "tags": [],
        "target_database": target_database,
        "dedup_status": "new",
        "dedup_matches": [],
        "action": action,
    }
    return store.add_item(run_id, decision)


class TestGetOverrides(unittest.TestCase):
    """Test get_overrides returns only disagreements."""

    def test_get_overrides(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # Agreement: scorer says strong_fit, user accepts -> NOT an override
        item1 = _seed_item(store, run_id, "strong_fit", 6, name="Agreed Item")
        store.set_decision(item1, "accepted")

        # Override: scorer says reject, user accepts -> promoted
        item2 = _seed_item(store, run_id, "reject", -1, name="Promoted Item 1")
        store.set_decision(item2, "accepted")

        # Override: scorer says maybe, user accepts -> promoted
        item3 = _seed_item(store, run_id, "maybe", 2, name="Promoted Item 2")
        store.set_decision(item3, "accepted")

        # Override: scorer says likely_fit, user rejects -> demoted
        item4 = _seed_item(store, run_id, "likely_fit", 4, name="Demoted Item")
        store.set_decision(item4, "rejected")

        overrides = proc.get_overrides()
        self.assertEqual(len(overrides), 3)

        types = [o["override_type"] for o in overrides]
        self.assertIn("promoted", types)
        self.assertIn("demoted", types)


class TestFormatExamplesEmpty(unittest.TestCase):
    """Test format_examples returns empty string with no feedback."""

    def test_format_examples_empty(self):
        store = _make_store()
        proc = FeedbackProcessor(store)
        result = proc.format_examples()
        self.assertEqual(result, "")


class TestFormatExamplesWithOverrides(unittest.TestCase):
    """Test format_examples returns formatted text with overrides."""

    def test_format_examples_with_overrides(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # Promoted override
        item1 = _seed_item(store, run_id, "reject", -1,
                           name="PG Guide", url="https://example.com/pg")
        store.set_decision(item1, "accepted")

        # Demoted override
        item2 = _seed_item(store, run_id, "likely_fit", 4,
                           name="React Builder", url="https://example.com/react")
        store.set_decision(item2, "rejected")

        result = proc.format_examples()
        self.assertIn("Recent Feedback", result)
        self.assertIn("PG Guide", result)
        self.assertIn("ACCEPTED", result)
        self.assertIn("React Builder", result)
        self.assertIn("REJECTED", result)


class TestDetectPatterns(unittest.TestCase):
    """Test detect_patterns finds recurring themes above threshold."""

    def test_detect_patterns(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # 5 promoted overrides of same item_type
        for i in range(5):
            item_id = _seed_item(store, run_id, "reject", -1,
                                 item_type="article", name=f"Article {i}",
                                 url=f"https://example.com/art{i}")
            store.set_decision(item_id, "accepted")

        patterns = proc.detect_patterns(min_count=4)
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["item_type"], "article")
        self.assertEqual(patterns[0]["override_type"], "promoted")
        self.assertGreaterEqual(patterns[0]["count"], 4)


class TestDetectPatternsBelowThreshold(unittest.TestCase):
    """Test detect_patterns returns empty when below min_count."""

    def test_detect_patterns_below_threshold(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # Only 2 overrides
        for i in range(2):
            item_id = _seed_item(store, run_id, "reject", -1,
                                 item_type="article", name=f"Article {i}",
                                 url=f"https://example.com/art{i}")
            store.set_decision(item_id, "accepted")

        patterns = proc.detect_patterns(min_count=4)
        self.assertEqual(len(patterns), 0)


class TestGetRuleProposals(unittest.TestCase):
    """Test get_rule_proposals returns proposals with correct structure."""

    def test_get_rule_proposals(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # 5 promoted overrides
        for i in range(5):
            item_id = _seed_item(store, run_id, "maybe", 1,
                                 item_type="python_library",
                                 name=f"Library {i}",
                                 url=f"https://example.com/lib{i}")
            store.set_decision(item_id, "accepted")

        proposals = proc.get_rule_proposals()
        self.assertEqual(len(proposals), 1)

        p = proposals[0]
        self.assertIn("proposal", p)
        self.assertIn("type", p)
        self.assertIn("detail", p)
        self.assertIn("evidence_count", p)
        self.assertIn("examples", p)
        self.assertEqual(p["type"], "add_interest")
        self.assertEqual(p["detail"], "python_library")
        self.assertEqual(p["evidence_count"], 5)


class TestNoOverridesWhenAgreement(unittest.TestCase):
    """Test that agreements produce no overrides."""

    def test_no_overrides_when_agreement(self):
        store = _make_store()
        run_id = store.create_run(emails_fetched=1)
        proc = FeedbackProcessor(store)

        # All agreements: scorer and user agree
        item1 = _seed_item(store, run_id, "strong_fit", 6, name="Good Item 1")
        store.set_decision(item1, "accepted")

        item2 = _seed_item(store, run_id, "likely_fit", 4, name="Good Item 2")
        store.set_decision(item2, "accepted")

        item3 = _seed_item(store, run_id, "reject", -1, name="Bad Item 1")
        store.set_decision(item3, "rejected")

        item4 = _seed_item(store, run_id, "maybe", 1, name="Meh Item 1")
        store.set_decision(item4, "rejected")

        overrides = proc.get_overrides()
        self.assertEqual(len(overrides), 0)

        proposals = proc.get_rule_proposals()
        self.assertEqual(len(proposals), 0)


if __name__ == "__main__":
    unittest.main()
