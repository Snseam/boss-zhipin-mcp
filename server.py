"""BOSS 直聘招聘者 MCP Server — 候选人筛选与评估."""

import sys
import os
import json
import asyncio
import importlib
from fastmcp import FastMCP
import browser as browser_mod
import scraper as scraper_mod
import evaluator as evaluator_mod
from browser import BossBrowser
from scraper import BossScraper
from evaluator import CandidateEvaluator
from config import DEDUP_FILE, PROFILE

mcp = FastMCP("boss-recruiter")

# Shared instances
_browser: BossBrowser | None = None
_scraper: BossScraper | None = None
_evaluator = CandidateEvaluator()


# --- Persistent dedup ---

def _load_seen_ids() -> set[str]:
    """Load seen candidate IDs from disk."""
    if os.path.exists(DEDUP_FILE):
        try:
            with open(DEDUP_FILE, "r") as f:
                return set(json.load(f).get("ids", []))
        except Exception:
            return set()
    return set()


def _save_seen_ids(ids: set[str]):
    """Persist seen candidate IDs to disk."""
    with open(DEDUP_FILE, "w") as f:
        json.dump({"ids": list(ids), "count": len(ids)}, f)


_seen_ids: set[str] = _load_seen_ids()


# --- Browser/scraper helpers ---

async def get_browser() -> BossBrowser:
    global _browser, _scraper
    if _browser is None or not _browser.is_alive:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
        _browser = BossBrowser()
        await _browser.launch()
        _scraper = None
    return _browser


async def get_scraper() -> BossScraper:
    global _scraper
    browser = await get_browser()
    if _scraper is None:
        _scraper = BossScraper(browser)
    return _scraper


# --- Tools ---

@mcp.tool()
async def boss_login() -> dict:
    """登录 BOSS 直聘招聘者账号。

    首次使用需要在弹出的浏览器中手动完成登录（扫码或短信验证）。
    登录成功后 Cookie 会自动保存，后续无需重复登录。
    """
    browser = await get_browser()
    if await browser.is_logged_in():
        return {"status": "success", "message": "已登录，Cookie 有效"}
    return await browser.login()


@mcp.tool()
async def boss_search_candidates(
    keyword: str,
    city: str = "",
    experience: str = "",
    salary: str = "",
    count: int = 30,
) -> list[dict]:
    """在 BOSS 直聘「找人才」页面搜索候选人。

    支持多次搜索自动去重：重复调用会刷新结果，只返回新候选人。

    Args:
        keyword: 搜索关键词，如 "产品经理"、"前端工程师"
        city: 城市，如 "杭州"、"北京"（可选）
        experience: 经验要求，如 "3-5年"（可选）
        salary: 期望薪资范围（可选）
        count: 期望获取数量，默认 30，传入更大值会自动滚动加载（如 100、200）

    Returns:
        候选人列表（已去重），附带 _stats 统计信息
    """
    global _seen_ids
    scraper = await get_scraper()
    all_candidates = await scraper.search_candidates(keyword, city, experience, salary, count)

    new_candidates = []
    for c in all_candidates:
        eid = c.get("expectId", "")
        if not eid or eid in _seen_ids:
            continue
        _seen_ids.add(eid)
        new_candidates.append(c)

    _save_seen_ids(_seen_ids)

    stats = {
        "_stats": True,
        "total_fetched": len(all_candidates),
        "new": len(new_candidates),
        "duplicates": len(all_candidates) - len(new_candidates),
        "cumulative_seen": len(_seen_ids),
    }
    return [stats] + new_candidates


