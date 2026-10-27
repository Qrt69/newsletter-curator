"""Regression tests for the defects found in the independent review of 2026-10-25."""
import asyncio
import json
from datetime import date

import httpx

from curator2.judge import Judge, _parse
from curator2.mailsource import FakeSource
from curator2.state import State
from curator2.urls import github_repo, normalize
from curator2.vault import Vault, slug

from .conftest import FakeGitHub, FakeLLM, item
from .test_pipeline import TODAY, answers, build_mail, go, make, notes


def tick(path, needle):
    text = path.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if needle in l)
    path.write_text(text.replace(line, line.replace("- [ ]", "- [x]")), encoding="utf-8")


def test_upgraded_item_is_not_upgraded_again_after_rerender(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    go(cfg, state, judge, src, gh)
    lst = cfg.root / "Verworpen" / "2026-10-26.md"
    tick(lst, "Article 3")
    go(cfg, state, judge, FakeSource([]), gh, today=date(2026, 10, 27))
    note = cfg.root / "Items" / "Article 3.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nMijn eigen notitie.\n", encoding="utf-8")
    # a second run on 26 Oct re-renders that list from the database
    src2 = FakeSource([{"id": "m5", "subject": "x", "sender_name": "X", "items": [item(500)]}])
    go(cfg, state, judge, src2, gh, today=TODAY)
    assert "opgewaardeerd" in next(l for l in lst.read_text(encoding="utf-8").splitlines() if "Article 3" in l)
    res = go(cfg, state, judge, FakeSource([]), gh, today=date(2026, 10, 28))
    assert res.counts["opgewaardeerd"] == 0
    assert "Mijn eigen notitie." in note.read_text(encoding="utf-8")


def test_interrupted_while_writing_resumes_with_same_decisions(cfg, tmp_path):
    llm = FakeLLM(answers=answers(), novelty={"Qwen9 B": "nee"})
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    vault = Vault(cfg)
    real = vault.write_item
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("file locked by sync")
        return real(**kw)

    vault.write_item = flaky
    try:
        asyncio.run(__import__("curator2.pipeline", fromlist=["run"]).run(
            cfg, src, judge, state, vault, github=gh, today=TODAY, progress=lambda m: None))
    except OSError:
        pass
    assert src.moved == []
    res = go(cfg, state, judge, src, gh)
    assert res.status == "klaar"
    assert res.counts["toppers"] == 1 and res.counts["repos"] == 1 and res.counts["gewoon_bewaard"] == 3
    n = notes(cfg)
    assert "Qwen9 B" not in n and len(n) == 5
    assert src.moved == ["m1"]


def test_stubborn_item_does_not_block_forever(cfg, tmp_path):
    mails = [{"id": "s1", "subject": "s", "sender_name": "S",
              "items": [item(1), item(2), item(3, link_text="always fails here")]}]

    class Stubborn(FakeLLM):
        def handler(self, request):
            if request.url.path.endswith("/chat/completions") and b"always fails here" in request.content:
                return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})
            return super().handler(request)

    llm = Stubborn(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, mails)
    statuses = [go(cfg, state, judge, src, gh, today=date(2026, 10, 26 + i)).status for i in range(3)]
    assert statuses[:2] == ["onderbroken", "onderbroken"] and statuses[2] == "klaar"
    assert src.moved == ["s1"]


def test_parse_only_returns_objects():
    assert _parse('[{"a": 1}]') == {"a": 1}
    assert _parse("true") is None
    assert _parse("[1, 2]") is None


def test_cut_off_answer_is_retried_with_more_room(cfg):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body["max_tokens"])
        if len(sent) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "length",
                                                          "message": {"content": '{"title": "A", "descr'}}]})
        out = {"title": "A", "description_nl": "d", "category": "ai_tool", "new_category": "", "kind": "tool",
               "topic": "", "usable": "ja", "fits": "ja", "junk": "nee", "topper": "nee", "fit": 5, "reason_nl": "r"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(out)}}]})

    j = Judge(cfg, transport=httpx.MockTransport(handler))
    j.model = "m"
    a = j.judge({"resolved_url": "https://e.com", "link_text": "x", "text": "t"})
    assert a["fit"] == 5 and sent == [600, 1200]


