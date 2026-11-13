"""Turn the model's answers into points and pick what is kept."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .config import Config


def points(answers: dict, cfg: Config) -> float:
    p = cfg.points
    total = 0.0
    for q in ("bruikbaar", "past", "rommel"):
        key = {"bruikbaar": "usable", "past": "fits", "rommel": "junk"}[q]
        total += float(p.get(q, {}).get(answers.get(key, ""), 0))
    total += float(p.get("fit_gewicht", 1)) * int(answers.get("fit", 1))
    return total


@dataclass
class Candidate:
    item: dict
    key: str
    answers: dict
    is_repo: bool = False
    is_model: bool = False
    repo_meta: dict | None = None
    score: float = 0.0
    lane: str = ""          # "topper" | "repo" | "gewoon"
    novelty: str = ""       # what this item adds to its topic
    reason: str = ""        # why rejected, when it is
    extra: dict = field(default_factory=dict)


# A model name usually carries its size: "gpt-oss-120b", "Qwen3.8-27B", "Qwen3-235B-A22B".
_PARAMS = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")
# Rough memory need of a 4-bit compressed model: GB per billion parameters, plus working room.
GB_PER_BILLION = 0.55
GB_OVERHEAD = 1.0


def model_memory_gb(c: Candidate) -> float | None:
    """Estimated graphics memory a local model needs, or None when its size is not in the name."""
    it = c.item or {}
    texts = (it.get("resolved_url") or it.get("source_url") or "",
             c.answers.get("title", ""), c.answers.get("topic", ""))
    sizes = [float(m) for t in texts for m in _PARAMS.findall(str(t))]
    sizes = [s for s in sizes if 0 < s < 2000]
    if not sizes:
        return None
    return max(sizes) * GB_PER_BILLION + GB_OVERHEAD


def topper_ok(c: Candidate, cfg: Config) -> bool:
    """A topper must be a repo or local model, flagged by the model, and alive enough."""
    if c.answers.get("topper") != "ja":
        return False
    if not (c.is_repo or c.is_model or c.answers.get("kind") in ("repo", "local_model")):
        return False
    if c.answers.get("junk") == "ja" or c.answers.get("fits") == "nee":
        return False
    if c.is_model or c.answers.get("kind") == "local_model":
        # Not usable right away when it does not fit the reader's graphics card.
        need = model_memory_gb(c)
        if need is not None and need > cfg.topper_max_memory_gb:
            return False
    m = c.repo_meta
    if c.is_repo and m:  # metadata unknown -> trust the model, the weekly review checks again
        if m.get("archived"):
            return False
        if m.get("stars", 0) < cfg.topper_min_stars:
            return False
        if m.get("days_idle", 0) > cfg.topper_max_idle_days:
            return False
    return True


def assign_lanes(cands: list[Candidate], cfg: Config):
    for c in cands:
        c.score = points(c.answers, cfg)
        if topper_ok(c, cfg):
            c.lane = "topper"
        elif c.is_repo:
            c.lane = "repo"
        else:
            c.lane = "gewoon"


def quota(n_regular: int, cfg: Config) -> int:
    if n_regular == 0:
        return 0
    return min(n_regular, max(cfg.minimum, math.ceil(n_regular * cfg.percentage / 100)))


def order(cands: list[Candidate]) -> list[Candidate]:
    """Best first: points, then fit, then having article text."""
    return sorted(
        cands,
        key=lambda c: (c.score, int(c.answers.get("fit", 1)), bool(c.item.get("text"))),
        reverse=True,
    )
