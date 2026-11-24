"""
Start a run:   uv run python -m curator2
Options:       --vault <map>   schrijf naar een andere map (bijvoorbeeld om te testen)
               --geen-mails-verplaatsen   laat de mails in 'to qualify' staan
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from curator2 import config as config_mod  # noqa: E402
from curator2.github import GitHub  # noqa: E402
from curator2.judge import Judge  # noqa: E402
from curator2.net import internet_ok  # noqa: E402
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


# The daily run has a window: from 20:00 until 08:00 the next morning. The scheduled
# task starts at 20:00, or as soon as the laptop is on, and repeats every hour; the
# first run that works in the window is that day's run. Outside the window a
# scheduled start does nothing (Windows also catches up missed starts the next
# afternoon): that day simply did not happen, and the mails wait for the next window.
WINDOW_START = 20
WINDOW_END = 8


def window_start(now: datetime) -> datetime | None:
    """Start of the run window that `now` falls in, or None outside the window."""
    if now.hour >= WINDOW_START:
        return now.replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)
    if now.hour < WINDOW_END:
        return (now - timedelta(days=1)).replace(hour=WINDOW_START, minute=0, second=0, microsecond=0)
    return None


def skip_scheduled(st: "State", now: datetime) -> str:
    """Why the scheduled run (--avond) should do nothing now; empty string: go ahead."""
    start = window_start(now)
    if start is None:
        return (f"Geplande run: buiten het venster ({WINDOW_START}:00 tot {WINDOW_END}:00), "
                "niets gedaan; de volgende kans is om 20:00")
    if st.finished_since(start.timestamp()):
        return "Geplande run: in dit venster al gelukt"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(prog="curator2")
    ap.add_argument("--vault", help="andere vault-map")
    ap.add_argument("--geen-mails-verplaatsen", action="store_true")
    ap.add_argument("--import", dest="import_file",
                    help="eenmalige import: items uit dit JSON-bestand in plaats van mails")
    ap.add_argument("--avond", action="store_true",
                    help="geplande avondrun: niets doen als de run van vanavond al gelukt is")
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
    lock = d / "curator2.lock"
    if not _take_lock(lock):
        progress("Er draait al een curator-run; deze start doet niets.")
        return 0
    try:
        return _main(args, cfg, d, log, progress)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _take_lock(lock: Path, stale_hours: float = 7) -> bool:
    """One run at a time. A lock older than the longest possible run is left over from a crash."""
    import time
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_hours * 3600:
                    lock.unlink()
                    continue
            except OSError:
                continue
            return False
    return False


def _main(args, cfg, d: Path, log, progress) -> int:
    if args.avond:
        st = State(d / "curator2.sqlite")
        try:
            # Only an evening run counts: a run Kurt starts himself during the day
            # (to catch up) does not replace the evening run.
            why = skip_scheduled(st, datetime.now())
            if why:
                log.info(why)
                return 0
        finally:
            st.close()
    if not cfg.vault.exists():
        progress(f"De vault {cfg.vault} is niet bereikbaar (externe schijf niet aangesloten?). "
                 "De geplande run probeert het elk uur opnieuw tot 8 uur 's morgens.")
        return 2
    if not (cfg.vault / ".obsidian").is_dir():
        progress(f"{cfg.vault} is geen Obsidian-vault (er staat geen .obsidian-map in). "
                 "Pas 'vault' aan in instellingen.toml.")
        return 2

    if args.import_file:
        from curator2.mailsource import FileSource
        source = FileSource(args.import_file)
    else:
        from curator2.mailsource import OutlookSource
        source = OutlookSource()
    if args.geen_mails_verplaatsen:
        source = _KeepMail(source)
    state = State(d / "curator2.sqlite")
    busy = state.unfinished_run()
    # An interrupted import is resumed by the same import command; a newsletter run is not.
    if not args.import_file and busy and "import" in busy:
        progress(f"De import '{busy}' is onderbroken. Start ze opnieuw met hetzelfde import-commando; "
                 "de gewone run wacht tot ze klaar is.")
        state.close()
        return 3
    if args.import_file and busy and not busy.endswith(Path(args.import_file).stem):
        progress("Er staat nog een onderbroken run. Start eerst gewoon 'uv run python -m curator2' "
                 "zodat die afgewerkt wordt, en daarna de import.")
        state.close()
        return 3
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
        label = f"{date.today().isoformat()} {Path(args.import_file).stem}" if args.import_file else ""
        if args.import_file and busy:
            label = busy
        result = asyncio.run(run(cfg, source, judge, state, Vault(cfg), github=gh, progress=progress,
                                 online=internet_ok, label=label))
        if args.import_file:
            state.mark_import(state.last_run_id())
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
