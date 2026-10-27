import asyncio
import os
import re
from datetime import date

from curator2.judge import Judge
from curator2.mailsource import FakeSource
from curator2.pipeline import run
from curator2.state import State
from curator2.vault import Vault

from .conftest import FakeGitHub, FakeLLM, item

TODAY = date(2026, 10, 26)


def build_mail():
    items = []
    # 20 regular articles with rising fit; numbers 18 and 19 are the best.
    for n in range(20):
        items.append(item(n))
    # same article as #0 via another route -> real duplicate
    items.append(item(0, url="https://www.example.com/article-0?utm_medium=email&utm_source=other"))
    items.append(item(100, url="https://x.com/someone/status/1"))
    items.append(item(101, link_text="Register now for our webinar on agents"))
    items.append(item(102, url="https://github.com/small/toy/blob/main/README.md", link_text="toy repo"))
    items.append(item(103, url="https://github.com/big/tool", link_text="big tool repo"))
    items.append(item(104, link_text="Qwen9 launch coverage A", title="Qwen9 A"))
    items.append(item(105, link_text="Qwen9 launch coverage B", title="Qwen9 B"))
    items.append(item(106, link_text="Conference in Ghent next month"))
    return [{"id": "m1", "subject": "TLDR AI", "sender_name": "TLDR AI", "items": items}]


def answers():
    a = {}
    for n in range(20):
        a[f"Article number {n} about things"] = {"usable": "ja", "fits": "deels", "fit": 1 + n % 5}
    a["Article number 18 about things"] = {"usable": "ja", "fits": "ja", "fit": 4}
    a["Article number 19 about things"] = {"usable": "ja", "fits": "ja", "fit": 4}
    a["toy repo"] = {"kind": "repo", "usable": "ja", "fits": "deels", "fit": 2}
    a["big tool repo"] = {"kind": "repo", "usable": "ja", "fits": "ja", "fit": 5, "topper": "ja",
                          "category": "python_library", "title": "Big Tool"}
    a["Qwen9 launch coverage A"] = {"usable": "ja", "fits": "ja", "fit": 5, "topic": "Qwen9",
                                    "category": "model_release", "title": "Qwen9 A"}
    a["Qwen9 launch coverage B"] = {"usable": "ja", "fits": "ja", "fit": 5, "topic": "Qwen9",
                                    "category": "model_release", "title": "Qwen9 B"}
    a["Conference in Ghent next month"] = {"category": "event", "fit": 5, "fits": "ja", "usable": "ja"}
    return a


def make(cfg, tmp_path, llm, mails):
    state = State(tmp_path / "state.sqlite")
    judge = Judge(cfg, transport=llm.transport())
    src = FakeSource(mails)
    gh = FakeGitHub({"big/tool": {"stars": 900, "days_idle": 2, "archived": False, "license": "MIT"},
                     "small/toy": {"stars": 3, "days_idle": 20, "archived": False, "license": ""}})
    return state, judge, src, gh


def go(cfg, state, judge, src, gh, today=TODAY):
    return asyncio.run(run(cfg, src, judge, state, Vault(cfg), github=gh, today=today, progress=lambda m: None))


def notes(cfg):
    return {p.stem: p.read_text(encoding="utf-8") for p in (cfg.root / "Items").glob("*.md")}


def test_full_run(cfg, tmp_path):
    llm = FakeLLM(answers=answers(), novelty={"Qwen9 B": "nee"})
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    res = go(cfg, state, judge, src, gh)
    assert res.status == "klaar", res.message
    c = res.counts
    assert c["dubbel"] == 1
    assert c["regels"] == 2          # X link + webinar words
    assert c["toppers"] == 1
    assert c["repos"] == 1
    # 20 articles + Qwen9 A + Qwen9 B = 22 regular -> quota ceil(2.2) = 3
    assert c["gewoon_bewaard"] == 3
    assert c["zelfde_inhoud"] == 1

    n = notes(cfg)
    assert "Big Tool" in n and "topper: true" in n["Big Tool"]
    assert 'sterren: 900' in n["Big Tool"] and 'repo_oordeel: ""' in n["Big Tool"]
    assert "Qwen9 A" in n and "Qwen9 B" not in n
    kept_regular = [k for k in n if k.startswith("Article") and 'soort: "repo"' not in n[k]]
    assert 'soort: "repo"' in n["Article 102"] and "topper: false" in n["Article 102"]
    assert sorted(kept_regular) == ["Article 18", "Article 19"]
    assert 'categorie: "[[Curator/Categorieën/Python-bibliotheken|Python-bibliotheken]]"' in n["Big Tool"]

    base = (cfg.root / "Curator.base").read_text(encoding="utf-8")
    assert 'file.inFolder("Curator/Items")' in base and "Om te gebruiken" in base

    rej = (cfg.root / "Verworpen" / "2026-10-26.md").read_text(encoding="utf-8")
    assert "## link naar X (1)" in rej
    assert "zelfde inhoud als Qwen9 A" in rej
    assert "## webinar of evenement (2)" in rej     # word rule + event category
    assert rej.count("- [ ]") == c["verworpen"]
    assert src.moved == ["m1"]

    summary = (cfg.root / "Runs" / "2026-10-26.md").read_text(encoding="utf-8")
    assert "**5 bewaard**" in summary


