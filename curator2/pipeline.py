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


def _book_cost(state: State, judge, run_id: int, month: str):
    """Write the dollars spent since the last booking to the database (Claude only)."""
    spent = getattr(judge, "cost", 0.0)
    booked = getattr(judge, "_booked", 0.0)
    if spent > booked:
        tin = getattr(judge, "input_tokens", 0) - getattr(judge, "_booked_in", 0)
        tout = getattr(judge, "output_tokens", 0) - getattr(judge, "_booked_out", 0)
        state.add_cost(month, run_id, spent - booked, tin, tout)
        judge._booked, judge._booked_in, judge._booked_out = spent, judge.input_tokens, judge.output_tokens


async def run(cfg: Config, source, judge, state: State, vault: Vault,
              github=None, today: date | None = None,
              progress: Callable[[str], None] = print) -> RunResult:
    today = today or date.today()
    try:
        return await _run(cfg, source, judge, state, vault, github, today, progress)
    finally:
        last = state.db.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            _book_cost(state, judge, last[0], today.strftime("%Y-%m"))


async def _run(cfg: Config, source, judge, state: State, vault: Vault, github, today: date,
               progress: Callable[[str], None]) -> RunResult:
    vault.prepare()
    run_id, resumed = state.open_run(today.isoformat())
    run_date = state.run_date(run_id)
    c = {"mails": 0, "mails_overgeslagen": 0, "items": 0, "dubbel": 0, "regels": 0,
         "niet_leesbaar": 0, "beoordeeld": 0, "niet_beoordeeld": 0, "toppers": 0, "repos": 0,
         "gewoon_bewaard": 0, "zelfde_inhoud": 0, "verworpen": 0, "opgewaardeerd": 0, "opgeruimd": 0}
    if resumed:
        progress(f"Onderbroken run van {run_date} wordt verdergezet.")

    # 0. The judge first (Claude or LM Studio): without it nothing is touched.
    try:
        model = judge.ensure_ready()
        progress(f"Beoordelaar klaar (model: {model}).")
    except LLMUnavailable as exc:
        return RunResult("onderbroken", f"Beoordelaar niet bereikbaar: {exc}", c)

    # 1. Upgrades Kurt ticked in earlier Verworpen lists.
    c["opgewaardeerd"] = _apply_upgrades(cfg, judge, state, vault, run_date, github, progress)

    # 2. The plan: every decision of this run, saved before anything is written,
    #    so a resumed run carries out the same decisions instead of making new ones.
    saved = state.load_plan(run_id)
    if saved is None:
        emails = await source.fetch()
        c["mails"] = len(emails)
        if not emails:
            _finish(cfg, state, vault, run_id, run_date, today, c, "geen mails", progress, judge)
            return RunResult("geen mails", "Geen nieuwe mails in 'to qualify'.", c)
        progress(f"{len(emails)} mails gevonden.")
        built = await _build_plan(cfg, emails, source, judge, state, vault, github, run_id, c, progress)
        if isinstance(built, RunResult):
            return built
        rows = built
        state.save_plan(run_id, rows, c)
    else:
        rows, saved_counts = saved
        c.update(saved_counts)
        progress("Het plan van de onderbroken run wordt verder uitgevoerd.")

    # 3. Carry out the plan: notes for kept items, entries for rejected ones.
    #    Each item is recorded as done right after it is written.
    for r in rows:
        if r.get("done"):
            continue
        key, it, answers = r["key"], r["item"], r["answers"]
        if r["keep"]:
            path = vault.write_item(
                key=key, url=it.get("resolved_url") or it.get("source_url") or "",
                answers=answers, source=it.get("_source", ""), run_date=run_date,
                points=r.get("score", 0), topper=r["lane"] == "topper", novelty=r.get("novelty", ""),
                repo_meta=r.get("repo_meta"))
            topic = answers.get("topic", "").strip().lower()
            if topic:
                state.add_topic_entry(topic, key, answers["title"], answers.get("description_nl", ""), path.stem)
            state.mark_seen(key, "bewaard", path.stem)
        else:
            state.add_rejected(rid_for(key), key, it, answers or None, r["reason"], run_date)
            state.mark_seen(key, "verworpen")
        state.plan_done(run_id, key)
        state.commit()
    vault.write_rejected(run_date, state.rejected_for_date(run_date))

    # 4. Only now: move the emails.
    moved = 0
    for eid in c.get("_ok_emails", []):
        try:
            await source.move(eid)
            moved += 1
        except Exception as exc:
            log.warning("Could not move email %s: %s", eid, exc)
    c["verplaatst"] = moved

    _finish(cfg, state, vault, run_id, run_date, today, c, "klaar", progress, judge)
    return RunResult("klaar", "", c)


