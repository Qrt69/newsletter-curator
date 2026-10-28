"""
The same fixed questions as judge.py, answered by Claude instead of LM Studio.

- All items of a run go out as one batch: half the normal price, and no limit
  on questions per minute. Results usually come back within minutes.
- Every answer's token count is turned into dollars; the pipeline keeps the
  monthly total and refuses to start a batch that would exceed the monthly cap.
- No temperature is sent: current Claude models reject it.
"""

from __future__ import annotations

import logging
import threading
import time

import anthropic
import httpx

from .config import Config
from .judge import (KINDS, NOVELTY_SYSTEM, NOVELTY_USER, SYSTEM, USER, LLMUnavailable,
                    _NOVELTY_SCHEMA, _pick, _safe, _schema, category_lines, clean_answers, YN)

log = logging.getLogger("curator2.claude")

REQUIRED = ("category", "usable", "fits", "junk", "fit")


class BudgetExceeded(RuntimeError):
    pass


class ClaudeJudge:
    def __init__(self, cfg: Config, http_client: httpx.Client | None = None, poll_seconds: int = 30):
        self.cfg = cfg
        self.categories = list(cfg.categories.keys())
        self.model = cfg.claude_model
        self.poll_seconds = poll_seconds
        kwargs = {"max_retries": 4}
        if http_client is not None:
            kwargs["http_client"] = http_client
        try:
            self.client = anthropic.Anthropic(**kwargs)
        except anthropic.AnthropicError as exc:  # no API key
            raise LLMUnavailable(f"geen Claude-sleutel gevonden ({exc})") from exc
        self.cost = 0.0           # dollars spent by this run
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.failures = 0
        self._lock = threading.Lock()

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    # ── money ───────────────────────────────────────────────────
    def _add_usage(self, usage, batch: bool):
        if usage is None:
            return
        with self._lock:
            self._add_usage_locked(usage, batch)

    def _add_usage_locked(self, usage, batch: bool):
        inp = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        self.input_tokens += inp + cached
        self.output_tokens += out
        factor = self.cfg.claude_batch_discount if batch else 1.0
        dollars = (inp * self.cfg.claude_price_in + cached * self.cfg.claude_price_in * 0.1
                   + out * self.cfg.claude_price_out) / 1_000_000 * factor
        self.cost += dollars

    def estimate(self, n_items: int) -> float:
        """Rough dollars for judging n items in a batch (about 1,800 tokens in, 250 out each)."""
        per = (1800 * self.cfg.claude_price_in + 250 * self.cfg.claude_price_out) / 1_000_000
        factor = self.cfg.claude_batch_discount if self.cfg.claude_batch else 1.0
        return n_items * per * factor

    # ── requests ────────────────────────────────────────────────
    def _item_params(self, item: dict) -> dict:
        text = (item.get("text") or "")[: self.cfg.max_text] or \
            "[geen artikeltekst beschikbaar — oordeel op titel, link en linktekst]"
        system = SYSTEM.format(profile=self.cfg.profile, categories=category_lines(self.cfg),
                               kinds=", ".join(KINDS))
        user = USER.format(
            url=item.get("resolved_url") or item.get("source_url") or "",
            link_text=item.get("link_text") or "", title=item.get("title") or "",
            site=item.get("sitename") or item.get("hostname") or "",
            description=item.get("description") or "", n=self.cfg.max_text, text=text)
        return {
            "model": self.model,
            "max_tokens": 700,
            # the long, identical instructions are cached: later items pay a tenth for them
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "answers", "description": "Your answers about the item.",
                       "input_schema": _schema(self.categories)}],
            "tool_choice": {"type": "tool", "name": "answers"},
        }

    @staticmethod
    def _answers_of(message) -> dict | None:
        for block in getattr(message, "content", []) or []:
            if getattr(block, "type", "") == "tool_use":
                data = block.input if isinstance(block.input, dict) else None
                if data and all(k in data for k in REQUIRED):
                    return data
        return None

    # ── server ──────────────────────────────────────────────────
    def ensure_ready(self) -> str:
        """One tiny call: proves the key works and the model exists before anything else happens."""
        try:
            r = self.client.messages.create(model=self.model, max_tokens=5,
                                            messages=[{"role": "user", "content": "OK"}])
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("de Claude-sleutel wordt geweigerd") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailable(f"model '{self.model}' bestaat niet") from exc
        except anthropic.BadRequestError as exc:
            msg = str(exc)
            if "credit" in msg.lower() or "balance" in msg.lower():
                raise LLMUnavailable("geen tegoed meer op de Claude-rekening") from exc
            raise LLMUnavailable(f"Claude weigert de testvraag: {msg[:200]}") from exc
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            raise LLMUnavailable(f"Claude niet bereikbaar: {exc}") from exc
        self._add_usage(r.usage, batch=False)
        return self.model

    # ── one item at a time (upgrades, or when batches are switched off) ──
    def judge(self, item: dict) -> dict | None:
        self.calls += 1
        try:
            r = self.client.messages.create(**self._item_params(item))
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            log.warning("Claude call failed: %s", exc)
            self.failures += 1
            return None
        self._add_usage(r.usage, batch=False)
        a = self._answers_of(r)
        if a is None:
            self.failures += 1
            return None
        return clean_answers(a, self.categories, item)

    # ── a whole run in one batch ────────────────────────────────
    def judge_many(self, items: dict[str, dict], progress=print) -> dict[str, dict | None]:
        """items: {key: item}. Returns {key: answers or None}."""
        if not items:
            return {}
        if not self.cfg.claude_batch:
            return self._judge_parallel(items, progress)
        ids = {f"i{n}": k for n, k in enumerate(items)}
        requests = [{"custom_id": cid, "params": self._item_params(items[k])} for cid, k in ids.items()]
        try:
            batch = self.client.messages.batches.create(requests=requests)
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"Claude weigert het pakket: {str(exc)[:200]}") from exc
        progress(f"{len(requests)} items naar Claude gestuurd; wachten op de antwoorden...")
        deadline = time.time() + self.cfg.claude_max_wait_minutes * 60
        last_note = 0.0
        while True:
            try:
                batch = self.client.messages.batches.retrieve(batch.id)
            except anthropic.APIConnectionError:
                batch = batch  # keep waiting; a dropped connection is not the end of the batch
            if getattr(batch, "processing_status", "") == "ended":
                break
            if time.time() > deadline:
                try:
                    self.client.messages.batches.cancel(batch.id)
                except Exception:
                    pass
                raise LLMUnavailable("Claude antwoordde niet binnen de wachttijd; de run gaat de volgende keer verder")
            if time.time() - last_note > 60:
                rc = getattr(batch, "request_counts", None)
                done = getattr(rc, "succeeded", 0) + getattr(rc, "errored", 0) if rc else 0
                progress(f"  Claude is bezig ({done}/{len(requests)} beantwoord)")
                last_note = time.time()
            time.sleep(self.poll_seconds)

        out: dict[str, dict | None] = {k: None for k in items}
        for entry in self.client.messages.batches.results(batch.id):
            key = ids.get(entry.custom_id)
            if key is None:
                continue
            self.calls += 1
            res = entry.result
            if getattr(res, "type", "") != "succeeded":
                self.failures += 1
                continue
            self._add_usage(res.message.usage, batch=True)
            a = self._answers_of(res.message)
            if a is None:
                self.failures += 1
                continue
            out[key] = clean_answers(a, self.categories, items[key])
        return out

    def _judge_parallel(self, items: dict[str, dict], progress) -> dict[str, dict | None]:
        """Ask Claude item by item, a few at a time, reporting progress every 10 items."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        out: dict[str, dict | None] = {}
        total = len(items)
        t0 = time.time()
        progress(f"Claude beoordeelt {total} items, vier tegelijk...")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.judge, it): k for k, it in items.items()}
            try:
                for n, fut in enumerate(as_completed(futures), 1):
                    out[futures[fut]] = fut.result()
                    if n % 10 == 0 or n == total:
                        left = (time.time() - t0) / n * (total - n)
                        progress(f"  {n}/{total} beoordeeld, nog ongeveer {left / 60:.0f} min, "
                                 f"kost tot nu ${self.cost:.3f}")
            except LLMUnavailable:
                for f in futures:
                    f.cancel()
                raise
        return out

    def novelty(self, topic: str, earlier: list[dict], title: str, text: str) -> dict | None:
        lines = "\n".join(f"- {e['title']}: {e['summary']}" for e in earlier[-6:])
        user = NOVELTY_USER.format(topic=topic, earlier=lines, title=title, text=(text or "")[: self.cfg.max_text])
        self.calls += 1
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=200, system=NOVELTY_SYSTEM,
                messages=[{"role": "user", "content": user}],
                tools=[{"name": "novelty", "description": "Your verdict.", "input_schema": _NOVELTY_SCHEMA}],
                tool_choice={"type": "tool", "name": "novelty"})
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(str(exc)) from exc
        except anthropic.APIStatusError:
            self.failures += 1
            return None
        self._add_usage(r.usage, batch=False)
        for block in r.content:
            if getattr(block, "type", "") == "tool_use" and isinstance(block.input, dict):
                a = block.input
                return {"adds": _pick(a.get("adds"), YN, "ja"), "what_nl": _safe(a.get("what_nl"))[:300]}
        return None