def test_second_run_skips_everything_seen(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    go(cfg, state, judge, src, gh)
    calls = llm.judge_calls
    src2 = FakeSource([dict(build_mail()[0], id="m2")])
    res = go(cfg, state, judge, src2, gh, today=date(2026, 10, 27))
    assert res.counts["dubbel"] == res.counts["items"]
    assert llm.judge_calls == calls


def test_interrupted_run_resumes_without_redoing_work(cfg, tmp_path):
    llm = FakeLLM(answers=answers(), fail_after=5)
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    res = go(cfg, state, judge, src, gh)
    assert res.status == "onderbroken"
    assert src.moved == []
    assert not list((cfg.root / "Items").glob("*.md"))

    llm.fail_after = None
    before = llm.judge_calls
    res2 = go(cfg, state, judge, src, gh)
    assert res2.status == "klaar", res2.message
    assert src.extract_calls == ["m1"]               # mail not read a second time
    assert llm.judge_calls - before == 25 - 5        # only the unjudged items asked again
    assert src.moved == ["m1"]


def test_lmstudio_down_touches_nothing(cfg, tmp_path):
    llm = FakeLLM(down=True)
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    judge.ensure_ready = lambda: (_ for _ in ()).throw(__import__("curator2.judge").judge.LLMUnavailable("x"))
    res = go(cfg, state, judge, src, gh)
    assert res.status == "onderbroken"
    assert src.extract_calls == [] and src.moved == []


def test_ticked_item_is_upgraded_next_run(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    go(cfg, state, judge, src, gh)
    path = cfg.root / "Verworpen" / "2026-10-26.md"
    text = path.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if "Article 3" in l)
    text = text.replace(line, line.replace("- [ ]", "- [x]"))
    xline = next(l for l in text.splitlines() if "x.com" in l)
    text = text.replace(xline, xline.replace("- [ ]", "- [x]"))
    path.write_text(text, encoding="utf-8")

    res = go(cfg, state, judge, FakeSource([]), gh, today=date(2026, 10, 27))
    assert res.counts["opgewaardeerd"] == 2
    n = notes(cfg)
    assert "Article 3" in n and "opgewaardeerd: true" in n["Article 3"]
    after = path.read_text(encoding="utf-8")
    assert "opgewaardeerd" in next(l for l in after.splitlines() if "Article 3" in l)
    # a third run does not upgrade it again
    res3 = go(cfg, state, judge, FakeSource([]), gh, today=date(2026, 10, 28))
    assert res3.counts["opgewaardeerd"] == 0


def test_upgrade_pattern_is_reported(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    go(cfg, state, judge, src, gh)
    path = cfg.root / "Verworpen" / "2026-10-26.md"
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if re.search(r"Article (\d|1[0-7])\b", l)][:5]
    for l in lines:
        text = text.replace(l, l.replace("- [ ]", "- [x]"))
    path.write_text(text, encoding="utf-8")
    go(cfg, state, judge, FakeSource([]), gh, today=date(2026, 10, 27))
    summaries = sorted((cfg.root / "Runs").glob("*.md"))
    last = summaries[-1].read_text(encoding="utf-8")
    assert "## Voorstel" in last and "TLDR AI" in last


def test_old_rejected_lists_are_removed(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    Vault(cfg).prepare()
    old = cfg.root / "Verworpen" / "2026-06-01.md"
    old.write_text("oud", encoding="utf-8")
    recent = cfg.root / "Verworpen" / "2026-10-01.md"
    recent.write_text("recent", encoding="utf-8")
    go(cfg, state, judge, FakeSource([]), gh)
    assert not old.exists() and recent.exists()


def test_failed_mail_is_not_moved(cfg, tmp_path):
    llm = FakeLLM(answers=answers())
    mails = build_mail() + [{"id": "m9", "subject": "broken", "items": []}]
    state, judge, _, gh = make(cfg, tmp_path, llm, mails)
    src = FakeSource(mails, fail_extract={"m9"})
    res = go(cfg, state, judge, src, gh)
    assert res.status == "klaar"
    assert src.moved == ["m1"]