_EXTRACT_FAILED = ("redirect_failed", "process_failed", "fetch_failed")
MAIL_RETRIES = 3   # a mail whose articles cannot be read is held back this many nights
ITEM_RETRIES = 3   # an item the model cannot judge is retried this many nights


async def _build_plan(cfg, emails, source, judge, state, vault, github, run_id, c, progress):
    # Read every link (cached per email for a resumed run).
    raw: list[tuple[dict, str]] = []  # (item, source name)
    ok_emails: list[str] = []
    for i, e in enumerate(emails, 1):
        items = state.get_extracted(run_id, e["id"])
        if items is None:
            progress(f"Artikels lezen ({i}/{len(emails)}): {e.get('subject', '')[:60]}")
            try:
                items = source.extract(e)
            except Exception as exc:
                log.warning("Extraction failed for %s: %s", e.get("subject"), exc)
                progress("  Kon deze mail niet lezen; ze blijft staan voor de volgende run.")
                c["mails_overgeslagen"] += 1
                continue
            bad = sum(1 for it in items if it.get("extraction_status") in _EXTRACT_FAILED)
            if len(items) >= 4 and bad / len(items) > 0.5 and state.bump_failure("mail:" + e["id"]) < MAIL_RETRIES:
                progress(f"  {bad} van {len(items)} artikels niet leesbaar; de mail blijft staan "
                         "en wordt morgen opnieuw geprobeerd.")
                c["mails_overgeslagen"] += 1
                continue
            state.put_extracted(run_id, e["id"], items)
        ok_emails.append(e["id"])
        src = _source_name(e)
        for it in items:
            raw.append((it, src))
    c["items"] = len(raw)
    c["_ok_emails"] = ok_emails

    rows: list[dict] = []

    def reject(it, key, answers, reason, why=""):
        if why:
            it["_why"] = why
        rows.append({"key": key, "item": it, "answers": answers or {}, "keep": False, "reason": reason})

    # Real duplicates, fixed rules, unreadable pages.
    known = vault.known_keys()
    seen_now: set[str] = set()
    to_judge: list[tuple[dict, str]] = []
    for it, src in raw:
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
            reject(it, key, None, broken)
        elif rules.unreadable(it):
            c["niet_leesbaar"] += 1
            reject(it, key, None, "niet leesbaar")
        else:
            to_judge.append((it, key))
    progress(f"{c['items']} items: {c['dubbel']} dubbel, {c['regels']} door vaste regels, "
             f"{c['niet_leesbaar']} niet leesbaar, {len(to_judge)} te beoordelen.")

    # Claude: all unjudged items in one batch, after checking the monthly cap.
    if hasattr(judge, "judge_many"):
        todo = {key: it for it, key in to_judge if state.get_judged(key) is None}
        if todo:
            month = date.today().strftime("%Y-%m")
            spent = state.month_cost(month)
            est = judge.estimate(len(todo))
            if spent + est > cfg.claude_monthly_cap:
                msg = (f"Maandplafond bereikt: deze maand al ${spent:.2f} uitgegeven, deze run zou ongeveer "
                       f"${est:.2f} kosten en het plafond is ${cfg.claude_monthly_cap:.2f}. De mails blijven staan.")
                vault.write_summary(state.run_date(run_id), f"# Run niet uitgevoerd\n\n{msg}\n")
                return RunResult("plafond", msg, c)
            progress(f"Beoordelen door Claude: {len(todo)} items, geschat ${est:.2f} "
                     f"(deze maand al ${spent:.2f} van ${cfg.claude_monthly_cap:.2f}).")
            try:
                answers_by_key = judge.judge_many(todo, progress=progress)
            except LLMUnavailable as exc:
                return RunResult("onderbroken", str(exc), c)
            finally:
                _book_cost(state, judge, run_id, month)
            for key, a in answers_by_key.items():
                if a is not None:
                    state.put_judged(key, a)

    # Judge (cached per link for a resumed run).
    cands: list[Candidate] = []
    retry_later = 0
    t0 = time.time()
    for n, (it, key) in enumerate(to_judge, 1):
        answers = state.get_judged(key)
        if answers is None:
            if n == 1 or n % 10 == 0:
                rate = f", {((time.time() - t0) / max(1, n - 1)):.0f} s per item" if n > 1 else ""
                progress(f"Beoordelen ({n}/{len(to_judge)}{rate})")
            try:
                answers = judge.judge(it)
            except LLMUnavailable as exc:
                return RunResult("onderbroken", f"Beoordelaar viel weg tijdens het beoordelen: {exc}", c)
            if answers is None:
                if state.bump_failure(key) >= ITEM_RETRIES:
                    c["niet_beoordeeld"] += 1
                    reject(it, key, None, "kon niet beoordeeld worden")
                else:
                    retry_later += 1
                continue
            state.put_judged(key, answers)
        c["beoordeeld"] += 1
        url = it.get("resolved_url") or ""
        cands.append(Candidate(item=it, key=key, answers=answers, is_repo=bool(github_repo(url)),
                               is_model=bool(huggingface_model(url))))
    if to_judge and retry_later / len(to_judge) > MAX_FAILED_SHARE:
        return RunResult("onderbroken", f"{retry_later} van {len(to_judge)} items konden niet "
                                        "beoordeeld worden; de run gaat de volgende keer verder.", c)
    if retry_later:
        # A few stubborn items must not hold back the whole run: they go to Verworpen
        # now and can be ticked; the model gets them again only if Kurt asks.
        for it, key in to_judge:
            if state.get_judged(key) is None and not any(r["key"] == key for r in rows):
                c["niet_beoordeeld"] += 1
                reject(it, key, None, "kon niet beoordeeld worden")

    # Never-keep categories (events).
    remaining = []
    for cd in cands:
        if cd.answers["category"] in cfg.never_categories:
            reject(cd.item, cd.key, cd.answers, "webinar of evenement", cd.answers.get("reason_nl", ""))
        else:
            remaining.append(cd)
    cands = remaining

    # Repo vital signs (for toppers and the weekly review).
    if github is not None:
        for cd in cands:
            repo = github_repo(cd.item.get("resolved_url") or "")
            if repo:
                cd.repo_meta = github.repo(repo)

    # Lanes and the best share of the regular items.
    rank.assign_lanes(cands, cfg)
    keep: list[Candidate] = [cd for cd in cands if cd.lane in ("topper", "repo")]
    regular = rank.order([cd for cd in cands if cd.lane == "gewoon"])
    quota = rank.quota(len(regular), cfg)
    kept_regular = 0
    run_topics: dict[str, list[dict]] = {}
    for cd in regular:
        if kept_regular >= quota:
            reject(cd.item, cd.key, cd.answers, "niet bij de beste van de run",
                   f"{cd.answers.get('reason_nl', '')} ({cd.score:g} punten)".strip())
            continue
        topic = cd.answers.get("topic", "").strip().lower()
        earlier = (state.topic_entries(topic) + run_topics.get(topic, [])) if topic else []
        if earlier:
            try:
                nv = judge.novelty(cd.answers["topic"], earlier, cd.answers["title"], cd.item.get("text") or "")
            except LLMUnavailable as exc:
                return RunResult("onderbroken", f"Beoordelaar viel weg: {exc}", c)
            if nv and nv["adds"] == "nee":
                c["zelfde_inhoud"] += 1
                reject(cd.item, cd.key, cd.answers, "zelfde inhoud als eerder artikel",
                       f"zelfde inhoud als {earlier[0]['title']}")
                continue
            cd.novelty = (nv or {}).get("what_nl", "")
        if topic:
            run_topics.setdefault(topic, []).append(
                {"title": cd.answers["title"], "summary": cd.answers.get("description_nl", ""), "note": ""})
        keep.append(cd)
        kept_regular += 1

    for cd in keep:
        rows.append({"key": cd.key, "item": cd.item, "answers": cd.answers, "keep": True,
                     "lane": cd.lane, "score": cd.score, "novelty": cd.novelty, "repo_meta": cd.repo_meta})
        c["toppers" if cd.lane == "topper" else ("repos" if cd.lane == "repo" else "gewoon_bewaard")] += 1
    c["verworpen"] = sum(1 for r in rows if not r["keep"])
    return rows


