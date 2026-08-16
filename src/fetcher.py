"""Unified page fetcher for PlusForward.

Single source of truth for HTTP fetching with rate limiting, retries,
and rotating User-Agents. Used by discovery, download, and tier resolver.

Uses curl --compressed for all requests. The PlusForward server has a
quirk where some pages never terminate the chunked gzip stream — curl
handles this gracefully (decompresses partial data, returns rc=28 on
timeout) while Python's requests/urllib3 hangs waiting for EOF.

curl works on all pages: 0.2s for normal pages, ~3s for stalled ones.

Usage:
    from src.fetcher import PageFetcher

    fetcher = PageFetcher()
    html = fetcher.fetch("https://www.plusforward.net/post/94419/")
    if html:
        ...
"""

import logging
import random
import subprocess
import time
from typing import Optional

from config import (
    HTTP_TIMEOUT,
    PF_COOKIE_HEADER,
    RATE_LIMIT_DELAY,
    RETRY_BACKOFF,
    USER_AGENTS,
)

logger = logging.getLogger(__name__)

# Max retry attempts before giving up. 0 = infinite.
MAX_RETRIES = 0


class PageFetcher:
    """Fetch pages from the web with rate limiting and retries.

    A single instance tracks the last request time for rate limiting.
    Thread-unsafe by design — each worker should have its own instance.
    """

    def __init__(self):
        self._last_request_time = 0.0

    def _rate_limit(self):
        """Sleep if the last request was too recent."""
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            sleep_time = RATE_LIMIT_DELAY - elapsed + random.uniform(0, 0.3)
            time.sleep(sleep_time)

    def fetch(self, url: str, max_retries: int = MAX_RETRIES,
              retry_delay: float = None) -> Optional[str]:
        """Fetch a URL and return the response text, or None on failure.

        Uses curl --compressed with the configured timeout. The PlusForward
        server never terminates some chunked gzip streams, so curl returns
        rc=28 (timeout) but still provides the full decompressed content.

        Returns None if all retries are exhausted.

        Args:
            url: Full URL to fetch.
            max_retries: Number of attempts before giving up. 0 = infinite.
            retry_delay: Base delay for exponential backoff in seconds.
                Defaults to RETRY_BACKOFF from config (2.0).
                Wait = retry_delay ^ attempt, capped at 60s + jitter.

        Returns:
            HTML text, or None.
        """
        if retry_delay is None:
            retry_delay = RETRY_BACKOFF
        ua = random.choice(USER_AGENTS)
        attempt = 0
        while True:
            self._rate_limit()
            self._last_request_time = time.time()

            try:
                cmd = [
                    "curl", "-s", "--compressed",
                    "--connect-timeout", str(HTTP_TIMEOUT),
                    "--max-time", str(HTTP_TIMEOUT),
                    "-A", ua,
                    "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "-H", "Accept-Language: en-US,en;q=0.5",
                ]
                # Send the PlusForward cookie header (accepts cookies + disables
                # sidebars) so the server omits sidebar HTML, halving page size.
                if PF_COOKIE_HEADER:
                    cmd += ["-H", f"Cookie: {PF_COOKIE_HEADER}"]
                cmd += [url]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=HTTP_TIMEOUT + 2,
                )

                body = result.stdout.decode("utf-8", errors="replace")

                # rc=0: full response. rc=28: timeout but partial data is valid
                # (stalled gzip stream — server sent all data but no EOF).
                if result.returncode in (0, 28) and body and len(body) > 100:
                    return body

                # First failure only — retries are expected under load; don't
                # spam one line per attempt.
                if attempt == 0:
                    logger.debug(f"curl rc={result.returncode}, {len(body)}b for {url}")

            except (subprocess.TimeoutExpired, OSError) as e:
                if attempt == 0:
                    logger.debug(f"curl failed for {url}: {e}")

            attempt += 1
            if max_retries > 0 and attempt >= max_retries:
                # Single-retry mode is used by tier resolver which handles
                # its own retry loop + DB checks. Don't log as ERROR there.
                level = logger.debug if max_retries <= 1 else logger.error
                level(f"fetch failed: {url} after {attempt} attempts")
                return None

            # Exponential backoff with jitter.
            wait = min(retry_delay ** attempt, 60)
            wait += random.uniform(0, wait * 0.3)
            if attempt == 1:
                logger.debug(f"retry {url} in {wait:.1f}s")
            time.sleep(wait)