@mcp.tool()
async def boss_multi_search(
    keywords: list[str] = None,
    city: str = "",
    experience: str = "",
    count_per_keyword: int = 50,
) -> list[dict]:
    """多关键词批量搜索候选人，自动去重。

    按顺序执行多个关键词搜索，跨关键词去重，返回所有新候选人的合并结果。
    如果不传 keywords 和 city，自动从 search_profile.yaml 配置文件读取。

    Args:
        keywords: 搜索关键词列表（可选，默认从配置文件读取）
        city: 城市，所有搜索共用（可选，默认从配置文件读取）
        experience: 经验要求，所有搜索共用（可选）
        count_per_keyword: 每个关键词期望获取数量，默认 50

    Returns:
        统一结果列表：[总stats, 分词stats..., 候选人...]
    """
    global _seen_ids

    # Defaults from profile
    if keywords is None:
        keywords = PROFILE.get("keywords", ["AI产品经理"])
    if not city:
        city = PROFILE.get("job", {}).get("city", "")

    scraper = await get_scraper()
    all_new_candidates = []
    per_keyword_stats = []
    initial_seen = len(_seen_ids)

    for i, kw in enumerate(keywords):
        raw = await scraper.search_candidates(kw, city, experience, "", count_per_keyword)

        new_for_kw = []
        for c in raw:
            eid = c.get("expectId", "")
            if not eid or eid in _seen_ids:
                continue
            _seen_ids.add(eid)
            c["_source_keyword"] = kw
            new_for_kw.append(c)

        per_keyword_stats.append({
            "_keyword_stats": True,
            "keyword": kw,
            "order": i + 1,
            "fetched": len(raw),
            "new": len(new_for_kw),
            "duplicates": len(raw) - len(new_for_kw),
        })
        all_new_candidates.extend(new_for_kw)

    _save_seen_ids(_seen_ids)

    summary = {
        "_stats": True,
        "_multi_search": True,
        "keywords_count": len(keywords),
        "total_new": len(all_new_candidates),
        "total_fetched": sum(s["fetched"] for s in per_keyword_stats),
        "total_duplicates": sum(s["duplicates"] for s in per_keyword_stats),
        "cumulative_seen": len(_seen_ids),
        "new_this_session": len(_seen_ids) - initial_seen,
    }

    return [summary] + per_keyword_stats + all_new_candidates


@mcp.tool()
async def boss_clear_dedup() -> dict:
    """清空去重记录，重新开始统计。

    当开始新一轮招聘搜索时使用，会清除所有已记录的候选人 ID。

    Returns:
        清除结果
    """
    global _seen_ids
    old_count = len(_seen_ids)
    _seen_ids = set()
    _save_seen_ids(_seen_ids)
    return {"status": "success", "cleared": old_count, "message": f"已清除 {old_count} 条去重记录"}


@mcp.tool()
async def boss_view_candidate(profile_url: str) -> dict:
    """查看候选人详细简历。

    Args:
        profile_url: 候选人的 BOSS 直聘个人页面 URL

    Returns:
        结构化简历数据
    """
    scraper = await get_scraper()
    return await scraper.view_candidate(profile_url)


@mcp.tool()
async def boss_view_by_index(index: int) -> dict:
    """点击搜索结果中第 N 个候选人，查看详细简历。

    必须先执行 boss_search_candidates 搜索，然后用返回结果中的 index 字段调用此工具。

    Args:
        index: 候选人在搜索结果中的索引（0-based，来自搜索结果的 index 字段）

    Returns:
        候选人简历截图路径
    """
    scraper = await get_scraper()
    return await scraper.view_candidate_by_index(index)


@mcp.tool()
async def boss_evaluate_candidate(
    resume: dict,
    job_requirements: str = "",
) -> dict:
    """AI 评估候选人与岗位匹配度。

    Args:
        resume: 候选人简历数据
        job_requirements: 岗位要求文本（可选，默认使用配置文件中的 JD）

    Returns:
        评估结果：score (0-100), strengths, weaknesses, recommendation, summary
    """
    return _evaluator.evaluate(resume, job_requirements)


