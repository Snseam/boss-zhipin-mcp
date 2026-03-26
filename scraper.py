"""BOSS 直聘 page scraper — works with SPA iframe-based search UI."""

import asyncio
import base64
import functools
import logging
import os
import tempfile
from browser import BossBrowser
from config import BOSS_BASE_URL

try:
    from PIL import Image
    from pyzbar.pyzbar import decode as decode_qr
    import io
    HAS_QR_DECODER = True
except ImportError:
    HAS_QR_DECODER = False

log = logging.getLogger("boss-scraper")

SCREENSHOT_DIR = os.path.join(tempfile.gettempdir(), "boss-recruiter-screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


# --- Retry decorator for resilient operations ---

def retry(max_attempts=3, delay=1.0):
    """Retry async methods with automatic dialog cleanup between attempts."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except Exception as e:
                    last_error = e
                    log.warning(f"{func.__name__} attempt {attempt+1} failed: {e}")
                    if attempt < max_attempts - 1:
                        await self._cleanup_dialogs()
                        await asyncio.sleep(delay)
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

    def __init__(self, browser: BossBrowser):
        self.browser = browser
        self._search_frame = None  # cache the search iframe

    async def _cleanup_dialogs(self):
        """Clean up all dialog overlays that might block interactions."""
        p = self.browser.page
        await p.evaluate("""() => {
            document.querySelectorAll('div.dialog-wrap').forEach(d => d.remove());
            document.querySelectorAll('.boss-layer__wrapper').forEach(l => l.remove());
            document.querySelectorAll('.boss-popup__wrapper').forEach(l => l.remove());
            document.querySelectorAll('.c-pay-4-another').forEach(el => {
                const wrapper = el.closest('.boss-popup__wrapper');
                if (wrapper && wrapper.parentElement) wrapper.parentElement.remove();
            });
        }""")
        await asyncio.sleep(0.5)

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

    @retry(max_attempts=3, delay=1.0)
    async def _get_search_frame(self, p):
        """Navigate to search page and return the search iframe's Frame object."""
        search_menu = await p.query_selector("dl.menu-geeksearch")
        if search_menu:
            await search_menu.click()
            await asyncio.sleep(2)

        iframe_el = await p.query_selector("#searchContent iframe")
        if not iframe_el:
            return None
        self._search_frame = await iframe_el.content_frame()
        return self._search_frame

    async def search_candidates(
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
        p = self.browser.page
        frame = await self._get_search_frame(p)
        if not frame:
            return [{"error": "无法找到搜索 iframe"}]

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

        p.on("response", _capture_api)

        # Clear and type keyword
        await search_input.click()
        await search_input.fill("")
        await search_input.type(keyword, delay=80)
        await asyncio.sleep(0.5)

        await search_input.press("Enter")
        await asyncio.sleep(3)

        try:
            await frame.wait_for_selector("li.geek-info-card", timeout=10000)
        except Exception:
            p.remove_listener("response", _capture_api)
            return [{"error": "搜索超时，未找到候选人卡片"}]

        await self.browser.random_delay()

        # Scroll to load more candidates if count > 30
        if count > 30:
            loaded = await frame.evaluate('document.querySelectorAll("li.geek-info-card").length')
            max_rounds = min((count - loaded) // 14 + 2, 25)  # cap at ~375 cards
            for _ in range(max_rounds):
                if loaded >= count:
                    break
                await frame.evaluate(
                    'document.documentElement.scrollTop = document.documentElement.scrollHeight'
                )
                await asyncio.sleep(2)
                new_loaded = await frame.evaluate(
                    'document.querySelectorAll("li.geek-info-card").length'
                )
                if new_loaded == loaded:
                    break  # no more to load
                loaded = new_loaded

        # Stop network interception
        p.remove_listener("response", _capture_api)

        # Extract candidate cards from DOM
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

        # Ensure we have the search frame (with retry)
        if not self._search_frame:
            await self._get_search_frame(p)
        frame = self._search_frame
        if not frame:
            iframe_el = await p.query_selector("#searchContent iframe")
            if iframe_el:
                self._search_frame = await iframe_el.content_frame()
                frame = self._search_frame
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

        # Get search frame
        if not self._search_frame:
            iframe_el = await p.query_selector("#searchContent iframe")
            if iframe_el:
                self._search_frame = await iframe_el.content_frame()
        frame = self._search_frame
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
