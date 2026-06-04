"""
Stealth scrape API — give a URL, get back the page's text and a screenshot URL.

Run:
    pip install -r requirements.txt
    playwright install chromium
    python server.py                 # serves on http://0.0.0.0:8080

Endpoints:
    GET  /health
    GET  /shots/<file>.png                      (static — the saved screenshots)
    POST /scrape   {"url": "...", "full_page": true, "scroll": true, "screenshot": true}
    GET  /scrape?url=...&full_page=true&scroll=true&screenshot=true

Response (JSON):
    {
      "success": true,
      "url": "...", "final_url": "...", "status": 200, "title": "...",
      "text": "<rendered visible text>",
      "screenshot_url": "http://host:8080/shots/<id>.png",   # null if screenshot=false
      "screenshot_file": "<id>.png"
    }

Each screenshot is written to ./shots and served back as a URL. Pass "inline": true
to get the raw base64 in the response instead of saving a file.

A single Chrome instance is shared across requests; each request runs in its own
isolated context with a fresh virtual identity + stealth patches. Built on aiohttp
so it shares Playwright's asyncio event loop with no extra moving parts.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from typing import Optional

import dotenv
from aiohttp import web

from browser import StealthBrowser

dotenv.load_dotenv()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
# Real Google Chrome usually isn't in a container image; set PREFER_CHROME=false there
# to skip the chrome channel and use bundled Chromium directly (no startup warning).
PREFER_CHROME = os.environ.get("PREFER_CHROME", "true").lower() != "false"
MAX_CONCURRENCY = max(1, int(os.environ.get("MAX_CONCURRENCY", "10")))
NAV_TIMEOUT_MS = max(5000, int(os.environ.get("NAV_TIMEOUT_MS", "45000")))

# Where screenshots are written and how they're linked back.
_HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS_DIR = os.environ.get("SHOTS_DIR") or os.path.join(_HERE, "shots")
# When behind a proxy/domain, set this so URLs are public (e.g. https://scrape.example.com).
# If unset, the URL is derived from the incoming request's scheme + host.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Auto-delete screenshots older than this many hours (0 = keep forever).
SHOTS_TTL_HOURS = float(os.environ.get("SHOTS_TTL_HOURS", "24"))


def _as_bool(value, default: bool = True) -> bool:
    """Coerce query-string / JSON values to bool (query params arrive as strings)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _prune_old_shots() -> None:
    """Best-effort delete of screenshots older than SHOTS_TTL_HOURS."""
    if SHOTS_TTL_HOURS <= 0:
        return
    cutoff = time.time() - SHOTS_TTL_HOURS * 3600
    try:
        for name in os.listdir(SHOTS_DIR):
            path = os.path.join(SHOTS_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except FileNotFoundError:
        pass


async def _auto_scroll(page) -> None:
    """Scroll to the bottom in steps so lazy-loaded content renders."""
    await page.evaluate(
        """async () => {
            await new Promise((resolve) => {
                let total = 0;
                const step = 600;
                const timer = setInterval(() => {
                    window.scrollBy(0, step);
                    total += step;
                    if (total >= document.body.scrollHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 150);
            });
        }"""
    )
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


async def _scrape(app: web.Application, base_url: str, url: str, full_page: bool,
                  scroll: bool, screenshot: bool, inline: bool) -> dict:
    """Open the URL in a fresh stealth context and return text + screenshot."""
    if "://" not in url:
        url = "https://" + url
    sb: StealthBrowser = app["sb"]
    sem: asyncio.Semaphore = app["sem"]

    async with sem:
        context, page = await sb.new_identity_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            if scroll:
                await _auto_scroll(page)

            text = await page.evaluate("() => document.body ? document.body.innerText : ''")

            result = {
                "success": True,
                "url": url,
                "final_url": page.url,
                "status": resp.status if resp else None,
                "title": await page.title(),
                "text": text,
                "screenshot_url": None,
                "screenshot_file": None,
            }

            if screenshot:
                if inline:
                    png = await page.screenshot(full_page=full_page)
                    result["screenshot_base64"] = base64.b64encode(png).decode("ascii")
                    result["screenshot_format"] = "png"
                else:
                    filename = f"{uuid.uuid4().hex}.png"
                    path = os.path.join(SHOTS_DIR, filename)
                    await page.screenshot(path=path, full_page=full_page)
                    result["screenshot_file"] = filename
                    result["screenshot_url"] = f"{base_url}/shots/{filename}"

            return result
        finally:
            await context.close()


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def _base_url(request: web.Request) -> str:
    return PUBLIC_BASE_URL or f"{request.scheme}://{request.host}"


async def health(request: web.Request) -> web.Response:
    sb: Optional[StealthBrowser] = request.app.get("sb")
    return web.json_response(
        {"status": "ok", "browser": (sb.channel or "chromium") if sb else "starting"}
    )


async def scrape(request: web.Request) -> web.Response:
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "invalid JSON body"}, status=400)
    else:
        data = dict(request.query)

    url = (data.get("url") or "").strip()
    if not url:
        return web.json_response({"success": False, "error": "missing 'url'"}, status=400)

    full_page = _as_bool(data.get("full_page", True))
    scroll = _as_bool(data.get("scroll", True))
    screenshot = _as_bool(data.get("screenshot", True))
    inline = _as_bool(data.get("inline", False), default=False)

    try:
        result = await _scrape(
            request.app, _base_url(request), url, full_page, scroll, screenshot, inline
        )
        return web.json_response(result)
    except Exception as e:
        return web.json_response(
            {"success": False, "url": url, "error": str(e)}, status=502
        )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def _on_startup(app: web.Application) -> None:
    os.makedirs(SHOTS_DIR, exist_ok=True)
    _prune_old_shots()
    sb = StealthBrowser(headless=HEADLESS, prefer_chrome=PREFER_CHROME)
    await sb.launch()
    app["sb"] = sb
    app["sem"] = asyncio.Semaphore(MAX_CONCURRENCY)
    print(f"[server] browser ready ({sb.channel or 'bundled chromium'}), "
          f"concurrency={MAX_CONCURRENCY}, shots -> {SHOTS_DIR}")


async def _on_cleanup(app: web.Application) -> None:
    sb: Optional[StealthBrowser] = app.get("sb")
    if sb:
        await sb.close()
    print("[server] browser closed")


def make_app() -> web.Application:
    os.makedirs(SHOTS_DIR, exist_ok=True)
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.add_routes([
        web.get("/health", health),
        web.get("/scrape", scrape),
        web.post("/scrape", scrape),
        web.static("/shots", SHOTS_DIR),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT)
