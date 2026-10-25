"""
One run, start to finish. Every step's result is saved before the next step,
so an interrupted run picks up where it stopped: emails already read are not
read again, items already judged are not judged again, and the emails only
move to "processed" once everything is written into OSK.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from . import rank, rules
from .config import Config
from .judge import Judge, LLMUnavailable
from .rank import Candidate
from .state import State
from .urls import github_repo, huggingface_model, normalize
from .vault import Vault, rid_for

log = logging.getLogger("curator2.pipeline")

MAX_FAILED_SHARE = 0.25  # more unjudged items than this: stop and resume later


@dataclass
class RunResult:
    status: str                      # "klaar" | "onderbroken" | "geen mails"
    message: str = ""
    counts: dict = field(default_factory=dict)


def _source_name(email: dict) -> str:
    return (email.get("sender_name") or email.get("sender") or email.get("subject") or "").strip()[:80]


async def run(cfg: Config, source, judge: Judge, state: State, vault: Vault,
              github=None, today: date | None = None,
              progress: Callable[[str], None] = print) -> RunResult:
    today = today or date.today()
    vault.prepare()
    run_id, resumed = state.open_run(today.isoformat())
    run_date = state.run_date(run_id)
    c = {"mails": 0, "items": 0, "dubbel": 0, "regels": 0, "niet_leesbaar": 0,
         "beoordeeld": 0, "niet_beoordeeld": 0, "toppers": 0, "repos": 0, "gewoon_bewaard": 0,
         "zelfde_inhoud": 0, "verworpen": 0, "opgewaardeerd": 0, "opgeruimd": 0}
    if resumed:
        progress(f"Onderbroken run van {run_date} wordt verdergezet.")

    # 0. LM Studio first: without it there is nothing to do, and nothing is touched.
    try:
        model = judge.ensure_ready()
        progress(f"LM Studio klaar (model: {model}).")
    except LLMUnavailable as exc:
        return RunResult("onderbroken", f"LM Studio niet bereikbaar: {exc}", c)

    # 1. Upgrades Kurt ticked in earlier Verworpen lists.
    c["opgewaardeerd"] = _apply_upgrades(cfg, judge, state, vault, run_date, github, progress)

    # 2. Mails.
    emails = await source.fetch()
    c["mails"] = len(emails)
    if not emails:
        _finish(cfg, state, vault, run_id, run_date, today, c, "geen mails", progress)
        return RunResult("geen mails", "Geen nieuwe mails in 'to qualify'.", c)
    progress(f"{len(emails)} mails gevonden.")

    # 3. Read every link (cached per email for a resumed run).
    raw: list[tuple[dict, str, str]] = []  # (item, source name, email id)
    ok_emails: list[str] = []
    for i, e in enumerate(emails, 1):
        items = state.get_extracted(run_id, e["id"])
        if items is None:
            progress(f"Artikels lezen ({i}/{len(emails)}): {e.get('subject', '')[:60]}")
            try:
                items = source.extract(e)
            except Exception as exc:
                log.warning("Extraction failed for %s: %s", e.get("subject"), exc)
                progress(f"  Kon deze mail niet lezen; ze blijft staan voor de volgende run.")
                continue
            state.put_extracted(run_id, e["id"], items)
        ok_emails.append(e["id"])
        for it in items:
            raw.append((it, _source_name(e), e["id"]))
    c["items"] = len(raw)

    # 4. Real duplicates, fixed rules, unreadable pages.
    known = vault.known_keys()
    seen_now: set[str] = set()
    to_judge: list[tuple[dict, str, str]] = []  # (item, key, source)
    rejected: list[tuple[dict, str, dict | None, str]] = []  # (item, key, answers, reason)
    for it, src, _ in raw:
        url = it.get("resolved_url") or it.get("source_url") or ""
        key = normalize(url)
        if not key:
            continue
        it["_source"] = src
        if key in seen_now or key in known or state.is_seen(key):
            c["dubbel"] += 1
            continue
        seen_now.add(key)
        broken = rules.check(it, cfg.rules)
        if broken:
            c["regels"] += 1
            rejected.append((it, key, None, broken))
            continue
        if rules.unreadable(it):
            c["niet_leesbaar"] += 1
            rejected.append((it, key, None, "niet leesbaar"))
            continue
        to_judge.append((it, key, src))
    progress(f"{c['items']} items: {c['dubbel']} dubbel, {c['regels']} door vaste regels, "
             f"{c['niet_leesbaar']} niet leesbaar, {len(to_judge)} te beoordelen.")

    # 5. Judge (cached per link for a resumed run).
    cands: list[Candidate] = []
    failed: list[tuple[dict, str]] = []
    t0 = time.time()
    for n, (it, key, src) in enumerate(to_judge, 1):
        answers = state.get_judged(key)
        if answers is None:
            if n == 1 or n % 10 == 0:
                rate = f", {((time.time() - t0) / max(1, n - 1)):.0f} s per item" if n > 1 else ""
                progress(f"Beoordelen ({n}/{len(to_judge)}{rate})")
            try:
                answers = judge.judge(it)
            except LLMUnavailable as exc:
                return RunResult("onderbroken", f"LM Studio viel weg tijdens het beoordelen: {exc}", c)
            if answers is None:
                failed.append((it, key))
                continue
            state.put_judged(key, answers)
        c["beoordeeld"] += 1
        repo = github_repo(it.get("resolved_url") or "")
        cands.append(Candidate(item=it, key=key, answers=answers, is_repo=bool(repo),
                               is_model=bool(huggingface_model(it.get("resolved_url") or ""))))
    if to_judge and len(failed) / len(to_judge) > MAX_FAILED_SHARE:
        return RunResult("onderbroken", f"{len(failed)} van {len(to_judge)} items konden niet "
                                        "beoordeeld worden; de run gaat de volgende keer verder.", c)
    for it, key in failed:
        c["niet_beoordeeld"] += 1
        rejected.append((it, key, None, "kon niet beoordeeld worden"))

    # 6. Never-keep categories (events).
    keep_cands = []
    for cd in cands:
        if cd.answers["category"] in cfg.never_categories:
            cd.item["_why"] = cd.answers.get("reason_nl", "")
            rejected.append((cd.item, cd.key, cd.answers, "webinar of evenement"))
        else:
            keep_cands.append(cd)
    cands = keep_cands

    # 7. Repo vital signs (for toppers and the weekly review).
    if github is not None:
        for cd in cands:
            repo = github_repo(cd.item.get("resolved_url") or "")
            if repo:
                cd.repo_meta = github.repo(repo)

    # 8. Lanes and the 10%.
    rank.assign_lanes(cands, cfg)
    keep: list[Candidate] = [cd for cd in cands if cd.lane in ("topper", "repo")]
    regular = rank.order([cd for cd in cands if cd.lane == "gewoon"])
    quota = rank.quota(len(regular), cfg)
    kept_regular = 0
    run_topics: dict[str, list[dict]] = {}
    for cd in regular:
        if kept_regular >= quota:
            cd.item["_why"] = f"{cd.answers.get('reason_nl', '')} ({cd.score:g} punten)".strip()
            rejected.append((cd.item, cd.key, cd.answers, "niet bij de beste van de run"))
            continue
        topic = cd.answers.get("topic", "").strip().lower()
        earlier = (state.topic_entries(topic) + run_topics.get(topic, [])) if topic else []
        if earlier:
            try:
                nv = judge.novelty(cd.answers["topic"], earlier, cd.answers["title"], cd.item.get("text") or "")
            except LLMUnavailable as exc:
                return RunResult("onderbroken", f"LM Studio viel weg: {exc}", c)
            if nv and nv["adds"] == "nee":
                c["zelfde_inhoud"] += 1
                cd.item["_why"] = f"zelfde inhoud als {earlier[0]['title']}"
                rejected.append((cd.item, cd.key, cd.answers, "zelfde inhoud als eerder artikel"))
                continue
            cd.novelty = (nv or {}).get("what_nl", "")
        if topic:
            run_topics.setdefault(topic, []).append(
                {"title": cd.answers["title"], "summary": cd.answers.get("description_nl", ""), "note": ""})
        keep.append(cd)
        kept_regular += 1

    # 9. Write kept items into OSK.
    for cd in keep:
        topper = cd.lane == "topper"
        path = vault.write_item(
            key=cd.key, url=cd.item.get("resolved_url") or cd.item.get("source_url") or "",
            answers=cd.answers, source=cd.item.get("_source", ""), run_date=run_date,
            points=cd.score, topper=topper, novelty=cd.novelty, repo_meta=cd.repo_meta)
        topic = cd.answers.get("topic", "").strip().lower()
        if topic:
            state.add_topic_entry(topic, cd.key, cd.answers["title"], cd.answers.get("description_nl", ""), path.stem)
        state.mark_seen(cd.key, "bewaard", path.stem)
        c["toppers" if topper else ("repos" if cd.lane == "repo" else "gewoon_bewaard")] += 1

    # 10. Rejected list (rendered from the database, so a resumed run never loses entries).
    for it, key, answers, reason in rejected:
        state.add_rejected(rid_for(key), key, it, answers, reason, run_date)
        state.mark_seen(key, "verworpen")
    c["verworpen"] = len(rejected)
    state.commit()
    vault.write_rejected(run_date, state.rejected_for_date(run_date))

    # 11. Only now: move the emails.
    moved = 0
    for eid in ok_emails:
        try:
            await source.move(eid)
            moved += 1
        except Exception as exc:
            log.warning("Could not move email %s: %s", eid, exc)
    c["verplaatst"] = moved

    _finish(cfg, state, vault, run_id, run_date, today, c, "klaar", progress)
    return RunResult("klaar", "", c)


def _apply_upgrades(cfg: Config, judge: Judge, state: State, vault: Vault, run_date: str,
                    github, progress) -> int:
    ticked = vault.ticked()
    if not ticked:
        return 0
    done: dict = {}
    for path, rid in ticked:
        rec = state.get_rejected(rid)
        if not rec:
            continue
        it, answers = rec["item"], rec["answers"]
        if not answers:  # rejected by a fixed rule: never judged, so judge it now
            try:
                answers = judge.judge(it)
            except LLMUnavailable:
                answers = None
            if not answers:
                continue
        if answers["category"] in cfg.never_categories:
            answers = dict(answers, category="article")
        repo = github_repo(it.get("resolved_url") or "")
        meta = github.repo(repo) if (github is not None and repo) else None
        p = vault.write_item(key=rec["key"], url=it.get("resolved_url") or it.get("source_url") or "",
                             answers=answers, source=it.get("_source", ""), run_date=run_date,
                             points=rank.points(answers, cfg), topper=False, repo_meta=meta, upgraded=True)
        state.mark_seen(rec["key"], "bewaard", p.stem)
        cat = cfg.categories.get(answers["category"], answers["category"])
        state.mark_upgraded(rid, it.get("_source", ""), cat, rec["reason"])
        done.setdefault(path, set()).add(rid)
    state.commit()
    for path, rids in done.items():
        vault.mark_upgraded(path, rids)
    n = sum(len(r) for r in done.values())
    if n:
        progress(f"{n} aangevinkte items opgewaardeerd.")
    return n


def _finish(cfg: Config, state: State, vault: Vault, run_id: int, run_date: str, today: date,
            c: dict, status: str, progress):
    c["opgeruimd"] = vault.cleanup_rejected(today, cfg.reject_keep_days)
    state.forget_rejected_before((today - timedelta(days=cfg.reject_keep_days)).isoformat())
    patterns = state.upgrade_patterns(30, cfg.pattern_threshold)
    vault.write_summary(run_date, summary_text(run_date, c, patterns, status))
    state.finish_run(run_id, status, c)
    progress(f"Klaar: {c.get('toppers', 0)} toppers, {c.get('repos', 0)} repo's, "
             f"{c.get('gewoon_bewaard', 0)} andere items bewaard; {c.get('verworpen', 0)} verworpen.")


def summary_text(run_date: str, c: dict, patterns: list, status: str) -> str:
    kept = c.get("toppers", 0) + c.get("repos", 0) + c.get("gewoon_bewaard", 0)
    lines = [
        "---", f"datum: {run_date}", f"status: {status}", "tags:", "  - curator/run", "---", "",
        f"# Run van {run_date}", "",
        f"**{kept} bewaard**, {c.get('verworpen', 0)} verworpen, {c.get('dubbel', 0)} dubbels overgeslagen.", "",
        "| | Aantal |", "| --- | --- |",
        f"| Mails | {c.get('mails', 0)} |",
        f"| Items in de mails | {c.get('items', 0)} |",
        f"| Dubbels (al gezien) | {c.get('dubbel', 0)} |",
        f"| Door vaste regels verworpen | {c.get('regels', 0)} |",
        f"| Niet leesbaar | {c.get('niet_leesbaar', 0)} |",
        f"| Beoordeeld door LM Studio | {c.get('beoordeeld', 0)} |",
        f"| Kon niet beoordeeld worden | {c.get('niet_beoordeeld', 0)} |",
        f"| Toppers (Om te gebruiken) | {c.get('toppers', 0)} |",
        f"| Repo's | {c.get('repos', 0)} |",
        f"| Andere items bewaard | {c.get('gewoon_bewaard', 0)} |",
        f"| Zelfde inhoud als eerder artikel | {c.get('zelfde_inhoud', 0)} |",
        f"| Opgewaardeerd uit Verworpen | {c.get('opgewaardeerd', 0)} |",
        f"| Mails verplaatst naar processed | {c.get('verplaatst', 0)} |",
        "",
    ]
    if c.get("opgeruimd"):
        lines += [f"{c['opgeruimd']} oude Verworpen-lijsten opgeruimd.", ""]
    if patterns:
        lines += ["## Voorstel", ""]
        for src, cat, n in patterns:
            lines.append(f"- Je waardeerde de voorbije 30 dagen {n} items op uit **{src or 'onbekende bron'}** "
                         f"in **{cat}**. Moeten de regels daar milder zijn?")
        lines.append("")
    return "\n".join(lines)
