"""
Notes that tie the items together, so Obsidian's links, backlinks and graph work:

  Categorieën/<categorie>.md   one per category; its embedded Base lists every item linking to it
  Onderwerpen/<onderwerp>.md   one per subject that at least two items share
  Curator.md                   start note: categories, shared subjects, latest runs (rewritten each run)

Item notes link to their category (and shared subject) through full-path links,
so a note of Kurt's own with the same name is never picked up by mistake.
Category and subject notes are written once and never overwritten; Kurt may add
his own text to them.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

CAT_DIR = "Categorieën"
TOPIC_DIR = "Onderwerpen"
MIN_TOPIC_ITEMS = 2

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _slug(name: str) -> str:
    from .vault import slug
    return slug(name)


def link(folder: str, sub: str, name: str) -> str:
    return f"[[{folder}/{sub}/{_slug(name)}|{name}]]"


def plain(value: str) -> str:
    """'[[Curator/Categorieën/AI-tools|AI-tools]]' -> 'AI-tools'; plain text stays as it is."""
    v = (value or "").strip()
    m = re.fullmatch(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", v)
    if not m:
        return v
    return (m.group(2) or m.group(1).rsplit("/", 1)[-1]).strip()


def read_props(text: str) -> dict[str, str]:
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        return {}
    props = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "-")):
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.strip('"')
        props[k.strip()] = v
    return props


def set_prop(text: str, name: str, value: str) -> str:
    line = f"{name}: {json.dumps(value, ensure_ascii=False)}"
    pat = re.compile(rf"^{re.escape(name)}:.*$", re.M)
    end = text.find("\n---", 4)
    head, rest = text[:end], text[end:]
    if pat.search(head):
        head = pat.sub(lambda _: line, head, count=1)
    else:
        head = head + "\n" + line
    return head + rest


HUB_NOTE = """---
tags:
  - curator/{tag}
---

# {name}

Alle curator-items {what}. Deze lijst werkt zichzelf bij.

```base
filters:
  and:
    - file.inFolder("{folder}/Items")
    - file.hasLink(this.file)
views:
  - type: table
    name: {name_yaml}
    order:
      - file.name
      - {extra}
      - bron
      - datum
    sort:
      - property: note.datum
        direction: DESC
```
"""


def _ensure_hub(path: Path, folder: str, name: str, tag: str, what: str, extra: str):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HUB_NOTE.format(tag=tag, name=name, what=what, folder=folder,
                                    name_yaml=json.dumps(name, ensure_ascii=False), extra=extra),
                    encoding="utf-8")


def refresh(root: Path, folder: str) -> dict:
    """Link items to category and subject notes, create missing hubs, rewrite the start note."""
    items_dir = root / "Items"
    cats: Counter = Counter()
    topics: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    notes = []
    for p in sorted(items_dir.glob("*.md")) if items_dir.exists() else []:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        props = read_props(text)
        notes.append((p, text, props))
        cat = plain(props.get("categorie", ""))
        if cat:
            cats[cat] += 1
        topic = plain(props.get("onderwerp", ""))
        if topic:
            topics[topic.lower()].append((p, topic))

    shared = {k: v for k, v in topics.items() if len({p for p, _ in v}) >= MIN_TOPIC_ITEMS}
    shared_names = {k: v[0][1] for k, v in shared.items()}

    for p, text, props in notes:
        new = text
        cat = plain(props.get("categorie", ""))
        if cat:
            want = link(folder, CAT_DIR, cat)
            if props.get("categorie") != want:
                new = set_prop(new, "categorie", want)
            _ensure_hub(root / CAT_DIR / f"{_slug(cat)}.md", folder, cat, "categorie",
                        f"in de categorie {cat}", "reden")
        topic = plain(props.get("onderwerp", ""))
        if topic and topic.lower() in shared_names:
            name = shared_names[topic.lower()]
            want = link(folder, TOPIC_DIR, name)
            if props.get("onderwerp") != want:
                new = set_prop(new, "onderwerp", want)
            _ensure_hub(root / TOPIC_DIR / f"{_slug(name)}.md", folder, name, "onderwerp",
                        f"over {name}", "voegt_toe")
        if new != text:
            p.write_text(new, encoding="utf-8")

    _write_start(root, folder, cats, {shared_names[k]: len(v) for k, v in shared.items()})
    return {"categorieen": len(cats), "onderwerpen": len(shared), "items": len(notes)}


def _write_start(root: Path, folder: str, cats: Counter, shared: dict[str, int]):
    total = sum(cats.values())
    lines = ["---", "tags:", "  - curator", "---", "", "# Curator", "",
             "*Deze notitie wordt na elke run opnieuw gemaakt; eigen tekst hier gaat verloren.*", "",
             f"**{total} items bewaard.** Volledig overzicht met alle weergaven: [[{folder}/Curator.base|Curator.base]]",
             "", "## Categorieën", ""]
    for name, n in sorted(cats.items(), key=lambda x: (-x[1], x[0].lower())):
        lines.append(f"- {link(folder, CAT_DIR, name)} ({n})")
    if shared:
        lines += ["", "## Onderwerpen met meerdere artikels", ""]
        for name, n in sorted(shared.items(), key=lambda x: (-x[1], x[0].lower())):
            lines.append(f"- {link(folder, TOPIC_DIR, name)} ({n})")
    for title, sub in (("Laatste runs", "Runs"), ("Verworpen", "Verworpen")):
        files = sorted((root / sub).glob("*.md"), reverse=True)[:5] if (root / sub).exists() else []
        if files:
            lines += ["", f"## {title}", ""]
            lines += [f"- [[{folder}/{sub}/{f.stem}|{f.stem}]]" for f in files]
    (root / "Curator.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
