"""Is the internet really there? A hotel or B&B wifi can let Outlook through while
every website still waits for a login page, so check real HTTPS sites."""

from __future__ import annotations

import httpx

PROBES = ("https://www.wikipedia.org/", "https://github.com/", "https://www.cloudflare.com/")


def internet_ok(timeout: float = 10.0) -> bool:
    """True when at least one well-known site answers over HTTPS from its own domain.

    A login page of a captive portal fails here: HTTPS to these sites breaks, or
    the answer comes from another host."""
    for url in PROBES:
        host = httpx.URL(url).host
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
        except Exception:
            continue
        if r.status_code < 500 and r.url.host.endswith(host.removeprefix("www.")):
            return True
    return False
