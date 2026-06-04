"""
StealthBrowser — async Playwright wrapper that launches a hardened Chrome/Chromium
and hands out browser contexts pre-loaded with a unique virtual identity.

Usage:

    import asyncio
    from browser import StealthBrowser

    async def main():
        async with StealthBrowser(headless=False) as sb:
            context, page = await sb.new_identity_page()
            await page.goto("https://example.com")
            await page.screenshot(path="out.png")

    asyncio.run(main())

Design notes:
  * We launch *real* Chrome (`channel="chrome"`) when available — its UA, codecs and
    behaviour are the most convincing — and transparently fall back to Playwright's
    bundled Chromium otherwise.
  * The TLS (JA3/JA4) and HTTP/2 fingerprint of this browser is already genuine Chrome,
    because it *is* Chrome. We do not (and cannot) spoof that from Python here; for the
    raw-HTTP path see fingerprint_http.py.
  * Each context gets a fresh `VirtualIdentity` and the stealth init script, so the
    UA / client-hint headers (network layer) and the JS-readable signals agree.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple, Dict, Any

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from identity import VirtualIdentity, generate_identity
from stealth import build_stealth_script

# Flags that quiet the most common automation tells. We explicitly drop
# "--enable-automation" (which sets navigator.webdriver and the "Chrome is being
# controlled by automated test software" infobar) via ignore_default_args.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-infobars",
    "--disable-popup-blocking",
    "--disable-features=IsolateOrigins,site-per-process,Translate",
    "--start-maximized",
]

_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


class StealthBrowser:
    """A single shared browser; spawns one isolated identity per context."""

    def __init__(
        self,
        headless: bool = False,
        proxy: Optional[Dict[str, str]] = None,
        prefer_chrome: bool = True,
    ) -> None:
        """
        Args:
            headless: Run without a visible window. `False` is the most convincing.
                      For servers, Playwright's headless Chromium is "new headless"
                      and far less detectable than the legacy mode.
            proxy:    Playwright proxy dict, e.g.
                      {"server": "http://host:port", "username": ..., "password": ...}.
                      Applied at the browser level; can be overridden per context.
            prefer_chrome: Try real Chrome first, fall back to bundled Chromium.
        """
        self.headless = headless
        self.proxy = proxy
        self.prefer_chrome = prefer_chrome

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.channel: Optional[str] = None  # "chrome" or None (=bundled Chromium)

    async def __aenter__(self) -> "StealthBrowser":
        await self.launch()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def launch(self) -> Browser:
        """Start Playwright and launch the browser, preferring real Chrome."""
        self._pw = await async_playwright().start()

        args = list(_LAUNCH_ARGS)
        # In containers Chromium runs as root and the kernel sandbox is usually
        # unavailable, so it must be disabled. Set NO_SANDBOX=true in Docker.
        if os.environ.get("NO_SANDBOX", "").lower() in ("1", "true", "yes", "on"):
            args += ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]

        launch_kwargs: Dict[str, Any] = dict(
            headless=self.headless,
            args=args,
            ignore_default_args=_IGNORE_DEFAULT_ARGS,
        )
        if self.proxy:
            launch_kwargs["proxy"] = self.proxy

        if self.prefer_chrome:
            try:
                self._browser = await self._pw.chromium.launch(
                    channel="chrome", **launch_kwargs
                )
                self.channel = "chrome"
                return self._browser
            except Exception as e:
                print(f"[StealthBrowser] real Chrome unavailable ({e}); "
                      f"falling back to bundled Chromium.")

        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self.channel = None
        return self._browser

    async def new_identity_page(
        self,
        identity: Optional[VirtualIdentity] = None,
        proxy: Optional[Dict[str, str]] = None,
    ) -> Tuple[BrowserContext, Page]:
        """
        Create an isolated context wired to `identity` (generated if omitted) and a
        page inside it. Returns (context, page); close the context when done.
        """
        if self._browser is None:
            raise RuntimeError("Call launch() before new_identity_page().")

        if identity is None:
            identity = generate_identity()

        context_kwargs: Dict[str, Any] = dict(
            user_agent=identity.user_agent,
            locale=identity.locale,
            timezone_id=identity.timezone_id,
            viewport={"width": identity.viewport[0], "height": identity.viewport[1]},
            screen={"width": identity.screen["width"], "height": identity.screen["height"]},
            device_scale_factor=identity.device_scale_factor,
            color_scheme="light",
            is_mobile=False,
            has_touch=False,
            # Client-hint + language headers that must agree with the JS-side identity.
            extra_http_headers={
                "Accept-Language": identity.accept_language,
                "sec-ch-ua": identity.sec_ch_ua,
                "sec-ch-ua-mobile": "?1" if identity.ua_mobile else "?0",
                "sec-ch-ua-platform": f'"{identity.ua_platform}"',
            },
        )
        if proxy:
            context_kwargs["proxy"] = proxy

        context = await self._browser.new_context(**context_kwargs)
        # Inject before any page script runs, in every frame.
        await context.add_init_script(build_stealth_script(identity))
        # Stash the identity so callers/tests can read it back off the context.
        context.virtual_identity = identity  # type: ignore[attr-defined]

        page = await context.new_page()
        return context, page

    async def close(self) -> None:
        """Tear down the browser and Playwright."""
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()
            self._browser = None
            self._pw = None
