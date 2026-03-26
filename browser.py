"""Playwright browser management — connects to existing Chrome via CDP."""

import json
import os
import asyncio
import random
import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import (
    COOKIES_DIR, COOKIES_FILE,
    MIN_DELAY, MAX_DELAY, BOSS_BASE_URL
)

log = logging.getLogger("boss-browser")

# CDP endpoint for Chrome launched with --remote-debugging-port
CDP_URL = os.getenv("BOSS_CDP_URL", "http://localhost:9222")
CDP_DETECT_PORTS = [9222, 9229, 19222]


class BossBrowser:
    """Connects to an existing Chrome via CDP for BOSS 直聘.

    Supports multiple connection strategies:
    1. CDP connect to user-specified port (CDP_URL env var)
    2. Auto-detect Chrome debug port on common ports
    3. Fallback: launch a new Chromium instance
    """

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def launch(self):
        """Connect to Chrome via CDP with automatic fallback."""
        self._playwright = await async_playwright().start()

        # Strategy 1: Try configured CDP URL
        if await self._try_cdp_connect(CDP_URL):
            log.info(f"Connected via CDP: {CDP_URL}")
            return

        # Strategy 2: Auto-detect Chrome debug port
        for port in CDP_DETECT_PORTS:
            url = f"http://localhost:{port}"
            if url == CDP_URL:
                continue  # already tried
            if await self._try_cdp_connect(url):
                log.info(f"Auto-detected Chrome at port {port}")
                return

        # Strategy 3: Launch new Chromium
        log.info("No running Chrome found, launching new Chromium instance")
        await self._launch_new_browser()

    async def _try_cdp_connect(self, url: str) -> bool:
        """Try to connect to Chrome via CDP at the given URL."""
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                url, timeout=5000
            )
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="zh-CN",
                )
                self._page = await self._context.new_page()
            return True
        except Exception:
            return False

    async def _launch_new_browser(self):
        """Fallback: launch a new Chromium instance (user must log in manually)."""
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        await self._load_cookies()
        self._page = await self._context.new_page()

    async def close(self):
        """Disconnect (does NOT close the user's Chrome)."""
        if self._context:
            await self._save_cookies()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._page = None

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    @property
    def is_alive(self) -> bool:
        """Check if browser is still connected."""
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:
            return False

    async def _load_cookies(self):
        """Load cookies from file."""
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r") as f:
                cookies = json.load(f)
            await self._context.add_cookies(cookies)

    async def _save_cookies(self):
        """Save cookies to file."""
        os.makedirs(COOKIES_DIR, exist_ok=True)
        try:
            cookies = await self._context.cookies()
            with open(COOKIES_FILE, "w") as f:
                json.dump(cookies, f, indent=2)
        except Exception:
            pass

    async def is_logged_in(self) -> bool:
        """Check if currently logged in to BOSS 直聘."""
        await self.page.goto(BOSS_BASE_URL, wait_until="networkidle")
        await asyncio.sleep(3)
        current_url = self.page.url
        if "login" in current_url or "/web/user" in current_url or "bticket" in current_url:
            return False
        body_class = await self.page.evaluate("document.body.className || ''")
        if "login" in body_class:
            return False
        try:
            logged_in = await self.page.query_selector(".user-nav, .btn-post-job, .nav-figure, .menu-list")
            return logged_in is not None
        except Exception:
            return False

    async def _check_current_page_logged_in(self) -> bool:
        """Check login state from current page WITHOUT navigating away."""
        current_url = self.page.url
        if "login" in current_url or "/web/user" in current_url or "bticket" in current_url:
            body_class = await self.page.evaluate("document.body.className || ''")
            if "login" in body_class:
                return False
        if "/web/boss/" in current_url or "/web/chat/" in current_url:
            return True
        try:
            logged_in = await self.page.query_selector(".user-nav, .btn-post-job, .nav-figure, .menu-list")
            return logged_in is not None
        except Exception:
            return False

    async def login(self) -> dict:
        """Navigate to login page. User needs to manually complete login."""
        await self.page.goto(f"{BOSS_BASE_URL}/web/user/?ka=header-login", wait_until="domcontentloaded")

        for _ in range(90):
            await asyncio.sleep(2)
            if await self._check_current_page_logged_in():
                await self._save_cookies()
                return {"status": "success", "message": "登录成功，Cookie 已保存"}

        return {"status": "timeout", "message": "登录超时（3分钟），请在浏览器中完成登录后重试"}

    async def random_delay(self):
        """Random delay to mimic human behavior."""
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        await asyncio.sleep(delay)
