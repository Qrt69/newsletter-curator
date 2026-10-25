"""
Ask the local model fixed questions about an item. The model never gives a
score: it answers yes/no/partly questions plus a 1-5 fit, and rank.py turns
those into points with the weights from beoordeling.toml.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger("curator2.judge")

YN = ["ja", "nee"]
YPN = ["ja", "deels", "nee"]
KINDS = ["repo", "local_model", "tool", "article", "announcement", "event", "other"]

SYSTEM = """You sort newsletter items for one reader. You never score with numbers \
except the fit from 1 to 5. Answer the questions about the item honestly; most \
items in a newsletter are NOT a fit, and saying so is the useful answer.

The reader:
{profile}

Answer in JSON with exactly these fields:
- title: a clean, short name for the item (product, library, model or article title).
- description_nl: two short sentences in Dutch saying what it is and what it is good for.
- category: one of {categories}. Use "event" for webinars, conferences, live sessions \
and other dated events.
- new_category: empty string, or a short Dutch name when none of the categories fits at all.
- kind: one of {kinds}. "repo" = a code repository, "local_model" = model weights \
someone can run locally, "announcement" = news or a launch without substance.
- topic: the main subject in 1-4 words (for example a model or product name), so \
articles about the same thing can be grouped. Empty string if there is no single subject.
- usable: "ja" if it is something one can actually use or apply (tool, library, repo, \
method with steps); "nee" if it is only news, opinion or a launch announcement.
- fits: "ja", "deels" or "nee" — does it match the reader's interests above?
- junk: "ja" if it is consumer fluff, a thin AI wrapper, marketing without substance, \
or far too basic; otherwise "nee".
- topper: "ja" only if it is a repo or local model the reader could install and use \
right away for his own work; otherwise "nee". This should be rare.
- fit: 1 to 5, how well it fits the reader overall (5 = exactly his kind of thing).
- reason_nl: one Dutch sentence explaining your answers."""

USER = """Newsletter item:

Link: {url}
Link text in the newsletter: {link_text}
Title: {title}
Site: {site}
Description: {description}

Article text (first {n} characters):
{text}"""

NOVELTY_SYSTEM = """You compare a new article with earlier articles about the same \
subject. Answer in JSON with:
- adds: "ja" if the new article contains real information that the earlier ones do \
not (new facts, own tests, benchmarks, a different angle with substance); "nee" if it \
mostly repeats them.
- what_nl: one short Dutch sentence: what it adds, or "niets nieuws"."""

NOVELTY_USER = """Subject: {topic}

Earlier articles:
{earlier}

