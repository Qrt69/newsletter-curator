"""The fixed rules from regels.toml: no language model involved."""

from __future__ import annotations

from .config import Rule
from .urls import host_of


def _domain_match(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check(item: dict, rules: list[Rule]) -> str | None:
    """Return the name of the first rule the item breaks, or None."""
    url = (item.get("resolved_url") or item.get("source_url") or "").strip()
    host = host_of(url)
    low_url = url.lower()
    after_scheme = low_url.split("://", 1)[-1]
    text = " ".join(
        str(item.get(k) or "") for k in ("title", "link_text", "description")
    ).lower()

    for r in rules:
        hits = []
        if r.domains:
            hits.append(_domain_match(host, r.domains))
        if r.words:
            hits.append(any(w in text for w in r.words))
        if r.url_starts:
            hits.append(any(after_scheme.startswith(s) for s in r.url_starts))
        if r.url_contains:
            hits.append(any(s in low_url for s in r.url_contains))
        if not hits:
            continue
        if (r.require_all and all(hits)) or (not r.require_all and any(hits)):
            return r.name
    return None


def unreadable(item: dict) -> bool:
    """Nothing to judge: no text, no title, no description, no usable link text."""
    if item.get("text"):
        return False
    if item.get("title") or item.get("description"):
        return False
    lt = (item.get("link_text") or "").strip()
    return len(lt) < 12
