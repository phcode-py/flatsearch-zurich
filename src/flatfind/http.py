"""Shared HTTP client with retries."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class BlockedError(RuntimeError):
    """The site answered with a bot challenge instead of content."""


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        },
        timeout=30,
        follow_redirects=True,
    )


def get(client: httpx.Client, url: str, *, params=None, retries: int = 3) -> httpx.Response:
    for attempt in range(retries):
        try:
            r = client.get(url, params=params)
        except httpx.TransportError as e:
            log.warning("GET %s failed (%s), attempt %d", url, e, attempt + 1)
        else:
            if r.status_code == 403 and "cdn-cgi" in r.text:
                raise BlockedError(f"bot challenge at {r.url}")
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r
            log.warning("GET %s -> %d, attempt %d", r.url, r.status_code, attempt + 1)
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"giving up on {url} after {retries} attempts")
