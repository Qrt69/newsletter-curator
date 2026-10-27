"""Claude as the judge: one batch per run, costs booked, monthly cap respected."""
import asyncio
import json
from datetime import date

import httpx

from curator2.claude_judge import ClaudeJudge
from curator2.mailsource import FakeSource
from curator2.pipeline import run
from curator2.state import State
from curator2.vault import Vault

from .conftest import DEFAULT, FakeGitHub
from .test_pipeline import answers, build_mail


class FakeClaude:
    """Speaks just enough of the Messages and Batches API for the curator."""

    def __init__(self, answers_by_linktext, novelty=None, fail_keys=()):
        self.answers = answers_by_linktext
        self.novelty = novelty or {}
        self.fail = set(fail_keys)
        self.batches = {}
        self.batch_creates = 0
        self.sync_calls = 0
        self.sent_temperature = False

    def _tool_message(self, name, data, tin=1500, tout=200):
        return {"id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                "stop_reason": "tool_use", "stop_sequence": None,
                "content": [{"type": "tool_use", "id": "t1", "name": name, "input": data}],
                "usage": {"input_tokens": tin, "output_tokens": tout}}

    def _answer(self, params):
        user = params["messages"][0]["content"]
        lt = user.split("Link text in the newsletter: ", 1)[1].split("\n", 1)[0]
        title = user.split("Title: ", 1)[1].split("\n", 1)[0]
        return {**DEFAULT, "title": title or lt, **self.answers.get(lt, {})}, lt

    def handler(self, request: httpx.Request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages":
            self.sync_calls += 1
            body = json.loads(request.content)
            self.sent_temperature |= "temperature" in body
            if "tools" not in body:  # ensure_ready
                return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant",
                                                 "model": body["model"], "stop_reason": "end_turn",
                                                 "stop_sequence": None, "content": [{"type": "text", "text": "OK"}],
                                                 "usage": {"input_tokens": 8, "output_tokens": 2}})
            if body["tools"][0]["name"] == "novelty":
                title = body["messages"][0]["content"].split("New article: ", 1)[1].split("\n", 1)[0]
                adds = self.novelty.get(title, "ja")
                return httpx.Response(200, json=self._tool_message("novelty", {"adds": adds, "what_nl": "x"}, 500, 30))
            data, _ = self._answer(body)
            return httpx.Response(200, json=self._tool_message("answers", data))
        if request.method == "POST" and path == "/v1/messages/batches":
            self.batch_creates += 1
            body = json.loads(request.content)
            for r in body["requests"]:
                self.sent_temperature |= "temperature" in r["params"]
            bid = f"b{self.batch_creates}"
            self.batches[bid] = body["requests"]
            return httpx.Response(200, json=self._batch(bid, "in_progress"))
        if request.method == "GET" and path.startswith("/v1/messages/batches/") and path.endswith("/results"):
            bid = path.split("/")[4]
            lines = []
            for r in self.batches[bid]:
                data, lt = self._answer(r["params"])
                if lt in self.fail:
                    lines.append({"custom_id": r["custom_id"], "result": {"type": "errored",
                                  "error": {"type": "error", "error": {"type": "api_error", "message": "x"}}}})
                else:
                    lines.append({"custom_id": r["custom_id"], "result": {"type": "succeeded",
                                  "message": self._tool_message("answers", data)}})
            return httpx.Response(200, text="\n".join(json.dumps(l) for l in lines),
                                  headers={"content-type": "application/x-jsonl"})
        if request.method == "GET" and path.startswith("/v1/messages/batches/"):
            bid = path.split("/")[4]
            return httpx.Response(200, json=self._batch(bid, "ended"))
        return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": path}})

    def _batch(self, bid, status):
        return {"id": bid, "type": "message_batch", "processing_status": status,
                "request_counts": {"processing": 0, "succeeded": len(self.batches[bid]), "errored": 0,
                                   "canceled": 0, "expired": 0},
                "created_at": "2026-10-27T20:00:00Z", "expires_at": "2026-10-28T20:00:00Z",
                "ended_at": None, "archived_at": None, "cancel_initiated_at": None,
                "results_url": f"https://api.anthropic.com/v1/messages/batches/{bid}/results"}


def make_claude(cfg, fake, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return ClaudeJudge(cfg, http_client=client, poll_seconds=0)


def go(cfg, tmp_path, judge, mails, today=date(2026, 10, 27)):
    state = State(tmp_path / "s.sqlite")
    gh = FakeGitHub({"big/tool": {"stars": 900, "days_idle": 2, "archived": False, "license": "MIT"}})
    src = FakeSource(mails)
    res = asyncio.run(run(cfg, src, judge, state, Vault(cfg), github=gh, today=today, progress=lambda m: None))
    return res, state, src


def test_whole_run_in_one_batch_with_costs(cfg, tmp_path, monkeypatch):
    fake = FakeClaude(answers(), novelty={"Qwen9 B": "nee"})
    judge = make_claude(cfg, fake, monkeypatch)
    res, state, src = go(cfg, tmp_path, judge, build_mail())
    assert res.status == "klaar", res.message
    assert fake.batch_creates == 1
    assert not fake.sent_temperature
    c = res.counts
    assert c["toppers"] == 1 and c["repos"] == 1 and c["gewoon_bewaard"] == 3
    assert c["kosten_run"] > 0 and c["kosten_maand"] == c["kosten_run"]
    summary = next((cfg.root / "Runs").glob("*.md")).read_text(encoding="utf-8")
    assert "Kosten Claude" in summary
    # 25 items in a batch at half price, roughly 1500 in / 200 out each
    expected = 25 * (1500 * 0.10 + 200 * 0.50) / 1e6 * 0.5
    assert abs(c["kosten_run"] - expected) < 0.01
    assert src.moved == ["m1"]


def test_monthly_cap_stops_before_spending(cfg, tmp_path, monkeypatch):
    fake = FakeClaude(answers())
    judge = make_claude(cfg, fake, monkeypatch)
    state = State(tmp_path / "s.sqlite")
    state.add_cost("2026-10", 0, cfg.claude_monthly_cap - 0.0001, 0, 0)
    state.close()
    from curator2.pipeline import date as pdate  # the cap uses the real month
    month = pdate.today().strftime("%Y-%m")
    state = State(tmp_path / "s.sqlite")
    state.add_cost(month, 0, cfg.claude_monthly_cap, 0, 0)
    state.close()
    res, state, src = go(cfg, tmp_path, judge, build_mail())
    assert res.status == "plafond"
    assert fake.batch_creates == 0 and src.moved == []
    assert any("Maandplafond" in p.read_text(encoding="utf-8") for p in (cfg.root / "Runs").glob("*.md"))


def test_failed_batch_items_fall_back_to_single_calls(cfg, tmp_path, monkeypatch):
    fake = FakeClaude(answers(), fail_keys={"Article number 18 about things"})
    judge = make_claude(cfg, fake, monkeypatch)
    res, state, src = go(cfg, tmp_path, judge, build_mail())
    assert res.status == "klaar"
    assert res.counts["niet_beoordeeld"] == 0   # retried one by one and answered


def test_wrong_key_is_reported(cfg, tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                      "message": "invalid x-api-key"}})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad")
    judge = ClaudeJudge(cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)), poll_seconds=0)
    res, state, src = go(cfg, tmp_path, judge, build_mail())
    assert res.status == "onderbroken" and "sleutel" in res.message
    assert src.extract_calls == []
