"""Clean a link so the same article reached by different routes compares equal."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only say where a click came from.
_TRACKING = re.compile(
    r"^(utm_.*|ref|ref_src|ref_url|source|src|mc_cid|mc_eid|fbclid|gclid|dclid|yclid|"
    r"igshid|_hsenc|_hsmi|mkt_tok|trk|trkid|sk|s_cid|cmpid|campaign|via|share|"
    r"sh|si|spm|__s|oly_enc_id|oly_anon_id|vero_id|wickedid|elqtrackid|rid|lid)$",
    re.I,
)

# Paths on GitHub that are part of a repo, not a different project.
_GITHUB_RESERVED = {
    "orgs", "users", "topics", "collections", "trending", "marketplace", "sponsors",
    "features", "about", "pricing", "login", "join", "settings", "search", "explore",
    "apps", "enterprise", "customer-stories", "readme", "events", "site",
}


def github_repo(url: str) -> str | None:
    """'github.com/Owner/Repo/tree/main/x' -> 'owner/repo'; None if not a repo link."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or parts[0].lower() in _GITHUB_RESERVED:
        return None
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{parts[0]}/{repo}".lower()


def huggingface_model(url: str) -> str | None:
    """'huggingface.co/Org/Model/...' -> 'org/model' (model pages only)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if (p.hostname or "").lower() not in ("huggingface.co", "www.huggingface.co"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or parts[0] in ("datasets", "spaces", "docs", "blog", "papers", "models"):
        return None
    return f"{parts[0]}/{parts[1]}".lower()


def normalize(url: str) -> str:
    """
    A comparison key for a link: lower-case host without www, no tracking
    parameters, no fragment, no trailing slash. GitHub links collapse to the
    repo, so a subfolder or README link is the same project as its root.
    """
    if not url:
        return ""
    repo = github_repo(url)
    if repo:
        return f"github.com/{repo}"
    model = huggingface_model(url)
    if model:
        return f"huggingface.co/{model}"
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)]
    query.sort()
    path = re.sub(r"/+$", "", p.path) or ""
    # medium.com/@user/title-abc123 and user.medium.com/title-abc123 are the same story
    if host.endswith("medium.com"):
        m = re.search(r"-([0-9a-f]{10,12})$", path)
        if m:
            return f"medium.com/{m.group(1)}"
    return urlunsplit(("", host, path, urlencode(query), "")).lstrip("/")


def host_of(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h
