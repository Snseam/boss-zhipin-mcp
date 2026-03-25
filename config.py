"""Configuration for boss-recruiter-mcp."""

import os

import yaml

# BOSS 直聘
BOSS_BASE_URL = "https://www.zhipin.com"
BOSS_SEARCH_URL = "https://www.zhipin.com/web/boss/recommend"

# Cookie 持久化
COOKIES_DIR = os.path.join(os.path.dirname(__file__), "cookies")
COOKIES_FILE = os.path.join(COOKIES_DIR, "boss_cookies.json")

# 去重记录
DEDUP_FILE = os.path.join(os.path.dirname(__file__), "seen_candidates.json")

# 浏览器配置
BROWSER_HEADLESS = False  # 首次登录需要 GUI
SLOW_MO = 100  # 毫秒，模拟人类操作速度
MIN_DELAY = 2.0  # 最小随机延迟（秒）
MAX_DELAY = 5.0  # 最大随机延迟（秒）
MAX_CANDIDATES_PER_SESSION = 30  # 单次会话最大抓取数

# AI 评估
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVAL_MODEL = "claude-sonnet-4-20250514"

# 搜索配置（从 search_profile.yaml 加载）
PROFILE_FILE = os.path.join(os.path.dirname(__file__), "search_profile.yaml")


def load_profile() -> dict:
    """Load search profile from YAML config file."""
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


PROFILE = load_profile()

# 从 YAML 动态生成默认 JD
_job = PROFILE.get("job", {})
_company = PROFILE.get("company", {})
_reqs = PROFILE.get("requirements", {})

_must = "\n".join("- " + r for r in _reqs.get("must_have", []))
_nice = "\n".join("- " + r for r in _reqs.get("nice_to_have", []))

DEFAULT_JD = f"""岗位：{_job.get('title', 'AI 产品经理')}
公司：{_company.get('name', '')}（{_company.get('description', '')}）
要求：
{_must}
加分项：
{_nice}
薪资：{_job.get('salary', '')}
工作地：{_job.get('city', '')}
""" if PROFILE else """岗位：AI 产品经理
要求：
- 3-5 年产品经理经验
- 有 AI/大模型/SaaS 产品经验优先
薪资：面议
"""
