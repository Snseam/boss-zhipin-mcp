"""BOSS 直聘 page scraper — works with SPA iframe-based search UI."""

import asyncio
import base64
from datetime import date
import functools
import json
import logging
import os
import random
import tempfile
import time
from browser import BossBrowser
import config as config_mod
from config import BOSS_BASE_URL

try:
    from PIL import Image
    from pyzbar.pyzbar import decode as decode_qr
    import io
    HAS_QR_DECODER = True
except (ImportError, OSError):
    HAS_QR_DECODER = False

log = logging.getLogger("boss-scraper")

SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "boss-recruiter-screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
SEARCH_THROTTLE_FILE = os.path.join(SCREENSHOT_DIR, "search_throttle.json")

SEARCH_MENU_SELECTOR = 'dl.menu-geeksearch, a[ka="menu-geek-search"]'
SEARCH_IFRAME_SELECTOR = "#searchContent iframe"
SEARCH_FRAME_URL_KEYWORDS = ("/web/frame/search", "/web/frame/recommend")


class RiskControlError(Exception):
    """Raised when BOSS 直聘 triggers a verification or risk-control gate."""


class LoginRequiredError(Exception):
    """Raised when the page is on a login screen and user authentication is required."""


# --- Retry decorator for resilient operations ---

def retry(
    max_attempts=3,
    delay=1.0,
    jitter=1.5,
    non_retryable=(RiskControlError, LoginRequiredError),
):
    """Retry async methods with automatic dialog cleanup and jittered backoff."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except non_retryable:
                    raise
                except Exception as e:
                    last_error = e
                    log.warning(f"{func.__name__} attempt {attempt+1} failed: {e}")
                    if attempt < max_attempts - 1:
                        try:
                            await self._cleanup_dialogs()
                        except Exception:
                            pass
                        sleep_time = delay + (random.uniform(0, jitter) if jitter > 0 else 0)
                        await asyncio.sleep(sleep_time)
            raise last_error
        return wrapper
    return decorator

CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "广州": "101280100",
    "深圳": "101280600", "杭州": "101210100", "成都": "101270100",
    "南京": "101190100", "武汉": "101200100", "西安": "101110100",
    "苏州": "101190400", "天津": "101030100", "重庆": "101040100",
    "长沙": "101250100", "郑州": "101180100", "东莞": "101281600",
    "青岛": "101120200", "合肥": "101220100", "佛山": "101280800",
    "宁波": "101210400", "厦门": "101230200", "大连": "101070200",
    "福州": "101230100", "济南": "101120100", "珠海": "101280700",
    "无锡": "101190200", "昆明": "101290100", "哈尔滨": "101050100",
    "沈阳": "101070100", "长春": "101060100", "石家庄": "101090100",
    "太原": "101100100", "南宁": "101300100", "贵阳": "101260100",
    "兰州": "101160100", "海口": "101310100",
}

# JS to extract candidate info from cards (reused in search_candidates)
EXTRACT_CARDS_JS = """() => {
    const cards = document.querySelectorAll("li.geek-info-card");
    const results = [];

    for (let idx = 0; idx < cards.length; idx++) {
        const card = cards[idx];
        const text = card.innerText || "";
        const link = card.querySelector("a[data-contact]");
        const lines = text.split("\\n").map(l => l.trim()).filter(l => l);

        const name = lines[0] || "未知";

        let age = "", experience = "", education = "", jobStatus = "", salary = "";
        for (const line of lines) {
            const parts = line.split(/\\s{2,}/);
            if (parts.length >= 3) {
                for (const part of parts) {
                    if (part.includes("岁")) age = part;
                    else if (part.includes("年")) experience = part;
                    else if (["本科", "硕士", "博士", "大专", "高中"].some(e => part.includes(e))) education = part;
                    else if (part.includes("离职") || part.includes("在职") || part.includes("到岗")) jobStatus = part;
                    else if (part.includes("K") || part.includes("k") || part.includes("面议") || part.includes("薪")) salary = part;
                }
                if (age || experience) break;
            }
        }

        const skillEls = card.querySelectorAll(".rcd-tags span, .tag-item, [class*='tag']");
        const skills = Array.from(skillEls).map(el => el.innerText.trim()).filter(s => s.length > 0 && s.length < 20);

        let expectCity = "";
        const cityIdx = lines.findIndex(l => l === "期望城市");
        if (cityIdx >= 0 && lines[cityIdx + 1]) expectCity = lines[cityIdx + 1];
        // Also check "期望" line
        if (!expectCity) {
            const expIdx = lines.findIndex(l => l === "期望");
            if (expIdx >= 0 && lines[expIdx + 1]) expectCity = lines[expIdx + 1];
        }

        let company = "", title = "";
        const posIdx = lines.findIndex(l => l === "职位");
        if (posIdx >= 0) {
            company = lines[posIdx + 1] || "";
            title = lines[posIdx + 2] || "";
        }

        let school = "", major = "";
        const eduIdx = lines.findIndex(l => l === "院校");
        if (eduIdx >= 0) {
            school = lines[eduIdx + 1] || "";
            major = lines[eduIdx + 2] || "";
        }

        const expectId = link ? link.getAttribute("data-expect") : "";
        const lid = link ? link.getAttribute("data-lid") : "";
        const jid = link ? link.getAttribute("data-jid") : "";

        results.push({
            index: idx, name, age, experience, education, jobStatus, salary,
            skills: skills.slice(0, 8),
            expectCity, company, title, school, major,
            expectId, lid, jid,
            fullText: text.slice(0, 500)
        });
    }
    return results;
}"""


class BossScraper:
    """Scrapes candidate data from BOSS 直聘 recruiter SPA (iframe-based)."""

    def __init__(
        self,
        browser: BossBrowser,
        min_search_interval: float | None = None,
        search_jitter_max: float | None = None,
        max_searches_per_day: int | None = None,
        throttle_file: str | None = None,
    ):
        self.browser = browser
        self._search_frame = None  # cache the search iframe
        self._search_lock = asyncio.Lock()
        self._min_search_interval = (
            float(getattr(config_mod, "SEARCH_MIN_INTERVAL", 15.0))
            if min_search_interval is None
            else float(min_search_interval)
        )
        self._search_jitter_max = (
            float(getattr(config_mod, "SEARCH_JITTER_MAX", 3.0))
            if search_jitter_max is None
            else float(search_jitter_max)
        )
        self._max_searches_per_day = (
            int(getattr(config_mod, "MAX_SEARCHES_PER_DAY", 60))
            if max_searches_per_day is None
            else int(max_searches_per_day)
        )
        self._throttle_file = SEARCH_THROTTLE_FILE if throttle_file is None else throttle_file
        self._last_search_ts = 0.0
        self._daily_search_date = ""
        self._daily_search_count = 0

    async def get_visible_expect_ids(self) -> list[str]:
        """Return all expectIds currently visible on the search page."""
        frame = await self._find_existing_search_frame(self.browser.page, require_visible=False)
        if not frame:
            return []
        try:
            return await frame.evaluate("""() => {
                const links = document.querySelectorAll("li.geek-info-card a[data-contact]");
                const ids = [];
                for (const link of links) {
                    const eid = link.getAttribute('data-expect');
                    if (eid) ids.push(eid);
                }
                return ids;
            }""")
        except Exception:
            if not self._is_frame_alive(frame):
                self._search_frame = None
            return []

    async def _cleanup_dialogs(self):
        """Clean up all dialog overlays that might block interactions."""
        p = self.browser.page
        try:
            await p.evaluate("""() => {
                document.querySelectorAll('div.dialog-wrap').forEach(d => d.remove());
                document.querySelectorAll('.boss-layer__wrapper').forEach(l => l.remove());
                document.querySelectorAll('.boss-popup__wrapper').forEach(l => l.remove());
                document.querySelectorAll('.c-pay-4-another').forEach(el => {
                    const wrapper = el.closest('.boss-popup__wrapper');
                    if (wrapper && wrapper.parentElement) wrapper.parentElement.remove();
                });
            }""")
        except Exception:
            return
        await asyncio.sleep(0.5)

    async def _dismiss_overlays(self, p, toast_timeout: float = 2.0):
        """Clean up blocking dialogs and wait briefly for transient toasts to disappear."""
        await self._cleanup_dialogs()
        if toast_timeout <= 0:
            return
        deadline = time.monotonic() + toast_timeout
        while time.monotonic() < deadline:
            try:
                has_toast = await p.evaluate("""() => {
                    const toasts = document.querySelectorAll('div.toast, .boss-toast');
                    for (const el of toasts) {
                        const style = window.getComputedStyle(el);
                        if (style.display !== 'none' && style.visibility !== 'hidden'
                                && el.offsetWidth > 0 && el.offsetHeight > 0) {
                            return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                return
            if not has_toast:
                return
            await asyncio.sleep(0.2)

    def _load_throttle_state(self) -> tuple[str, int, float]:
        """Load today's search count and last search timestamp."""
        today = date.today().isoformat()
        count = self._daily_search_count if self._daily_search_date == today else 0
        last_ts = self._last_search_ts

        if self._throttle_file and os.path.exists(self._throttle_file):
            try:
                with open(self._throttle_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    file_last_ts = float(data.get("last_search_ts", 0.0) or 0.0)
                    last_ts = max(last_ts, file_last_ts)
                    if data.get("date") == today:
                        count = max(count, int(data.get("count", 0) or 0))
            except Exception:
                pass

        self._daily_search_date = today
        self._daily_search_count = count
        self._last_search_ts = last_ts
        return today, count, last_ts

    def _save_throttle_state(self, today: str, count: int, last_ts: float):
        """Persist search throttle state to temp storage."""
        self._daily_search_date = today
        self._daily_search_count = count
        self._last_search_ts = last_ts
        if not self._throttle_file:
            return
        try:
            parent = os.path.dirname(self._throttle_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self._throttle_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"date": today, "count": count, "last_search_ts": last_ts},
                    f,
                )
        except Exception:
            pass

    def _check_daily_search_limit(self) -> dict | None:
        """Return an error dict if today's search quota is exhausted."""
        if self._max_searches_per_day <= 0:
            return None
        _, count, _ = self._load_throttle_state()
        if count >= self._max_searches_per_day:
            return {
                "error": "rate_limit",
                "message": (
                    f"今日搜索次数已达上限（{count}/{self._max_searches_per_day}），"
                    "为降低风控风险已停止搜索，可通过 BOSS_MAX_SEARCHES_PER_DAY 调整"
                ),
            }
        return None

    async def _wait_for_search_cooldown(self):
        """Enforce minimum interval plus jitter between consecutive searches."""
        if self._min_search_interval <= 0:
            return
        _, _, last_ts = self._load_throttle_state()
        if last_ts <= 0:
            return
        remaining = self._min_search_interval - (time.time() - last_ts)
        if remaining > 0:
            jitter = random.uniform(0, self._search_jitter_max) if self._search_jitter_max > 0 else 0.0
            wait_seconds = remaining + jitter
            log.info(f"Search cooldown active, sleeping {wait_seconds:.1f}s")
            await asyncio.sleep(wait_seconds)

    def _record_search_timestamp(self):
        """Record a search execution timestamp and increment today's counter."""
        today, count, _ = self._load_throttle_state()
        self._save_throttle_state(today, count + 1, time.time())

    def _is_frame_alive(self, frame) -> bool:
        """Return True if the Playwright frame handle is present and still attached."""
        if frame is None:
            return False
        is_detached = getattr(frame, "is_detached", None)
        if callable(is_detached):
            try:
                return not is_detached()
            except Exception:
                return False
        return True

    async def _is_search_frame_usable(self, frame, require_visible: bool = True) -> bool:
        """Check that a frame is attached and has a usable search input."""
        if not self._is_frame_alive(frame):
            return False
        try:
            if require_visible:
                frame_el_fn = getattr(frame, "frame_element", None)
                if callable(frame_el_fn):
                    frame_el = await frame_el_fn()
                    el_visible = getattr(frame_el, "is_visible", None)
                    if callable(el_visible) and not await el_visible():
                        return False
            search_input = await frame.query_selector("input.search-input")
            if not search_input:
                return False
            if require_visible:
                is_visible = getattr(search_input, "is_visible", None)
                if callable(is_visible):
                    return bool(await is_visible())
            return True
        except Exception:
            return False

    async def _find_existing_search_frame(self, p, require_visible: bool = True):
        """Return an already-attached search frame without navigating or clicking menus."""
        if self._search_frame is not None:
            if self._is_frame_alive(self._search_frame):
                if not require_visible or await self._is_search_frame_usable(
                    self._search_frame, require_visible=True
                ):
                    return self._search_frame
            else:
                self._search_frame = None

        try:
            iframe_el = await p.query_selector(SEARCH_IFRAME_SELECTOR)
            if iframe_el:
                frame = await iframe_el.content_frame()
                if self._is_frame_alive(frame):
                    if not require_visible or await self._is_search_frame_usable(
                        frame, require_visible=True
                    ):
                        self._search_frame = frame
                        return frame
        except Exception:
            pass

        for frame in getattr(p, "frames", []) or []:
            if not self._is_frame_alive(frame):
                continue
            frame_url = getattr(frame, "url", "") or ""
            if any(keyword in frame_url for keyword in SEARCH_FRAME_URL_KEYWORDS):
                if not require_visible or await self._is_search_frame_usable(
                    frame, require_visible=True
                ):
                    self._search_frame = frame
                    return frame

        return None

    async def _detect_risk_control(self, p) -> str | None:
        """Detect BOSS 直聘 risk-control / verification gates on the current page."""
        url = (getattr(p, "url", "") or "").lower()
        for token in ("verify-slider", "/safe/verify", "captcha", "/web/user/safe"):
            if token in url:
                return f"url:{token}"

        try:
            reason = await p.evaluate("""() => {
                const selectors = [
                    '.verify-wrap', '.captcha', '.slider-verify',
                    '#wrap-verify', '.geetest_holder', '.geetest_panel',
                    '[class*="verify-slider"]', '[class*="captcha"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetWidth > 0 && el.offsetHeight > 0) {
                        return 'selector:' + sel;
                    }
                }
                const text = (document.body && document.body.innerText) || '';
                const keywords = [
                    '安全验证', '滑动验证', '请完成验证',
                    '请进行安全验证', '操作过于频繁', '行为异常',
                    '访问异常', '账号存在异常'
                ];
                for (const kw of keywords) {
                    if (text.includes(kw)) return 'text:' + kw;
                }
                return null;
            }""")
            return reason or None
        except Exception:
            return None

    async def _is_login_page(self, p) -> bool:
        """Return True if the page is currently a login screen."""
        url = (getattr(p, "url", "") or "").lower()
        if not any(token in url for token in ("login", "/web/user", "bticket")):
            return False
        try:
            body_class = await p.evaluate("(document.body && document.body.className) || ''")
            if "login" in str(body_class).lower():
                return True
        except Exception:
            pass
        try:
            search_menu = await p.query_selector(SEARCH_MENU_SELECTOR)
            return search_menu is None
        except Exception:
            return True

    async def _build_risk_control_response(self, reason: str) -> dict:
        """Build a structured risk-control error payload, capturing screenshot when possible."""
        payload = {
            "error": "risk_control",
            "needs_verification": True,
            "reason": reason,
            "message": f"检测到风控/安全验证（{reason}），已停止后续操作，请在浏览器中手动处理",
        }
        checker = getattr(self.browser, "check_and_screenshot_verification", None)
        if callable(checker):
            try:
                verify = await checker()
                if isinstance(verify, dict) and verify.get("screenshot"):
                    payload["screenshot"] = verify["screenshot"]
            except Exception:
                pass
        return payload

    async def _click_search_menu(self, p) -> bool:
        """Click the SPA search menu safely even when transient overlays/frames intercept pointers."""
        await self._dismiss_overlays(p)
        search_menu = await p.query_selector(SEARCH_MENU_SELECTOR)
        if not search_menu:
            return False

        try:
            await search_menu.click(timeout=3000)
            return True
        except Exception as e:
            log.warning(f"Normal search menu click failed, trying force/JS click: {e}")

        risk_reason = await self._detect_risk_control(p)
        if risk_reason:
            raise RiskControlError(risk_reason)

        try:
            search_menu = await p.query_selector(SEARCH_MENU_SELECTOR)
            if search_menu:
                await search_menu.click(force=True, timeout=3000)
                return True
        except Exception as e:
            log.warning(f"Force search menu click failed, falling back to JS click: {e}")

        try:
            clicked = await p.evaluate("""() => {
                const el = document.querySelector('a[ka="menu-geek-search"]')
                    || document.querySelector('dl.menu-geeksearch a')
                    || document.querySelector('dl.menu-geeksearch');
                if (!el) return false;
                el.click();
                return true;
            }""")
            return bool(clicked)
        except Exception:
            return False

    async def _find_share_icon_position(self, resume_frame) -> tuple[int, int] | None:
        """Dynamically calculate the forward icon position on canvas."""
        canvas_size = await resume_frame.evaluate("""() => {
            const c = document.querySelector('canvas');
            return c ? {w: c.offsetWidth, h: c.offsetHeight} : null;
        }""")
        if not canvas_size:
            return None
        # Forward icon is at ~94% width, ~9.5% height of canvas
        x = int(canvas_size['w'] * 0.938)
        y = int(canvas_size['h'] * 0.095)
        return (x, y)

    async def _get_search_frame(self, p):
        """Return a live search iframe, reusing existing frame and navigating at most once."""
        risk_reason = await self._detect_risk_control(p)
        if risk_reason:
            raise RiskControlError(risk_reason)

        # 1. Reuse already-attached, visible search frame without navigating or clicking menu
        frame = await self._find_existing_search_frame(p, require_visible=True)
        if frame:
            return frame

        # 2. Do not navigate away from a login page in a loop
        if await self._is_login_page(p):
            raise LoginRequiredError("当前处于登录页或登录已失效，请先完成登录")

        # 3. Navigate AT MOST ONCE (strictly outside retry) only when not on recruiter SPA
        url = getattr(p, "url", "") or ""
        on_recruiter_spa = ("/web/boss/" in url or "/web/chat/" in url)
        search_menu = await p.query_selector(SEARCH_MENU_SELECTOR)
        if not search_menu and not on_recruiter_spa:
            from config import BOSS_SEARCH_URL
            log.info(f"Navigating to recruiter search page: {BOSS_SEARCH_URL}")
            await p.goto(BOSS_SEARCH_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            risk_reason = await self._detect_risk_control(p)
            if risk_reason:
                raise RiskControlError(risk_reason)
            if await self._is_login_page(p):
                raise LoginRequiredError("页面跳转到登录页，请先完成登录")

            frame = await self._find_existing_search_frame(p, require_visible=True)
            if frame:
                return frame

        # 4. Switch SPA tabs once; only frame lookup is retried
        try:
            clicked = await self._click_search_menu(p)
            if clicked:
                await asyncio.sleep(2)
        except RiskControlError:
            raise
        except Exception as e:
            log.warning(f"Search menu click failed: {e}")

        try:
            return await self._acquire_search_frame(p)
        except (RiskControlError, LoginRequiredError):
            raise
        except Exception as e:
            log.warning(f"Failed to acquire search frame: {e}")
            return None

    @retry(max_attempts=3, delay=1.0, jitter=1.5)
    async def _acquire_search_frame(self, p):
        """Poll for a live search iframe (no navigation or menu clicks)."""
        risk_reason = await self._detect_risk_control(p)
        if risk_reason:
            raise RiskControlError(risk_reason)
        if await self._is_login_page(p):
            raise LoginRequiredError("当前处于登录页或登录已失效，请先完成登录")

        frame = await self._find_existing_search_frame(p, require_visible=True)
        if frame:
            return frame

        for _ in range(20):
            risk_reason = await self._detect_risk_control(p)
            if risk_reason:
                raise RiskControlError(risk_reason)

            frame = await self._find_existing_search_frame(p, require_visible=True)
            if frame:
                return frame
            await asyncio.sleep(0.5)

        url = getattr(p, "url", "") or ""
        frame_urls = [getattr(f, "url", "") for f in (getattr(p, "frames", []) or [])]
        log.warning(f"Search frame missing: page={url} frames={frame_urls}")
        raise RuntimeError("无法找到可用的搜索 iframe")

    async def search_candidates(
        self,
        keyword: str,
        city: str = "",
        experience: str = "",
        salary: str = "",
        count: int = 30,
    ) -> list[dict]:
        async with self._search_lock:
            return await self._search_candidates(
                keyword, city, experience, salary, count
            )

    async def _search_candidates(
        self,
        keyword: str,
        city: str = "",
        experience: str = "",
        salary: str = "",
        count: int = 30,
    ) -> list[dict]:
        """Search candidates via the BOSS SPA search iframe.

        Args:
            keyword: 搜索关键词
            city: 城市（可选）
            experience: 经验要求（可选）
            salary: 薪资范围（可选）
            count: 期望获取的候选人数量，默认 30，最大约 300

        Returns:
            候选人列表
        """
        limit_error = self._check_daily_search_limit()
        if limit_error:
            return [limit_error]

        p = self.browser.page
        risk_reason = await self._detect_risk_control(p)
        if risk_reason:
            return [await self._build_risk_control_response(risk_reason)]

        try:
            frame = await self._get_search_frame(p)
        except RiskControlError as e:
            return [await self._build_risk_control_response(str(e))]
        except LoginRequiredError as e:
            return [{"error": "login_required", "message": str(e)}]

        if not frame:
            return [{"error": "无法找到搜索 iframe"}]

        await self._wait_for_search_cooldown()

        # Re-validate frame after cooldown in case SPA rebuilt the iframe
        if not self._is_frame_alive(frame):
            self._search_frame = None
            try:
                frame = await self._get_search_frame(p)
            except RiskControlError as e:
                return [await self._build_risk_control_response(str(e))]
            except LoginRequiredError as e:
                return [{"error": "login_required", "message": str(e)}]
            if not frame:
                return [{"error": "无法找到搜索 iframe"}]

        try:
            search_input = await frame.query_selector("input.search-input")
        except Exception:
            self._search_frame = None
            frame = await self._find_existing_search_frame(p, require_visible=True)
            if not frame:
                return [{"error": "搜索 iframe 已失效，请重试"}]
            search_input = await frame.query_selector("input.search-input")

        if not search_input:
            return [{"error": "无法找到搜索输入框"}]

        # Set up network interception to capture API data
        api_candidates = []

        async def _capture_api(response):
            try:
                url = response.url
                if ("geeks.json" in url or "searchGeek" in url
                        or ("zpgeek" in url and "search" in url)):
                    data = await response.json()
                    if isinstance(data, dict):
                        geek_list = data.get("zpData", {}).get("geekList", [])
                        if not geek_list:
                            geek_list = data.get("data", {}).get("list", [])
                        api_candidates.extend(geek_list)
            except Exception:
                pass

        self._record_search_timestamp()
        p.on("response", _capture_api)
        try:
            await self._dismiss_overlays(p, toast_timeout=1.5)

            # Clear and type keyword
            await search_input.click(timeout=5000)
            await search_input.fill("")
            await search_input.type(keyword, delay=80)
            await asyncio.sleep(0.5)

            await search_input.press("Enter")
            await asyncio.sleep(3)

            try:
                await frame.wait_for_selector("li.geek-info-card", timeout=10000)
            except Exception:
                risk_reason = await self._detect_risk_control(p)
                if risk_reason:
                    return [await self._build_risk_control_response(risk_reason)]
                if not self._is_frame_alive(frame):
                    self._search_frame = None
                    return [{"error": "搜索 iframe 已失效（Frame was detached），请重试"}]
                return [{"error": "搜索超时，未找到候选人卡片"}]

            await self.browser.random_delay()

            # Scroll to load more candidates if count > 30
            if count > 30 and self._is_frame_alive(frame):
                loaded = await frame.evaluate('document.querySelectorAll("li.geek-info-card").length')
                max_rounds = min((count - loaded) // 14 + 2, 25)  # cap at ~375 cards
                for _ in range(max_rounds):
                    if loaded >= count or not self._is_frame_alive(frame):
                        break
                    await frame.evaluate(
                        'document.documentElement.scrollTop = document.documentElement.scrollHeight'
                    )
                    await asyncio.sleep(2)
                    if not self._is_frame_alive(frame):
                        break
                    new_loaded = await frame.evaluate(
                        'document.querySelectorAll("li.geek-info-card").length'
                    )
                    if new_loaded == loaded:
                        break  # no more to load
                    loaded = new_loaded
        finally:
            try:
                p.remove_listener("response", _capture_api)
            except Exception:
                pass

        # Extract candidate cards from DOM
        if not self._is_frame_alive(frame):
            self._search_frame = None
            frame = await self._find_existing_search_frame(p, require_visible=False)
            if not frame:
                return [{"error": "搜索 iframe 已失效（Frame was detached），请重试"}]
        candidates = await frame.evaluate(EXTRACT_CARDS_JS)

        # Enrich DOM candidates with API data if available
        if api_candidates:
            api_by_expect = {}
            for ac in api_candidates:
                eid = str(ac.get("expectId", ac.get("encryptExpectId", "")))
                if eid:
                    api_by_expect[eid] = ac
            for c in candidates:
                api = api_by_expect.get(c.get("expectId", ""))
                if api:
                    # Merge richer API fields (only if missing from DOM parse)
                    if not c.get("salary") and api.get("salaryDesc"):
                        c["salary"] = api["salaryDesc"]
                    if api.get("geekName"):
                        c["_apiName"] = api["geekName"]
                    if api.get("encryptGeekId"):
                        c["geekId"] = api["encryptGeekId"]

        return candidates

    async def view_candidate_by_index(self, index: int) -> dict:
        """Click the Nth candidate card, screenshot the resume canvas, OCR to extract text.

        Args:
            index: 0-based index of the candidate card to click

        Returns:
            Detailed resume data with OCR text
        """
        p = self.browser.page

        # Close any existing dialog FIRST
        await self._cleanup_dialogs()

        # Reuse live search frame without forcing navigation; recover if detached
        frame = await self._find_existing_search_frame(p, require_visible=False)
        if not frame:
            try:
                frame = await self._get_search_frame(p)
            except (RiskControlError, LoginRequiredError) as e:
                return {"error": str(e)}
        if not frame:
            return {"error": "搜索 iframe 不可用，请先执行搜索"}

        # Click the candidate card by index
        clicked = await frame.evaluate(f"""() => {{
            const cards = document.querySelectorAll("li.geek-info-card a[data-contact]");
            if ({index} >= cards.length) return false;
            cards[{index}].click();
            return true;
        }}""")

        if not clicked:
            return {"error": f"索引 {index} 超出范围"}

        await asyncio.sleep(3)

        # Find the resume dialog on parent page
        dialog = await p.query_selector("div.boss-dialog__body")
        if not dialog:
            dialog = await p.query_selector("div.dialog-wrap.active")
        if not dialog:
            return {"error": "未找到简历弹窗"}

        # Screenshot the dialog (captures the canvas-rendered resume)
        screenshot = await dialog.screenshot()

        # Check if resume is scrollable and capture full content
        resume_iframe_el = await p.query_selector("iframe[src*='c-resume']")
        all_screenshots = [screenshot]

        if resume_iframe_el:
            resume_frame = await resume_iframe_el.content_frame()
            if resume_frame:
                # Check canvas height vs container height
                scroll_info = await resume_frame.evaluate("""() => {
                    const canvas = document.querySelector('canvas');
                    const container = canvas?.parentElement;
                    if (!canvas || !container) return null;
                    return {
                        canvasH: canvas.height,
                        containerH: container.clientHeight,
                        styleH: parseInt(canvas.style.height) || 0
                    };
                }""")

                if scroll_info and scroll_info.get("canvasH", 0) > scroll_info.get("containerH", 0) * 2:
                    # Canvas is taller than viewport, need to scroll
                    container_h = scroll_info["containerH"]
                    canvas_h = scroll_info["styleH"] or scroll_info["canvasH"] // 2
                    scroll_steps = max(1, (canvas_h // container_h))

                    for step in range(1, min(scroll_steps + 1, 5)):
                        await resume_frame.evaluate(f"""() => {{
                            const canvas = document.querySelector('canvas');
                            if (canvas) {{
                                canvas.style.transform = 'translateY(-{step * container_h}px)';
                            }}
                        }}""")
                        await asyncio.sleep(0.5)
                        shot = await dialog.screenshot()
                        all_screenshots.append(shot)

                    # Reset scroll
                    await resume_frame.evaluate("""() => {
                        const canvas = document.querySelector('canvas');
                        if (canvas) canvas.style.transform = 'translateY(0px)';
                    }""")

        # Extract geekId and other identifiers from dialog
        ids = await p.evaluate("""() => {
            const el = document.querySelector('[data-geekid]');
            if (!el) return {};
            return {
                geekId: el.getAttribute('data-geekid') || '',
                encryptUserId: el.getAttribute('data-encryptuserid') || '',
                expectId: el.getAttribute('data-expectid') || '',
                securityId: (el.getAttribute('data-securityid') || '').slice(0, 50) + '...',
                jid: el.getAttribute('data-jid') || '',
            };
        }""")

        # Save screenshots to temp files
        paths = []
        for i, shot in enumerate(all_screenshots):
            path = os.path.join(SCREENSHOT_DIR, f"resume_{index}_p{i}.png")
            with open(path, "wb") as f:
                f.write(shot)
            paths.append(path)

        # Extract share link by clicking the forward icon on canvas
        share_url = ""
        share_card_path = ""
        try:
            share_url, share_card_path = await self._extract_share_link(p, resume_iframe_el, index)
        except Exception:
            pass

        # Close the dialog via JS
        await p.evaluate("""() => {
            document.querySelectorAll('div.dialog-wrap').forEach(d => d.remove());
            document.querySelectorAll('.boss-layer__wrapper').forEach(l => l.remove());
            document.querySelectorAll('.boss-popup__wrapper').forEach(l => l.remove());
        }""")
        await asyncio.sleep(0.5)

        return {
            "screenshots": paths,
            "ids": ids,
            "share_url": share_url,
            "share_card": share_card_path,
            "pages": len(paths),
        }

    async def _extract_share_link(self, p, resume_iframe_el, index: int) -> tuple[str, str]:
        """Click the forward icon on canvas, extract QR code, decode share URL.

        Returns:
            (share_url, share_card_path) tuple
        """
        if not resume_iframe_el:
            return "", ""

        resume_frame = await resume_iframe_el.content_frame()
        if not resume_frame:
            return "", ""

        # Dynamically calculate forward icon position based on canvas size
        base_pos = await self._find_share_icon_position(resume_frame)
        if not base_pos:
            return "", ""
        bx, by = base_pos
        # Try the calculated position + a few nearby offsets
        for fx, fy in [(bx, by), (bx+6, by), (bx-6, by), (bx, by+5), (bx+6, by+5)]:
            await resume_frame.evaluate(f"""() => {{
                const canvas = document.querySelector('canvas');
                if (!canvas) return;
                canvas.dispatchEvent(new MouseEvent('click', {{
                    clientX: {fx}, clientY: {fy}, bubbles: true
                }}));
            }}""")
            await asyncio.sleep(0.5)

            # Check if share options popup appeared (站内同事 / 转发至其他)
            has_share = await p.evaluate("""() => {
                const els = document.querySelectorAll('.c-pay-4-another, .boss-popup__wrapper');
                for (const el of els) {
                    if (el.offsetWidth > 0 && (el.innerText || '').includes('转发')) return true;
                }
                return false;
            }""")
            if has_share:
                break
        else:
            return "", ""

        await asyncio.sleep(0.5)

        # Click "转发至其他" to show QR code card
        await p.evaluate("""() => {
            const items = document.querySelectorAll('.c-pay-4-another .item, .nav-list .item');
            for (const item of items) {
                if ((item.innerText || '').includes('转发至其他')) {
                    item.click();
                    return true;
                }
            }
            return false;
        }""")
        await asyncio.sleep(2)

        # Extract the share card image (base64 PNG with QR code)
        img_data = await p.evaluate("""() => {
            const img = document.querySelector('img.share-image');
            return img ? img.src : '';
        }""")

        share_url = ""
        share_card_path = ""

        if img_data.startswith("data:image/"):
            # Save share card image
            b64 = img_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64)
            share_card_path = os.path.join(SCREENSHOT_DIR, f"share_{index}.png")
            with open(share_card_path, "wb") as f:
                f.write(img_bytes)

            # Decode QR code to get share URL
            if HAS_QR_DECODER:
                try:
                    img = Image.open(io.BytesIO(img_bytes))
                    results = decode_qr(img)
                    if results:
                        share_url = results[0].data.decode("utf-8")
                except Exception:
                    pass

        # Close the share panel (but keep resume dialog)
        await p.evaluate("""() => {
            const panels = document.querySelectorAll('.c-pay-4-another');
            panels.forEach(panel => {
                const wrapper = panel.closest('.boss-popup__wrapper');
                if (wrapper && wrapper.parentElement) {
                    wrapper.parentElement.remove();
                }
            });
        }""")
        await asyncio.sleep(0.3)

        return share_url, share_card_path

    async def greet_by_index(self, index: int, message: str = "") -> dict:
        """Click the Nth candidate in search results and send a greeting.

        After greeting, the candidate appears in the "沟通" chat list permanently.

        Args:
            index: 0-based index of the candidate card
            message: Custom greeting message (optional)

        Returns:
            Result with status and candidate identifiers
        """
        p = self.browser.page

        # Close any existing dialog
        await self._cleanup_dialogs()

        # Reuse live search frame; discard stale detached handle if needed
        frame = await self._find_existing_search_frame(p, require_visible=False)
        if not frame:
            return {"status": "error", "message": "搜索 iframe 不可用"}

        # Click the candidate card
        clicked = await frame.evaluate(f"""() => {{
            const cards = document.querySelectorAll("li.geek-info-card a[data-contact]");
            if ({index} >= cards.length) return false;
            cards[{index}].click();
            return true;
        }}""")

        if not clicked:
            return {"status": "error", "message": f"索引 {index} 超出范围"}

        await asyncio.sleep(3)

        # Extract candidate identifiers from dialog
        ids = await p.evaluate("""() => {
            const el = document.querySelector('[data-geekid]');
            if (!el) return {};
            return {
                geekId: el.getAttribute('data-geekid') || '',
                expectId: el.getAttribute('data-expectid') || '',
                jid: el.getAttribute('data-jid') || '',
            };
        }""")

        # Click "联系Ta" button
        try:
            greet_btn = await p.query_selector("button.btn-getcontact, .btn-getcontact")
            if not greet_btn:
                # Close dialog and return error
                await p.evaluate("""() => {
                    document.querySelectorAll('div.dialog-wrap').forEach(d => d.remove());
                    document.querySelectorAll('.boss-layer__wrapper').forEach(l => l.remove());
                }""")
                return {"status": "error", "message": "未找到联系按钮", "ids": ids}

            await greet_btn.click()
            await asyncio.sleep(3)

            # Check if a chat window / message input appeared
            # If custom message, try to type and send
            if message:
                chat_input = await p.query_selector("textarea, .chat-input, [contenteditable='true']")
                if chat_input:
                    await chat_input.fill("")
                    await chat_input.type(message, delay=50)
                    await asyncio.sleep(0.5)
                    send_btn = await p.query_selector("button:has-text('发送'), .btn-send")
                    if send_btn:
                        await send_btn.click()
                        await asyncio.sleep(1)

            result = {"status": "success", "message": "已发送招呼，候选人已进入沟通列表", "ids": ids}

        except Exception as e:
            result = {"status": "error", "message": str(e), "ids": ids}

        # Close dialog
        await self._cleanup_dialogs()

        return result

    async def view_candidate(self, profile_url: str) -> dict:
        """View detailed candidate resume by URL (legacy, may not work in SPA)."""
        p = self.browser.page
        await p.goto(profile_url, wait_until="domcontentloaded")
        await self.browser.random_delay()

        resume = {"profile_url": profile_url}
        try:
            body = await p.query_selector("body")
            if body:
                resume["full_text"] = (await body.inner_text())[:5000]
        except Exception as e:
            resume["error"] = str(e)
        return resume

    async def send_greeting(self, profile_url: str, message: str = "") -> dict:
        """Send a greeting message to a candidate."""
        p = self.browser.page
        await p.goto(profile_url, wait_until="domcontentloaded")
        await self.browser.random_delay()

        try:
            greet_btn = await p.query_selector(
                "button:has-text('打招呼'), button:has-text('沟通'), .btn-greet, .btn-chat"
            )
            if not greet_btn:
                return {"status": "error", "message": "未找到打招呼按钮"}

            await greet_btn.click()
            await self.browser.random_delay()

            if message:
                input_el = await p.query_selector("textarea, .chat-input, [contenteditable]")
                if input_el:
                    await input_el.fill("")
                    await input_el.type(message, delay=50)
                    await self.browser.random_delay()
                    send_btn = await p.query_selector("button:has-text('发送'), .btn-send")
                    if send_btn:
                        await send_btn.click()

            return {"status": "success", "message": "已向候选人发送消息"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
