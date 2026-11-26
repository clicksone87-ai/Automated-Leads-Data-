"""Headless Chromium for sites that show their content only through JavaScript.

Used only after a normal fetch found a page with scripts but no readable text. Every
request the page makes is checked against the same public-address rule as the HTTP
client, and images, media and fonts are never downloaded.
"""

from __future__ import annotations

import asyncio
import socket
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlsplit

import anyio
import structlog

from leads.enrich.safe_http import MAX_BYTES, is_public
from leads.settings import WebsiteSettings

if TYPE_CHECKING:
    from playwright.async_api import Browser as PwBrowser
    from playwright.async_api import Playwright, Route

log = structlog.get_logger(__name__)

_SKIPPED = {"image", "media", "font"}
# Scripts often fill the page a moment after the load event.
_SETTLE_MS = 2000


def available() -> bool:
    """Whether Playwright is installed (the optional ``browser`` extra)."""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


class Browser:
    """One Chromium for a batch of checks; a few pages at a time."""

    def __init__(self, cfg: WebsiteSettings) -> None:
        self.cfg = cfg
        self._pages = asyncio.Semaphore(max(1, cfg.browser_pages))
        self._hosts: dict[str, bool] = {}
        self._playwright: Playwright | None = None
        self._browser: PwBrowser | None = None

    async def __aenter__(self) -> Self:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def _public(self, host: str) -> bool:
        if host not in self._hosts:
            try:
                found = await anyio.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                self._hosts[host] = bool(found) and all(is_public(str(f[4][0])) for f in found)
            except (OSError, UnicodeError):
                self._hosts[host] = False
        return self._hosts[host]

    async def _guard(self, route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if request.resource_type in _SKIPPED or parts.scheme not in ("http", "https"):
            await route.abort()
        elif parts.port not in (None, 80, 443) or not await self._public(parts.hostname or ""):
            await route.abort("addressunreachable")
        else:
            await route.continue_()

    async def render(self, url: str) -> dict[str, Any] | None:
        """``{final_url, status_code, html}`` of the page after its scripts ran, or None."""
        if self._browser is None:
            return None
        async with self._pages:
            context = await self._browser.new_context(
                user_agent=self.cfg.user_agent, service_workers="block"
            )
            try:
                page = await context.new_page()
                await page.route("**/*", self._guard)
                response = await page.goto(
                    url, timeout=self.cfg.timeout_seconds * 1000, wait_until="load"
                )
                await page.wait_for_timeout(_SETTLE_MS)
                html = (await page.content())[:MAX_BYTES]
                return {
                    "final_url": page.url,
                    "status_code": response.status if response else None,
                    "html": html,
                }
            except Exception as exc:  # a page that will not render is just not readable
                log.info("browser_render_failed", url=url, error=type(exc).__name__)
                return None
            finally:
                await context.close()
