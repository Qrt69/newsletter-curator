"""Read the three TOML files Kurt can edit. Paths can be overridden for tests."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CURATOR2_CONFIG", Path(__file__).parent / "config"))


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

    @property
    def root(self) -> Path:
        return self.vault / self.folder


def _read(name: str, config_dir: Path) -> dict:
    with open(config_dir / name, "rb") as f:
        return tomllib.load(f)


def load(config_dir: Path | None = None, vault_override: Path | None = None) -> Config:
    d = Path(config_dir or CONFIG_DIR)
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
    )
