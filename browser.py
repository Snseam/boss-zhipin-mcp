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

        # Strategy 3: Launch system Chrome with debug port, then connect via CDP
        log.info("No running Chrome found, launching system Chrome with debug port")
        launched_port = await self._launch_system_chrome()
        if launched_port:
            cdp_url = f"http://localhost:{launched_port}"
            if await self._try_cdp_connect(cdp_url):
                log.info(f"Connected to system Chrome at port {launched_port}")
                return

        # Strategy 4: Fallback to bare Chromium (no user profile)
        log.info("System Chrome launch failed, falling back to bare Chromium")
        await self._launch_new_browser()

    async def _launch_system_chrome(self) -> int | None:
        """Launch system Chrome with --remote-debugging-port.

        Uses the user's default Chrome profile so existing cookies/logins are available.
        Returns the debug port if successful, None otherwise.
        """
        import subprocess
        import platform

        port = 9222
        system = platform.system()
        if system == "Darwin":
            chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        elif system == "Linux":
            chrome_path = "google-chrome"
        else:
            chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        # Use a dedicated user-data-dir so debug port works even if Chrome was running
        profile_dir = os.path.join(os.path.dirname(__file__), "chrome-profile")
        try:
            subprocess.Popen(
                [chrome_path, f"--remote-debugging-port={port}",
                 f"--user-data-dir={profile_dir}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for Chrome to start and debug port to be ready
            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
                    return port
                except Exception:
                    continue
        except FileNotFoundError:
            log.warning(f"Chrome not found at {chrome_path}")
        except Exception as e:
            log.warning(f"Failed to launch system Chrome: {e}")
        return None

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
                self._page = self._select_best_page(pages) or await self._context.new_page()
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="zh-CN",
                )
                self._page = await self._context.new_page()
            return True
        except Exception:
            return False

    @staticmethod
    def _is_verification_url(url: str) -> bool:
        lower = (url or "").lower()
        return any(
            kw in lower for kw in ("verify-slider", "/safe/verify", "captcha", "/web/user/safe")
        )

    @classmethod
    def _is_login_url(cls, url: str) -> bool:
        lower = (url or "").lower()
        if not lower or cls._is_verification_url(lower):
            return False
        return any(
            kw in lower for kw in ("login.zhipin.com", "/web/user", "bticket", "/login")
        )

    @classmethod
    def _is_recruiter_url(cls, url: str) -> bool:
        lower = (url or "").lower()
        if "zhipin.com" not in lower or cls._is_login_url(lower) or cls._is_verification_url(lower):
            return False
        return "/web/chat/" in lower or "/web/boss/" in lower

    @classmethod
    def _select_best_page(cls, pages: list[Page]) -> Page | None:
        """Prefer an existing BOSS recruiter tab over arbitrary or blank tabs."""
        best_page = None
        best_score = -1
        for candidate in pages or []:
            try:
                if hasattr(candidate, "is_closed") and candidate.is_closed():
                    continue
                url = candidate.url or ""
            except Exception:
                continue
            lower = url.lower()
            if cls._is_recruiter_url(lower):
                score = 4
            elif "zhipin.com" in lower and not cls._is_login_url(lower) and not cls._is_verification_url(lower):
                score = 3
            elif "zhipin.com" in lower:
                score = 2
            elif lower and not lower.startswith(("about:", "chrome:", "devtools:")):
                score = 1
            else:
                score = 0
            if score > best_score:
                best_score = score
                best_page = candidate
        return best_page

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
        """Check if currently logged in to BOSS 直聘.

        Checks the current tab and any other open BOSS tabs without navigating first.
        Avoids navigating away from active login or verification screens.
        """
        logged_page = await self._find_logged_in_page()
        if logged_page is not None:
            self._page = logged_page
            return True

        try:
            current_url = self.page.url or ""
        except Exception:
            current_url = ""

        if self._is_verification_url(current_url) or self._is_login_url(current_url):
            return False
        if "zhipin.com" in current_url.lower():
            return False

        # Current page is not BOSS — navigate once to recruiter portal (not consumer homepage)
        await self.page.goto(f"{BOSS_BASE_URL}/web/chat/recommend", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        return await self._check_current_page_logged_in()

    async def _find_logged_in_page(self) -> Page | None:
        """Scan current and open pages for an already-authenticated BOSS tab."""
        candidates: list[Page] = []
        if self._page is not None:
            candidates.append(self._page)
        if self._context is not None:
            try:
                for page in self._context.pages:
                    if page not in candidates:
                        candidates.append(page)
            except Exception:
                pass

        for candidate in candidates:
            try:
                if hasattr(candidate, "is_closed") and candidate.is_closed():
                    continue
                if await self._check_current_page_logged_in(candidate):
                    return candidate
            except Exception:
                continue
        return None

    async def _check_current_page_logged_in(self, target_page: Page | None = None) -> bool:
        """Check login state from a page WITHOUT navigating away."""
        page = target_page or self.page
        try:
            if hasattr(page, "is_closed") and page.is_closed():
                return False
            current_url = page.url or ""
        except Exception:
            return False

        lower_url = current_url.lower()
        if not lower_url or lower_url.startswith("about:") or "zhipin.com" not in lower_url:
            return False
        if self._is_verification_url(lower_url) or self._is_login_url(lower_url):
            return False

        if self._is_recruiter_url(lower_url):
            try:
                blocked = await page.evaluate("""() => {
                    const bodyClass = (document.body && document.body.className) || '';
                    if (bodyClass.includes('login')) return true;
                    const blockers = [
                        '.login-form', '.sign-form', '.qr-code-box',
                        '.verify-wrap', '.slider-verify'
                    ];
                    for (const sel of blockers) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetWidth > 0) return true;
                    }
                    return false;
                }""")
                return not blocked
            except Exception:
                # Page may be mid-navigation; let callers retry instead of crashing
                return False

        try:
            logged_in = await page.query_selector(
                ".user-nav, .btn-post-job, .nav-figure, .menu-list, "
                ".nav-chat, dl.menu-geeksearch, a[ka='menu-geek-search'], .boss-info"
            )
            return logged_in is not None
        except Exception:
            return False

    async def check_and_screenshot_verification(self) -> dict | None:
        """Check if the current page has a verification/CAPTCHA overlay.

        Returns screenshot info if verification detected, None otherwise.
        """
        try:
            current_url = (self.page.url or "").lower()
            url_is_verify = any(
                kw in current_url for kw in ("verify-slider", "/safe/verify", "captcha", "/web/user/safe")
            )
            has_verify = await self.page.evaluate("""() => {
                const text = document.body.innerText || '';
                const selectors = [
                    '.verify-wrap', '.captcha', '.slider-verify',
                    '[class*="verify"]', '[class*="captcha"]',
                    '.boss-popup__wrapper'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetWidth > 0) return true;
                }
                return text.includes('安全验证') || text.includes('滑动验证')
                    || text.includes('请完成验证') || text.includes('操作过于频繁')
                    || text.includes('行为异常') || text.includes('访问异常');
            }""")
            if url_is_verify or has_verify:
                screenshot_path = os.path.join(
                    os.path.dirname(__file__), "screenshot_verification.png"
                )
                await self.page.screenshot(path=screenshot_path)
                return {
                    "needs_verification": True,
                    "screenshot": screenshot_path,
                    "message": "检测到安全验证，请在浏览器中手动完成验证后告知",
                }
        except Exception:
            pass
        return None

    async def login(self, poll_interval: float = 2.0, max_polls: int = 90) -> dict:
        """Navigate to login page. User needs to manually complete login."""
        logged_page = await self._find_logged_in_page()
        if logged_page is not None:
            self._page = logged_page
            await self._save_cookies()
            return {"status": "success", "message": "已登录，Cookie 已保存", "url": self.page.url}

        verify = await self.check_and_screenshot_verification()
        if verify:
            return verify

        current_url = ""
        try:
            current_url = self.page.url or ""
        except Exception:
            pass

        if not self._is_login_url(current_url):
            await self.page.goto(
                f"{BOSS_BASE_URL}/web/user/?ka=header-login",
                wait_until="domcontentloaded",
            )

        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            logged_page = await self._find_logged_in_page()
            if logged_page is not None:
                self._page = logged_page
                await self._save_cookies()
                return {"status": "success", "message": "登录成功，Cookie 已保存", "url": self.page.url}

            try:
                polled_url = self.page.url or ""
            except Exception:
                polled_url = ""
            if self._is_verification_url(polled_url):
                verify = await self.check_and_screenshot_verification()
                if verify:
                    return verify

        return {"status": "timeout", "message": "登录超时（3分钟），请在浏览器中完成登录后重试"}

    async def random_delay(self):
        """Random delay to mimic human behavior."""
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        await asyncio.sleep(delay)
