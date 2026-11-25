"""Playwright browser management — connects to existing Chrome via CDP."""

import json
import os
import asyncio
import platform
import random
import logging
import shutil
import subprocess
import urllib.request
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
        self.connection_mode: str = "disconnected"

    @staticmethod
    def _ensure_no_proxy_localhost():
        """Ensure localhost and 127.0.0.1 bypass system HTTP proxies for CDP."""
        for env_key in ("NO_PROXY", "no_proxy"):
            existing = os.environ.get(env_key, "")
            entries = [item.strip() for item in existing.split(",") if item.strip()]
            lowered = {item.lower() for item in entries}
            updated = False
            for host in ("localhost", "127.0.0.1"):
                if host not in lowered:
                    entries.append(host)
                    lowered.add(host)
                    updated = True
            if updated:
                os.environ[env_key] = ",".join(entries)

    @staticmethod
    def _cdp_candidate_urls() -> list[str]:
        """Build deduplicated CDP endpoint URLs, preferring IPv4 127.0.0.1 fallback."""
        urls: list[str] = []
        configured = (os.getenv("BOSS_CDP_URL") or CDP_URL).strip()
        if configured:
            urls.append(configured)
            if "://localhost:" in configured:
                urls.append(configured.replace("://localhost:", "://127.0.0.1:", 1))
        for port in CDP_DETECT_PORTS:
            urls.append(f"http://127.0.0.1:{port}")
            urls.append(f"http://localhost:{port}")
        deduped: list[str] = []
        seen = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                deduped.append(url)
        return deduped

    @staticmethod
    def _is_cdp_ready(port: int, timeout: float = 2.0) -> bool:
        """Check local Chrome debug endpoint directly without using system proxy."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for host in ("127.0.0.1", "localhost"):
            try:
                with opener.open(f"http://{host}:{port}/json/version", timeout=timeout) as resp:
                    if getattr(resp, "status", 200) == 200:
                        return True
            except Exception:
                continue
        return False

    @staticmethod
    def _find_system_chrome() -> str | None:
        """Locate a system Chrome/Edge binary across macOS, Windows, and Linux."""
        override = os.getenv("BOSS_CHROME_PATH", "").strip()
        if override and os.path.exists(override):
            return override

        system = platform.system()
        candidates: list[str] = []
        if system == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        elif system == "Windows":
            local_app_data = os.getenv("LOCALAPPDATA", "")
            program_files = os.getenv("PROGRAMFILES", r"C:\Program Files")
            program_files_x86 = os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
            for base in (program_files, program_files_x86, local_app_data):
                if not base:
                    continue
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        else:
            for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
                found = shutil.which(binary)
                if found:
                    candidates.append(found)

        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    async def launch(self):
        """Connect to Chrome via CDP with automatic fallback."""
        self._ensure_no_proxy_localhost()
        self._playwright = await async_playwright().start()

        # Strategy 1 & 2: Try configured CDP URL and auto-detect common debug ports
        for cdp_url in self._cdp_candidate_urls():
            if await self._try_cdp_connect(cdp_url):
                self.connection_mode = "cdp"
                log.info(f"Connected via CDP: {cdp_url}")
                return

        # Strategy 3: Launch system Chrome with debug port, then connect via CDP
        log.info("No running Chrome found, launching system Chrome with debug port")
        launched_port = await self._launch_system_chrome()
        if launched_port:
            for host in ("127.0.0.1", "localhost"):
                cdp_url = f"http://{host}:{launched_port}"
                if await self._try_cdp_connect(cdp_url):
                    self.connection_mode = "cdp"
                    log.info(f"Connected to system Chrome at {cdp_url}")
                    return

        # Strategy 4: Fallback to persistent browser context
        log.info("System Chrome CDP launch failed, falling back to persistent browser context")
        await self._launch_new_browser()

    async def _launch_system_chrome(self) -> int | None:
        """Launch system Chrome with --remote-debugging-port.

        Uses a dedicated user-data-dir so Chrome can expose the debug port even when
        another Chrome window is already open.
        Returns the debug port if successful, None otherwise.
        """
        port = 9222
        chrome_path = self._find_system_chrome()
        if not chrome_path:
            log.warning("System Chrome/Edge executable not found")
            return None

        # Use a dedicated user-data-dir so debug port works even if Chrome was running
        profile_dir = os.path.join(os.path.dirname(__file__), "chrome-profile")
        try:
            subprocess.Popen(
                [
                    chrome_path,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for Chrome to start and debug port to be ready
            for _ in range(15):
                await asyncio.sleep(1)
                if self._is_cdp_ready(port):
                    return port
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
        """Fallback: launch a persistent Chrome/Chromium context without automation banner."""
        profile_dir = os.path.join(os.path.dirname(__file__), "chrome-profile")
        os.makedirs(profile_dir, exist_ok=True)
        launch_kwargs = {
            "headless": False,
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                profile_dir,
                channel="chrome",
                **launch_kwargs,
            )
            self.connection_mode = "persistent_chrome"
        except Exception as exc:
            log.warning("Persistent Chrome channel unavailable (%s); using Chromium fallback", exc)
            self._context = await self._playwright.chromium.launch_persistent_context(
                profile_dir,
                **launch_kwargs,
            )
            self.connection_mode = "persistent_chromium"

        self._browser = getattr(self._context, "browser", None)
        await self._load_cookies()
        pages = self._context.pages
        self._page = self._select_best_page(pages) or await self._context.new_page()

    async def close(self):
        """Disconnect (does NOT close the user's Chrome)."""
        if self._context:
            await self._save_cookies()
            if self.connection_mode != "cdp":
                try:
                    await self._context.close()
                except Exception:
                    pass
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._context = None
        self._page = None
        self.connection_mode = "disconnected"

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page

    @property
    def is_alive(self) -> bool:
        """Check if browser is still connected."""
        try:
            if self._browser is not None:
                return self._browser.is_connected()
            if self._page is not None:
                return not self._page.is_closed()
            return False
        except Exception:
            return False

    async def _load_cookies(self):
        """Load cookies from file when the context has no existing BOSS cookies."""
        if not self._context or not os.path.exists(COOKIES_FILE):
            return
        try:
            existing = await self._context.cookies()
            if any("zhipin.com" in (c.get("domain") or "") for c in existing):
                return
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and cookies:
                await self._context.add_cookies(cookies)
        except Exception as exc:
            log.warning("Failed to load saved cookies: %s", exc)

    async def _save_cookies(self):
        """Save cookies to file."""
        os.makedirs(COOKIES_DIR, exist_ok=True)
        try:
            cookies = await self._context.cookies()
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
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
                    || text.includes('请完成验证');
            }""")
            if has_verify:
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

        blank_hits = 0
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval)
            logged_page = await self._find_logged_in_page()
            if logged_page is not None:
                self._page = logged_page
                await self._save_cookies()
                return {"status": "success", "message": "登录成功，Cookie 已保存", "url": self.page.url}

            try:
                polled_url = (self.page.url or "").strip()
            except Exception:
                polled_url = ""

            if polled_url.lower() in ("", "about:blank"):
                blank_hits += 1
                if blank_hits >= 2:
                    return {
                        "status": "blocked_about_blank",
                        "connection_mode": self.connection_mode,
                        "message": (
                            "登录页自动跳转至 about:blank 白屏（通常是 BOSS 检测到非 CDP 自动化浏览器环境）。"
                            "请先手动以调试端口启动系统 Chrome（--remote-debugging-port=9222），"
                            "在 Chrome 中打开 https://www.zhipin.com 完成登录后，再重新调用 MCP 工具。"
                        ),
                    }
                continue

            blank_hits = 0
            if self._is_verification_url(polled_url):
                verify = await self.check_and_screenshot_verification()
                if verify:
                    return verify

        return {"status": "timeout", "message": "登录超时（3分钟），请在浏览器中完成登录后重试"}

    async def random_delay(self):
        """Random delay to mimic human behavior."""
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        await asyncio.sleep(delay)
