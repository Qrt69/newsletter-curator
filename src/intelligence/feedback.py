"""
Feedback Processor for Newsletter Curator.

Analyzes user accept/reject decisions to find overrides (disagreements
with the scorer), formats them as few-shot examples for prompt injection,
and detects recurring patterns that suggest rule changes.
"""

from collections import defaultdict


class FeedbackProcessor:
    """
    Processes feedback to close the learning loop.

    Usage:
        proc = FeedbackProcessor(store)
        examples = proc.format_examples()
        proposals = proc.get_rule_proposals()
    """

    # Verdicts the scorer considers positive vs negative
    _POSITIVE_VERDICTS = {"strong_fit", "likely_fit"}
    _NEGATIVE_VERDICTS = {"reject", "maybe"}

    def __init__(self, store):
        """
        Args:
            store: DigestStore instance to read feedback from.
        """
        self._store = store

    def get_overrides(self, limit: int = 20) -> list[dict]:
        """
        Find feedback entries where the user disagreed with the scorer.

        An override is when:
        - User accepted an item the scorer said reject/maybe (user promoted)
        - User rejected an item the scorer said strong_fit/likely_fit (user demoted)

        Args:
            limit: Max overrides to return (most recent first).

        Returns:
            List of feedback dicts with an added "override_type" key
            ("promoted" or "demoted").
        """
        feedback = self._store.get_feedback(200)
        overrides = []

        for fb in feedback:
            verdict = fb.get("verdict")
            decision = fb.get("user_decision")

            override_type = None
            if decision == "accepted" and verdict in self._NEGATIVE_VERDICTS:
                override_type = "promoted"
            elif decision == "rejected" and verdict in self._POSITIVE_VERDICTS:
                override_type = "demoted"

            if override_type:
                fb["override_type"] = override_type
                overrides.append(fb)
                if len(overrides) >= limit:
                    break

        return overrides

    def format_examples(self, overrides: list[dict] | None = None, max_examples: int = 10) -> str:
        """
        Format overrides as a text block for system prompt injection.

        Args:
            overrides: Pre-fetched overrides, or None to fetch automatically.
            max_examples: Max examples to include.

        Returns:
            Formatted string to append to the scorer system prompt,
            or empty string if no overrides exist.
        """
        if overrides is None:
            overrides = self.get_overrides(limit=max_examples)
        else:
            overrides = overrides[:max_examples]

        if not overrides:
            return ""

        lines = [
            "\n## Recent Feedback (learn from these corrections)\n",
            "The user reviewed previous suggestions and made these corrections.",
            "Adjust your scoring to align with these preferences:\n",
        ]

        for i, fb in enumerate(overrides, 1):
            name = fb.get("suggested_name") or "Unknown item"
            item_type = fb.get("item_type") or "unknown"
            score = fb.get("score", 0)
            verdict = fb.get("verdict", "unknown")
            url = fb.get("url") or ""
            override_type = fb.get("override_type", "")

            lines.append(f"{i}. **{name}** ({item_type})")
            if url:
                lines.append(f"   URL: {url}")

            if override_type == "promoted":
                lines.append(
                    f"   You scored this {verdict} (score: {score}), "
                    f"but the user ACCEPTED it. Score similar items higher."
                )
            else:
                lines.append(
                    f"   You scored this {verdict} (score: {score}), "
                    f"but the user REJECTED it. Score similar items lower."
                )
            lines.append("")

        return "\n".join(lines)

    def detect_patterns(self, min_count: int = 4) -> list[dict]:
        """
        Group overrides by item_type and find recurring themes.

        Args:
            min_count: Minimum overrides of the same type to flag as a pattern.

        Returns:
            List of pattern dicts with type, override_type, count, examples.
        """
        overrides = self.get_overrides(limit=200)

        # Group by (item_type, override_type)
        groups = defaultdict(list)
        for fb in overrides:
            key = (fb.get("item_type", "unknown"), fb.get("override_type", ""))
            groups[key].append(fb)

        patterns = []
        for (item_type, override_type), items in groups.items():
            if len(items) >= min_count:
                examples = [
                    fb.get("suggested_name") or "Unknown"
                    for fb in items[:5]
                ]
                patterns.append({
                    "item_type": item_type,
                    "override_type": override_type,
                    "count": len(items),
                    "examples": examples,
                })

        return patterns

    def get_rule_proposals(self) -> list[dict]:
        """
        Convert detected patterns into human-readable rule proposals.

        Contradictory patterns (same item_type promoted AND demoted) are
        suppressed — they indicate the type is too broad, not that a rule
        should be added.

        Returns:
            List of proposal dicts with proposal, type, detail,
            evidence_count, examples.
        """
        patterns = self.detect_patterns()

        # Find item_types with patterns in both directions — these cancel out
        types_by_direction = defaultdict(set)
        for pattern in patterns:
            types_by_direction[pattern["override_type"]].add(pattern["item_type"])
        contradictory = types_by_direction.get("promoted", set()) & types_by_direction.get("demoted", set())

        proposals = []

        for pattern in patterns:
            if pattern["item_type"] in contradictory:
                continue
            item_type = pattern["item_type"]
            override_type = pattern["override_type"]
            count = pattern["count"]
            examples = pattern["examples"]

            if override_type == "promoted":
                proposal = (
                    f"You frequently accept {item_type} items that the scorer "
                    f"rates low ({count} times). Consider adding '{item_type}' "
                    f"to the strong interests list."
                )
                proposal_type = "add_interest"
            else:
                proposal = (
                    f"You frequently reject {item_type} items that the scorer "
                    f"rates high ({count} times). Consider adding '{item_type}' "
                    f"to the rejection list."
                )
                proposal_type = "add_rejection"

            proposals.append({
                "proposal": proposal,
                "type": proposal_type,
                "detail": item_type,
                "evidence_count": count,
                "examples": examples,
            })

        return proposals

    def stats(self) -> dict:
        """Summary of feedback analysis."""
        feedback = self._store.get_feedback(200)
        overrides = self.get_overrides(limit=200)
        patterns = self.detect_patterns()
        proposals = self.get_rule_proposals()

        return {
            "total_feedback": len(feedback),
            "total_overrides": len(overrides),
            "patterns_detected": len(patterns),
            "rule_proposals": len(proposals),
        }
