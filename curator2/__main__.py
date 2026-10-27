"""
Start a run:   uv run python -m curator2
Options:       --vault <map>   schrijf naar een andere map (bijvoorbeeld om te testen)
               --geen-mails-verplaatsen   laat de mails in 'to qualify' staan
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curator2 import config as config_mod  # noqa: E402
from curator2.github import GitHub  # noqa: E402
from curator2.judge import Judge  # noqa: E402
from curator2.pipeline import run  # noqa: E402
from curator2.state import State  # noqa: E402
from curator2.vault import Vault  # noqa: E402


def data_dir() -> Path:
    base = os.environ.get("CURATOR2_DATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "curator2"


class _KeepMail:
    """Wraps a source so emails are never moved (for a trial run)."""

    def __init__(self, src):
        self.src = src

    async def fetch(self):
        return await self.src.fetch()

    def extract(self, e):
        return self.src.extract(e)

    async def move(self, eid):
        return None

    def close(self):
        self.src.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="curator2")
    ap.add_argument("--vault", help="andere vault-map")
    ap.add_argument("--geen-mails-verplaatsen", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(d / "curator2.log", encoding="utf-8")],
    )
    log = logging.getLogger("curator2")
    for noisy in ("azure", "httpx", "httpcore", "msal", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def progress(msg: str):
        print(msg, flush=True)
        log.info(msg)

    cfg = config_mod.load(vault_override=Path(args.vault) if args.vault else None)
    if not cfg.vault.exists():
        progress(f"De vault {cfg.vault} bestaat niet. Pas instellingen.toml aan.")
        return 2

    from curator2.mailsource import OutlookSource
    source = OutlookSource()
    if args.geen_mails_verplaatsen:
        source = _KeepMail(source)
    state = State(d / "curator2.sqlite")
    if cfg.judge_kind == "claude":
        from curator2.claude_judge import ClaudeJudge
        from curator2.judge import LLMUnavailable
        try:
            judge = ClaudeJudge(cfg)
        except LLMUnavailable as exc:
            progress(f"Claude kan niet gebruikt worden: {exc}")
            return 3
    else:
        judge = Judge(cfg)
    gh = GitHub()
    try:
        result = asyncio.run(run(cfg, source, judge, state, Vault(cfg), github=gh, progress=progress))
    except Exception:
        log.exception("Run crashed")
        progress("De run is gecrasht; details staan in " + str(d / "curator2.log") +
                 ". De volgende run gaat verder waar deze stopte.")
        return 1
    finally:
        source.close()
        judge.close()
        gh.close()
        state.close()
    if result.message:
        progress(result.message)
    return 0 if result.status in ("klaar", "geen mails") else 3


if __name__ == "__main__":
    sys.exit(main())