def test_other_400_errors_keep_structured_output(cfg):
    calls = []

    def handler(request):
        calls.append("response_format" in json.loads(request.content))
        return httpx.Response(400, json={"error": "context length exceeded"})

    j = Judge(cfg, transport=httpx.MockTransport(handler))
    j.model = "m"
    j.judge({"resolved_url": "https://e.com", "link_text": "x"})
    assert all(calls) and j._schema_ok


def test_missing_model_is_reported(cfg):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "some-other-model"}]})

    j = Judge(cfg, transport=httpx.MockTransport(handler))
    import curator2.judge as jm
    jm._start_lmstudio = lambda m: None
    try:
        j.ensure_ready()
        raised = False
    except jm.LLMUnavailable as exc:
        raised = "qwen2.5-14b-instruct" in str(exc)
    assert raised


def test_github_site_pages_are_not_repos():
    assert github_repo("https://github.com/resources/articles/ai/what-is-rag") is None
    a = normalize("https://github.com/resources/articles/ai/what-is-rag")
    b = normalize("https://github.com/resources/articles/devops/ci-cd")
    assert a != b


def test_newline_in_reason_keeps_tick_box_working(cfg, tmp_path):
    v = Vault(cfg)
    v.prepare()
    v.write_rejected("2026-10-26", [{"rid": "0123456789", "title": "Two\nlines", "url": "https://e.com/a b",
                                     "reason": "r", "why": "first\nsecond", "source": "S\n"}])
    p = v.rejected_path("2026-10-26")
    tick(p, "Two lines")
    assert v.ticked() == [(p, "0123456789")]


def test_windows_reserved_names():
    assert slug("CON") != "CON" and slug("nul.txt").upper() != "NUL.TXT"
    assert slug("Console tools") == "Console tools"


def test_mail_with_unreadable_articles_is_held_back(cfg, tmp_path):
    bad = [dict(item(i), extraction_status="redirect_failed", text=None) for i in range(5)]
    mails = [{"id": "b1", "subject": "beehiiv", "sender_name": "B", "items": bad}]
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, mails)
    r1 = go(cfg, state, judge, src, gh, today=date(2026, 10, 26))
    assert r1.counts["mails_overgeslagen"] == 1 and src.moved == []
    go(cfg, state, judge, src, gh, today=date(2026, 10, 27))
    assert src.moved == []
    go(cfg, state, judge, src, gh, today=date(2026, 10, 28))   # third night: processed anyway
    assert src.moved == ["b1"]


def test_weak_items_are_not_kept_just_to_fill_the_quota(cfg, tmp_path):
    mails = [{"id": "w1", "subject": "w", "sender_name": "W",
              "items": [item(1, link_text="weak marketing page"), item(2, link_text="decent article here")]}]
    ans = {"weak marketing page": {"usable": "ja", "fits": "deels", "fit": 2},      # 5 points
           "decent article here": {"usable": "ja", "fits": "deels", "fit": 3}}     # 6 points
    llm = FakeLLM(answers=ans)
    state, judge, src, gh = make(cfg, tmp_path, llm, mails)
    res = go(cfg, state, judge, src, gh)
    assert res.counts["gewoon_bewaard"] == 1
    rej = (cfg.root / "Verworpen" / "2026-10-26.md").read_text(encoding="utf-8")
    assert "## onder de ondergrens (1)" in rej

    cfg.min_points = 99
    mails2 = [{"id": "w2", "subject": "w", "sender_name": "W", "items": [item(3, link_text="anything at all")]}]
    state2 = State(tmp_path / "s2.sqlite")
    res2 = go(cfg, state2, Judge(cfg, transport=FakeLLM().transport()), FakeSource(mails2), gh,
              today=date(2026, 10, 27))
    assert res2.counts["gewoon_bewaard"] == 0     # minimum 1 never overrides the floor
