"""Read the three TOML files Kurt can edit. Paths can be overridden for tests."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_CONFIG = Path(__file__).parent / "config"


def config_dir() -> Path:
    """
    Kurt's own copy wins: C:\\Dev\\curator2-instellingen (next to the program folder),
    so updating the program never overwrites his edits. The copy inside the
    package is only the starting point.
    """
    env = os.environ.get("CURATOR2_CONFIG")
    if env:
        return Path(env)
    own = Path(__file__).resolve().parent.parent.parent / "curator2-instellingen"
    if (own / "instellingen.toml").exists():
        return own
    return PACKAGE_CONFIG


@dataclass
class Rule:
    name: str
    domains: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    url_starts: list[str] = field(default_factory=list)
    url_contains: list[str] = field(default_factory=list)
    require_all: bool = False


@dataclass
class Config:
    vault: Path
    folder: str
    percentage: float
    minimum: int
    reject_keep_days: int
    pattern_threshold: int
    topper_min_stars: int
    topper_max_idle_days: int
    llm_url: str
    llm_model: str
    llm_timeout: int
    max_text: int
    rules: list[Rule]
    profile: str
    points: dict
    categories: dict[str, str]
    never_categories: list[str]
    judge_kind: str = "claude"
    claude_model: str = "claude-haiku-5-5"
    claude_monthly_cap: float = 5.0
    claude_batch: bool = False
    claude_max_wait_minutes: int = 240
    claude_price_in: float = 0.10
    claude_price_out: float = 0.50
    claude_batch_discount: float = 0.5

    @property
    def root(self) -> Path:
        return self.vault / self.folder


def _read(name: str, folder: Path) -> dict:
    with open(folder / name, "rb") as f:
        return tomllib.load(f)


def load(cfg_dir: Path | None = None, vault_override: Path | None = None) -> Config:
    d = Path(cfg_dir) if cfg_dir else config_dir()
    s = _read("instellingen.toml", d)
    r = _read("regels.toml", d)
    b = _read("beoordeling.toml", d)

    rules = [
        Rule(
            name=x["naam"],
            domains=[v.lower() for v in x.get("domeinen", [])],
            words=[v.lower() for v in x.get("woorden", [])],
            url_starts=[v.lower() for v in x.get("adres_begint", [])],
            url_contains=[v.lower() for v in x.get("adres_bevat", [])],
            require_all=bool(x.get("alle", False)),
        )
        for x in r.get("regel", [])
    ]

    vault = Path(os.environ.get("CURATOR2_VAULT") or vault_override or s["osk"]["vault"])
    return Config(
        vault=vault,
        folder=s["osk"].get("map", "Curator"),
        percentage=float(s["selectie"].get("percentage", 10)),
        minimum=int(s["selectie"].get("minimum", 1)),
        reject_keep_days=int(s["verworpen"].get("bewaar_dagen", 90)),
        pattern_threshold=int(s["verworpen"].get("patroon_drempel", 5)),
        topper_min_stars=int(s["topper"].get("min_sterren", 50)),
        topper_max_idle_days=int(s["topper"].get("max_dagen_stil", 180)),
        llm_url=s["lmstudio"].get("adres", "http://127.0.0.1:1234/v1"),
        llm_model=s["lmstudio"].get("model", ""),
        llm_timeout=int(s["lmstudio"].get("timeout", 180)),
        max_text=int(s["lmstudio"].get("max_tekst", 3000)),
        rules=rules,
        profile=b["profiel"].strip(),
        points=b["punten"],
        categories=b["categorieen"],
        never_categories=b.get("nooit", {}).get("categorieen", []),
        judge_kind=s.get("beoordelaar", {}).get("soort", "claude"),
        claude_model=s.get("claude", {}).get("model", "claude-haiku-5-5"),
        claude_monthly_cap=float(s.get("claude", {}).get("maandplafond_dollar", 5.0)),
        claude_batch=bool(s.get("claude", {}).get("pakket", False)),
        claude_max_wait_minutes=int(s.get("claude", {}).get("max_wachten_minuten", 240)),
        claude_price_in=float(s.get("claude", {}).get("prijs_invoer", 0.10)),
        claude_price_out=float(s.get("claude", {}).get("prijs_uitvoer", 0.50)),
        claude_batch_discount=float(s.get("claude", {}).get("pakket_korting", 0.5)),
    )
