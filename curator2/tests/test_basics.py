from curator2 import rules
from curator2.rank import Candidate, assign_lanes, points, quota
from curator2.urls import github_repo, normalize


def test_normalize_strips_tracking_and_case():
    a = normalize("https://www.Example.com/post/?utm_source=x&utm_medium=email&id=3#top")
    b = normalize("https://example.com/post?id=3")
    assert a == b == "example.com/post?id=3"


def test_github_links_collapse_to_repo():
    assert normalize("https://github.com/Owner/Repo/tree/main/docs") == "github.com/owner/repo"
    assert normalize("https://github.com/owner/repo.git") == "github.com/owner/repo"
    assert github_repo("https://github.com/topics/llm") is None


def test_medium_variants_match():
    a = normalize("https://medium.com/@someone/a-great-title-1a2b3c4d5e6f")
    b = normalize("https://someone.medium.com/a-great-title-1a2b3c4d5e6f?source=rss")
    assert a == b


def test_rules(cfg):
    x = {"resolved_url": "https://x.com/someone/status/1", "title": "t"}
    web = {"resolved_url": "https://example.com/a", "title": "Join our webinar on agents"}
    paid = {"resolved_url": "https://go.vendor.com/lp?utm_medium=paid-social", "title": "t"}
    paid_host_only = {"resolved_url": "https://go.dev/doc/effective_go", "title": "Effective Go"}
    ok = {"resolved_url": "https://example.com/post", "title": "A library for graphs"}
    assert rules.check(x, cfg.rules) == "link naar X"
    assert rules.check(web, cfg.rules) == "webinar of evenement"
    assert rules.check(paid, cfg.rules) == "reclame of betaalde link"
    assert rules.check(paid_host_only, cfg.rules) is None
    assert rules.check(ok, cfg.rules) is None


def test_points_and_quota(cfg):
    good = {"usable": "ja", "fits": "ja", "junk": "nee", "fit": 5}
    bad = {"usable": "nee", "fits": "nee", "junk": "ja", "fit": 1}
    assert points(good, cfg) == 2 + 3 + 0 + 5
    assert points(bad, cfg) == 0 - 3 - 4 + 1
    assert quota(0, cfg) == 0
    assert quota(5, cfg) == 1      # minimum 1
    assert quota(25, cfg) == 3     # 10% rounded up


def test_topper_needs_live_repo(cfg):
    ans = {"usable": "ja", "fits": "ja", "junk": "nee", "fit": 5, "topper": "ja", "kind": "repo"}
    alive = Candidate(item={}, key="k1", answers=ans, is_repo=True,
                      repo_meta={"stars": 500, "days_idle": 3, "archived": False})
    dead = Candidate(item={}, key="k2", answers=ans, is_repo=True,
                     repo_meta={"stars": 500, "days_idle": 400, "archived": False})
    article = Candidate(item={}, key="k3", answers=dict(ans, kind="article"))
    assign_lanes([alive, dead, article], cfg)
    assert alive.lane == "topper"
    assert dead.lane == "repo"
    assert article.lane == "gewoon"


def test_beehiiv_and_newsletter_tracking_is_removed():
    a = normalize("https://wisprflow.ai/notetaker?utm_source=beehiiv&_bhiiv=opp_1&bhcl_id=x&_bhlid=abc")
    b = normalize("https://wisprflow.ai/notetaker?utm_source=superhuman&dub_id=q&shad=1&_bhlid=def")
    assert a == b == "wisprflow.ai/notetaker"
    # a real parameter stays
    assert normalize("https://example.com/watch?v=abc&utm_medium=x") == "example.com/watch?v=abc"