@mcp.tool()
async def boss_batch_screen(
    keyword: str,
    city: str = "",
    count: int = 20,
    job_requirements: str = "",
) -> list[dict]:
    """批量搜索 + AI 评估，一键输出排序后的候选人清单。

    Args:
        keyword: 搜索关键词
        city: 城市（可选）
        count: 筛选数量，默认 20
        job_requirements: 岗位要求（可选）

    Returns:
        按分数从高到低排序的候选人清单
    """
    scraper = await get_scraper()

    all_candidates = []
    page = 1
    while len(all_candidates) < count:
        candidates = await scraper.search_candidates(keyword, city, page=page)
        if not candidates:
            break
        all_candidates.extend(candidates)
        page += 1

    all_candidates = all_candidates[:count]

    results = []
    for i, candidate in enumerate(all_candidates):
        if not candidate.get("profile_url"):
            continue
        resume = await scraper.view_candidate(candidate["profile_url"])
        await (await get_browser()).random_delay()
        evaluation = _evaluator.evaluate(resume, job_requirements)
        results.append({
            "rank": 0,
            "name": candidate.get("name", resume.get("name", "未知")),
            "title": candidate.get("title", ""),
            "experience": candidate.get("experience", ""),
            "score": evaluation.get("score", 0),
            "recommendation": evaluation.get("recommendation", ""),
            "summary": evaluation.get("summary", ""),
            "strengths": evaluation.get("strengths", []),
            "weaknesses": evaluation.get("weaknesses", []),
            "profile_url": candidate["profile_url"],
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    return results


@mcp.tool()
async def boss_send_greeting(
    profile_url: str,
    message: str = "",
) -> dict:
    """向候选人发送打招呼消息。

    Args:
        profile_url: 候选人的 BOSS 直聘个人页面 URL
        message: 自定义消息内容（可选）

    Returns:
        发送结果
    """
    scraper = await get_scraper()
    return await scraper.send_greeting(profile_url, message)


@mcp.tool()
async def boss_debug_page() -> dict:
    """调试工具：全面扫描当前页面 DOM 结构。

    Returns:
        url, title, 所有表单元素, iframe, 主要内容区块的完整信息
    """
    browser = await get_browser()
    p = browser.page
    url = p.url
    title = await p.title()

    structure = await p.evaluate("""() => {
        const body = document.body;
        if (!body) return {error: 'no body'};

        const formEls = document.querySelectorAll('input, textarea, select, [contenteditable="true"]');
        const forms = Array.from(formEls).slice(0, 20).map(el => ({
            tag: el.tagName, type: el.type || '', placeholder: el.placeholder || '',
            class: el.className?.toString().slice(0, 80) || '',
            id: el.id || '', name: el.name || '',
            visible: el.offsetParent !== null
        }));

        const iframes = Array.from(document.querySelectorAll('iframe')).slice(0, 5).map(el => ({
            src: el.src || '', id: el.id || '', class: el.className?.toString().slice(0, 80) || ''
        }));

        const topLevel = Array.from(body.children).slice(0, 15).map(el => ({
            tag: el.tagName, id: el.id || '',
            class: el.className?.toString().slice(0, 100) || '',
            childCount: el.children.length,
            text: el.innerText?.slice(0, 150) || '',
            rect: {w: el.offsetWidth, h: el.offsetHeight}
        }));

        const mainEl = document.querySelector('.main-wrap, .main, main, [class*="content"], [class*="wrap"]');
        let mainChildren = [];
        if (mainEl) {
            mainChildren = Array.from(mainEl.querySelectorAll('*')).filter(el =>
                el.children.length < 5 && el.innerText?.trim().length > 5 && el.offsetParent !== null
            ).slice(0, 40).map(el => ({
                tag: el.tagName,
                class: el.className?.toString().slice(0, 80) || '',
                text: el.innerText?.slice(0, 120) || ''
            }));
        }

        const sidebar = document.querySelector('.menu-list, .sidebar, nav');
        let contentHtml = '';
        for (const child of body.children) {
            if (child === sidebar || child.contains?.(sidebar)) continue;
            if (child.offsetWidth > 200 && child.offsetHeight > 200) {
                contentHtml = child.innerHTML.slice(0, 3000);
                break;
            }
        }

        return {
            forms, iframes, topLevel, mainChildren, contentHtml: contentHtml.slice(0, 3000)
        };
    }""")

    return {"url": url, "title": title, "structure": structure}


@mcp.tool()
async def boss_reload() -> dict:
    """热重载 scraper/browser/evaluator 模块代码，无需重启 server。

    修改 scraper.py / browser.py / evaluator.py 后调用此工具即可生效。
    浏览器连接会保持不变。

    Returns:
        重载结果
    """
    global _scraper, _evaluator

    try:
        importlib.reload(browser_mod)
        importlib.reload(scraper_mod)
        importlib.reload(evaluator_mod)

        if _browser is not None and _browser.is_alive:
            _scraper = scraper_mod.BossScraper(_browser)
        else:
            _scraper = None

        _evaluator = evaluator_mod.CandidateEvaluator()

        return {"status": "success", "message": "已重载 browser, scraper, evaluator 模块"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    if "--transport" in sys.argv and "http" in sys.argv:
        port_idx = sys.argv.index("--port") if "--port" in sys.argv else -1
        port = int(sys.argv[port_idx + 1]) if port_idx >= 0 else 8090
        mcp.run(transport="streamable-http", port=port)
    else:
        mcp.run()