New article: {title}
{text}"""


def _schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description_nl": {"type": "string"},
            "category": {"type": "string", "enum": categories},
            "new_category": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "topic": {"type": "string"},
            "usable": {"type": "string", "enum": YN},
            "fits": {"type": "string", "enum": YPN},
            "junk": {"type": "string", "enum": YN},
            "topper": {"type": "string", "enum": YN},
            "fit": {"type": "integer", "minimum": 1, "maximum": 5},
            "reason_nl": {"type": "string"},
        },
        "required": ["title", "description_nl", "category", "new_category", "kind", "topic",
                     "usable", "fits", "junk", "topper", "fit", "reason_nl"],
        "additionalProperties": False,
    }


_NOVELTY_SCHEMA = {
    "type": "object",
    "properties": {"adds": {"type": "string", "enum": YN}, "what_nl": {"type": "string"}},
    "required": ["adds", "what_nl"],
    "additionalProperties": False,
}


def _parse(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    v = None
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        try:
            from json_repair import repair_json
            v = repair_json(raw, return_objects=True)
        except Exception:
            v = None
    if isinstance(v, list) and len(v) == 1:
        v = v[0]
    return v if isinstance(v, dict) else None


def _safe(s) -> str:
    """Text that SQLite and the vault can store (broken emoji halves replaced)."""
    return " ".join(str(s or "").encode("utf-8", "replace").decode("utf-8").split())


def _pick(v, allowed: list[str], default: str) -> str:
    v = str(v or "").strip().lower()
    if v in allowed:
        return v
    if v in ("yes", "true"):
        return "ja" if "ja" in allowed else default
    if v in ("no", "false"):
        return "nee" if "nee" in allowed else default
    if v in ("partly", "partially", "gedeeltelijk"):
        return "deels" if "deels" in allowed else default
    return default


def clean_answers(a: dict, categories: list[str], item: dict) -> dict:
    """Coerce whatever the model returned into the expected shape."""
    cat = str(a.get("category") or "").strip().lower()
    if cat not in categories:
        cat = "article" if "article" in categories else categories[0]
    try:
        fit = int(a.get("fit"))
    except (TypeError, ValueError):
        fit = 1
    fit = max(1, min(5, fit))
    title = _safe(a.get("title")) or _safe(item.get("title") or item.get("link_text")) or "Zonder titel"
    new_cat = a.get("new_category")
    return {
        "title": title[:150],
        "description_nl": _safe(a.get("description_nl"))[:600],
        "category": cat,
        "new_category": (_safe(new_cat)[:60] or None) if new_cat else None,
        "kind": _pick(a.get("kind"), KINDS, "other"),
        "topic": _safe(a.get("topic"))[:60],
        "usable": _pick(a.get("usable"), YN, "nee"),
        "fits": _pick(a.get("fits"), YPN, "nee"),
        "junk": _pick(a.get("junk"), YN, "nee"),
        "topper": _pick(a.get("topper"), YN, "nee"),
        "fit": fit,
        "reason_nl": _safe(a.get("reason_nl"))[:400],
    }


class LLMUnavailable(RuntimeError):
    pass


class Judge:
    """Talks to LM Studio's OpenAI-compatible endpoint with plain httpx."""

    def __init__(self, cfg: Config, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.categories = list(cfg.categories.keys())
        self.client = httpx.Client(
            base_url=cfg.llm_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.llm_timeout, connect=5.0),
            transport=transport,
        )
        self.model = cfg.llm_model
        self._schema_ok = True
        self.calls = 0
        self.failures = 0

    def close(self):
        self.client.close()

    # ── server ──────────────────────────────────────────────────
    def models(self) -> list[str]:
        r = self.client.get("/models", timeout=5.0)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", []) if "embed" not in m["id"]]

    def ensure_ready(self, start_wait: int = 120):
        """Make sure LM Studio answers and the model is there; start it if we can."""
        try:
            available = self.models()
        except httpx.HTTPError:
            available = None
        if available is None:
            _start_lmstudio(self.cfg.llm_model)
            deadline = time.time() + start_wait
            while time.time() < deadline:
                time.sleep(5)
                try:
                    available = self.models()
                    break
                except httpx.HTTPError:
                    continue
        if available is None:
            raise LLMUnavailable("LM Studio antwoordt niet op " + self.cfg.llm_url)
        if self.model and self.model not in available:
            _start_lmstudio(self.model)  # loads it, if LM Studio's command-line tool is there
            try:
                available = self.models()
            except httpx.HTTPError:
                available = []
            if self.model not in available:
                raise LLMUnavailable(f"model '{self.model}' staat niet in LM Studio "
                                     f"(beschikbaar: {', '.join(available) or 'geen'})")
        if not self.model:
            if not available:
                raise LLMUnavailable("LM Studio heeft geen model geladen")
            self.model = available[0]
        return self.model

    # ── calls ───────────────────────────────────────────────────
    def _chat(self, system: str, user: str, schema: dict, name: str, max_tokens: int,
              required: tuple[str, ...]) -> dict | None:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if self._schema_ok:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }
        for attempt in range(3):
            self.calls += 1
            try:
                r = self.client.post("/chat/completions", json=body)
                if r.status_code == 400 and "response_format" in body and (
                        "response_format" in r.text or "json_schema" in r.text or "schema" in r.text.lower()):
                    # Server without structured output: ask for plain JSON instead.
                    self._schema_ok = False
                    body.pop("response_format")
                    continue
                r.raise_for_status()
                choice = r.json()["choices"][0]
                if choice.get("finish_reason") == "length":
                    log.warning("LLM answer cut off at %d tokens", max_tokens)
                    body["max_tokens"] = max_tokens * 2
                    continue
                parsed = _parse(choice["message"].get("content") or "")
                if parsed is not None and all(k in parsed for k in required):
                    return parsed
                log.warning("LLM answer incomplete or not JSON (attempt %d)", attempt + 1)
            except httpx.ConnectError as exc:
                raise LLMUnavailable(str(exc)) from exc
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
                log.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(1 + attempt)
        self.failures += 1
        return None

    def judge(self, item: dict) -> dict | None:
        text = (item.get("text") or "")[: self.cfg.max_text]
        if not text:
            text = "[geen artikeltekst beschikbaar — oordeel op titel, link en linktekst]"
        system = SYSTEM.format(
            profile=self.cfg.profile,
            categories=", ".join(self.categories),
            kinds=", ".join(KINDS),
        )
        user = USER.format(
            url=item.get("resolved_url") or item.get("source_url") or "",
            link_text=item.get("link_text") or "",
            title=item.get("title") or "",
            site=item.get("sitename") or item.get("hostname") or "",
            description=item.get("description") or "",
            n=self.cfg.max_text,
            text=text,
        )
        a = self._chat(system, user, _schema(self.categories), "item_answers", 600,
                       ("category", "usable", "fits", "junk", "fit"))
        return clean_answers(a, self.categories, item) if a is not None else None

    def novelty(self, topic: str, earlier: list[dict], title: str, text: str) -> dict | None:
        lines = "\n".join(f"- {e['title']}: {e['summary']}" for e in earlier[-6:])
        user = NOVELTY_USER.format(
            topic=topic, earlier=lines, title=title, text=(text or "")[: self.cfg.max_text]
        )
        a = self._chat(NOVELTY_SYSTEM, user, _NOVELTY_SCHEMA, "novelty", 200, ("adds",))
        if a is None:
            return None
        return {"adds": _pick(a.get("adds"), YN, "ja"), "what_nl": _safe(a.get("what_nl"))[:300]}


def _lms_path() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for p in (home / ".lmstudio" / "bin" / "lms.exe", home / ".lmstudio" / "bin" / "lms"):
        if p.exists():
            return str(p)
    return None


def _start_lmstudio(model: str):
    """Start LM Studio's server and load the model with its command-line tool, if installed."""
    lms = _lms_path()
    if not lms:
        log.warning("LM Studio command-line tool not found; cannot start LM Studio")
        return
    try:
        subprocess.run([lms, "server", "start"], timeout=60, capture_output=True)
        if model:
            subprocess.run([lms, "load", model, "-y"], timeout=180, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Starting LM Studio failed: %s", exc)
