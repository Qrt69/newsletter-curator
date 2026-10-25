"""
Everything written into OSK, under <vault>/<map>/:

  Items/<titel>.md        one note per kept item, with properties for Bases
  Curator.base            the views (newest, per category, Om te gebruiken, Repo-review)
  Verworpen/<datum>.md    one list per run, a tick box per rejected item
  Runs/<datum>.md         a short summary per run
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f#^\[\]]')
_RID = re.compile(r"%%rid:([0-9a-f]{10})%%")
_TICKED = re.compile(r"^\s*-\s*\[[xX]\]\s")


def rid_for(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _q(v) -> str:
    """A YAML value: JSON strings are valid double-quoted YAML."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def one_line(s) -> str:
    return " ".join(str(s or "").split())


def slug(title: str) -> str:
    s = _BAD.sub(" ", title or "").strip()
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = (s or "Zonder titel")[:90].rstrip(" .")
    if s.split(".")[0].strip().upper() in _RESERVED:  # Windows device names
        s = s + " (notitie)"
    return s


class Vault:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root
        self.items = self.root / "Items"
        self.rejected_dir = self.root / "Verworpen"
        self.runs_dir = self.root / "Runs"

    def prepare(self):
        for d in (self.root, self.items, self.rejected_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)
        base = self.root / "Curator.base"
        if not base.exists():  # never overwrite views Kurt may have changed
            base.write_text(BASE_FILE.replace("{folder}", self.cfg.folder), encoding="utf-8")

    # ── duplicates already in the vault ─────────────────────────
    def known_keys(self) -> set[str]:
        keys = set()
        if not self.items.exists():
            return keys
        for p in self.items.rglob("*.md"):
            try:
                head = p.read_text(encoding="utf-8", errors="ignore")[:3000]
            except OSError:
                continue
            m = re.search(r'^sleutel:\s*"?([^"\n]+)"?\s*$', head, re.M)
            if m:
                keys.add(m.group(1).strip())
        return keys

    # ── kept items ──────────────────────────────────────────────
    def write_item(self, *, key: str, url: str, answers: dict, source: str, run_date: str,
                   points: float, topper: bool, novelty: str = "", repo_meta: dict | None = None,
                   upgraded: bool = False) -> Path:
        cat_key = answers["category"]
        category = self.cfg.categories.get(cat_key, cat_key)
        name = slug(answers["title"])
        path = self.items / f"{name}.md"
        for candidate in (path, self.items / f"{name} ({rid_for(key)[:6]}).md"):
            if candidate.exists() and f"sleutel: {_q(key)}" in candidate.read_text(encoding="utf-8", errors="ignore")[:3000]:
                return candidate  # already written: never overwrite Kurt's or the review's edits
        if path.exists():
            path = self.items / f"{name} ({rid_for(key)[:6]}).md"
        props = {
            "titel": answers["title"],
            "url": url,
            "sleutel": key,
            "bron": source,
            "datum": run_date,
            "categorie": category,
            "soort": answers.get("kind", "other"),
            "onderwerp": answers.get("topic", ""),
            "topper": topper,
            "bruikbaar": answers.get("usable", ""),
            "past": answers.get("fits", ""),
            "rommel": answers.get("junk", ""),
            "fit": int(answers.get("fit", 1)),
            "punten": points,
            "reden": answers.get("reason_nl", ""),
        }
        if novelty:
            props["voegt_toe"] = novelty
        if answers.get("new_category"):
            props["nieuwe_categorie"] = answers["new_category"]
        if upgraded:
            props["opgewaardeerd"] = True
        if answers.get("kind") == "repo" or key.startswith("github.com/"):
            props["soort"] = "repo"
            if repo_meta:
                props["sterren"] = repo_meta.get("stars", 0)
                props["dagen_stil"] = repo_meta.get("days_idle", 0)
                if repo_meta.get("license"):
                    props["licentie"] = repo_meta["license"]
            props["repo_oordeel"] = ""
            props["repo_reden"] = ""
        lines = ["---"] + [f"{k}: {_q(v)}" for k, v in props.items()] + ["tags:", "  - curator", "---", ""]
        lines.append(f"# {one_line(answers['title'])}")
        lines.append("")
        if answers.get("description_nl"):
            lines.append(answers["description_nl"])
            lines.append("")
        lines.append(f"[Open de bron]({url})")
        if novelty:
            lines += ["", f"**Voegt toe:** {novelty}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # ── rejected lists ──────────────────────────────────────────
    def rejected_path(self, run_date: str) -> Path:
        return self.rejected_dir / f"{run_date}.md"

    def write_rejected(self, run_date: str, entries: list[dict]):
        """entries: {rid, title, url, reason, source}. Rewritten whole on a resumed run."""
        path = self.rejected_path(run_date)
        ticked_before = set()
        if path.exists():  # keep ticks Kurt already set on a list from earlier today
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _RID.search(line)
                if m and _TICKED.match(line):
                    ticked_before.add(m.group(1))
        by_reason: dict[str, list[dict]] = {}
        for e in entries:
            by_reason.setdefault(e["reason"], []).append(e)
        out = [
            "---", f"datum: {run_date}", "tags:", "  - curator/verworpen", "---", "",
            f"# Verworpen op {run_date}", "",
            "Vink aan wat je toch wil bewaren; de volgende run zet het in de juiste categorie.", "",
        ]
        for reason in sorted(by_reason, key=lambda r: (-len(by_reason[r]), r)):
            out.append(f"## {reason} ({len(by_reason[reason])})")
            out.append("")
            for e in by_reason[reason]:
                box = "x" if (e["rid"] in ticked_before or e.get("upgraded")) else " "
                title = one_line(e["title"]).replace("[", "(").replace("]", ")")
                url = one_line(e["url"]).replace(" ", "%20").replace(")", "%29")
                src = f" · {one_line(e['source'])}" if e.get("source") else ""
                why = f" — {one_line(e['why'])}" if e.get("why") else ""
                done = " · opgewaardeerd" if e.get("upgraded") else ""
                out.append(f"- [{box}] [{title}]({url}){why}{src}{done} %%rid:{e['rid']}%%")
            out.append("")
        path.write_text("\n".join(out), encoding="utf-8")

    def ticked(self) -> list[tuple[Path, str]]:
        """Ticked lines not yet upgraded: (file, rid)."""
        found = []
        if not self.rejected_dir.exists():
            return found
        for p in sorted(self.rejected_dir.glob("*.md")):
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if _TICKED.match(line) and "opgewaardeerd" not in line:
                    m = _RID.search(line)
                    if m:
                        found.append((p, m.group(1)))
        return found

    def mark_upgraded(self, path: Path, rids: set[str]):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = _RID.search(line)
            if m and m.group(1) in rids and "opgewaardeerd" not in line:
                lines[i] = line.replace(m.group(0), f"· opgewaardeerd {m.group(0)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def cleanup_rejected(self, today: date, keep_days: int) -> int:
        cutoff = today - timedelta(days=keep_days)
        removed = 0
        if not self.rejected_dir.exists():
            return 0
        for p in self.rejected_dir.glob("*.md"):
            try:
                d = datetime.strptime(p.stem[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                p.unlink()
                removed += 1
        return removed

    # ── run summary ─────────────────────────────────────────────
    def write_summary(self, run_date: str, text: str) -> Path:
        path = self.runs_dir / f"{run_date}.md"
        n = 2
        while path.exists():
            path = self.runs_dir / f"{run_date} ({n}).md"
            n += 1
        path.write_text(text, encoding="utf-8")
        return path


BASE_FILE = """filters:
  and:
    - file.inFolder("{folder}/Items")
properties:
  note.titel:
    displayName: Titel
  note.categorie:
    displayName: Categorie
  note.bron:
    displayName: Bron
  note.datum:
    displayName: Datum
  note.onderwerp:
    displayName: Onderwerp
  note.voegt_toe:
    displayName: Voegt toe
  note.repo_oordeel:
    displayName: Oordeel Claude
  note.repo_reden:
    displayName: Reden
views:
  - type: table
    name: Nieuwste
    order:
      - file.name
      - categorie
      - bron
      - datum
      - reden
    sort:
      - property: note.datum
        direction: DESC
  - type: table
    name: Per categorie
    groupBy:
      property: note.categorie
      direction: ASC
    order:
      - file.name
      - bron
      - datum
    sort:
      - property: note.datum
        direction: DESC
  - type: table
    name: Om te gebruiken
    filters:
      and:
        - topper == true
    order:
      - file.name
      - categorie
      - datum
      - reden
    sort:
      - property: note.datum
        direction: DESC
  - type: table
    name: Per onderwerp
    filters:
      and:
        - onderwerp != ""
    groupBy:
      property: note.onderwerp
      direction: ASC
    order:
      - file.name
      - bron
      - voegt_toe
      - datum
  - type: table
    name: Repo-review
    filters:
      and:
        - soort == "repo"
    order:
      - file.name
      - sterren
      - dagen_stil
      - repo_oordeel
      - repo_reden
      - datum
    sort:
      - property: note.datum
        direction: DESC
"""