def _apply_upgrades(cfg: Config, judge: Judge, state: State, vault: Vault, run_date: str,
                    github, progress) -> int:
    ticked = vault.ticked()
    if not ticked:
        return 0
    done: dict = {}
    new_count = 0
    for path, rid in ticked:
        rec = state.get_rejected(rid)
        if not rec:
            continue
        if rec.get("upgraded"):  # done before; only the marker in the list was missing
            done.setdefault(path, set()).add(rid)
            continue
        new_count += 1
        it, answers = rec["item"], rec["answers"]
        if not answers:  # rejected by a fixed rule: never judged, so judge it now
            try:
                answers = judge.judge(it)
            except LLMUnavailable:
                answers = None
            if not answers:
                new_count -= 1
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
    n = new_count
    if n:
        progress(f"{n} aangevinkte items opgewaardeerd.")
    return n


def _finish(cfg: Config, state: State, vault: Vault, run_id: int, run_date: str, today: date,
            c: dict, status: str, progress, judge=None):
    if judge is not None and hasattr(judge, "judge_many"):
        month = date.today().strftime("%Y-%m")
        _book_cost(state, judge, run_id, month)
        c["kosten_run"] = round(state.run_cost(run_id), 4)
        c["kosten_maand"] = round(state.month_cost(month), 4)
        c["plafond"] = cfg.claude_monthly_cap
    c["opgeruimd"] = vault.cleanup_rejected(today, cfg.reject_keep_days)
    state.forget_rejected_before((today - timedelta(days=cfg.reject_keep_days)).isoformat())
    patterns = state.upgrade_patterns(30, cfg.pattern_threshold)
    vault.write_summary(run_date, summary_text(run_date, c, patterns, status))
    state.finish_run(run_id, status, c)
    cost = f" Kosten: ${c['kosten_run']:.2f} (maand: ${c['kosten_maand']:.2f})." if "kosten_run" in c else ""
    progress(f"Klaar: {c.get('toppers', 0)} toppers, {c.get('repos', 0)} repo's, "
             f"{c.get('gewoon_bewaard', 0)} andere items bewaard; {c.get('verworpen', 0)} verworpen.{cost}")


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
        f"| Beoordeeld door het taalmodel | {c.get('beoordeeld', 0)} |",
        f"| Kon niet beoordeeld worden | {c.get('niet_beoordeeld', 0)} |",
        f"| Toppers (Om te gebruiken) | {c.get('toppers', 0)} |",
        f"| Repo's | {c.get('repos', 0)} |",
        f"| Andere items bewaard | {c.get('gewoon_bewaard', 0)} |",
        f"| Zelfde inhoud als eerder artikel | {c.get('zelfde_inhoud', 0)} |",
        f"| Opgewaardeerd uit Verworpen | {c.get('opgewaardeerd', 0)} |",
        f"| Mails verplaatst naar processed | {c.get('verplaatst', 0)} |",
        "",
    ]
    if "kosten_run" in c:
        lines += [f"**Kosten Claude:** deze run ${c['kosten_run']:.2f}, deze maand ${c['kosten_maand']:.2f} "
                  f"van een plafond van ${c['plafond']:.2f}.", ""]
    if c.get("opgeruimd"):
        lines += [f"{c['opgeruimd']} oude Verworpen-lijsten opgeruimd.", ""]
    if patterns:
        lines += ["## Voorstel", ""]
        for src, cat, n in patterns:
            lines.append(f"- Je waardeerde de voorbije 30 dagen {n} items op uit **{src or 'onbekende bron'}** "
                         f"in **{cat}**. Moeten de regels daar milder zijn?")
        lines.append("")
    return "\n".join(lines)
