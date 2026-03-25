# BOSS 直聘 MCP Server

> 用 AI 自动化 BOSS 直聘招聘流程 — 批量搜索候选人、查看简历、智能评估、一键筛选

[![MCP](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/Python-3.12+-green)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**BOSS 直聘 (zhipin.com)** 招聘者端自动化工具，基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io)，让 Claude Code / Claude Desktop 等 AI 助手直接操作 BOSS 直聘招聘后台。

## 功能特性

- **候选人搜索** — 在 BOSS 直聘「找人才」页面搜索，支持滚动加载 100+ 候选人
- **多关键词批量搜索** — 一次配置 12 个关键词，自动轮询 + 跨关键词去重
- **简历查看** — 点击候选人卡片，自动截图 Canvas 渲染的简历（绕过防爬）
- **AI 智能评估** — 基于岗位 JD 对候选人进行 0-100 分匹配度评估
- **持久化去重** — 多次搜索自动跳过已看过的候选人，支持跨会话
- **YAML 配置** — 公司信息、JD、搜索关键词全部可配置，代码与业务分离

## 工作原理

```
Claude Code / Claude Desktop
    ↓ MCP (stdio)
boss-zhipin-mcp (FastMCP Server)
    ↓ CDP (Chrome DevTools Protocol)
Chrome 浏览器 (已登录 BOSS 直聘)
    ↓
BOSS 直聘招聘者后台 (zhipin.com)
```

通过 Playwright 连接你已登录的 Chrome 浏览器，在 BOSS 直聘的 SPA 页面内操作搜索、查看、发消息等功能。

## 快速开始

### 1. 安装

```bash
git clone https://github.com/Snseam/boss-zhipin-mcp.git
cd boss-zhipin-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置搜索条件

```bash
cp search_profile.example.yaml search_profile.yaml
```

编辑 `search_profile.yaml`，填入你的岗位信息和搜索关键词：

```yaml
job:
  title: "AI 产品经理"
  salary: "20-30K"
  city: "北京"

keywords:
  - "AI产品经理"
  - "AIGC产品经理"
  - "大模型 产品"
  # ... 更多关键词
```

### 3. 启动 Chrome（带调试端口）

```bash
# macOS - Playwright 内置 Chromium
"/path/to/Google Chrome for Testing" --remote-debugging-port=9222 --user-data-dir="./chrome-profile"

# 或使用系统 Chrome
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="./chrome-profile"
```

在浏览器中打开 [zhipin.com](https://www.zhipin.com) 并登录你的招聘者账号。

### 4. 连接 Claude Code

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "boss-zhipin": {
      "command": "/path/to/boss-zhipin-mcp/.venv/bin/python",
      "args": ["/path/to/boss-zhipin-mcp/server.py"],
      "env": {
        "NO_PROXY": "localhost,127.0.0.1"
      }
    }
  }
}
```

### 5. 开始使用

```
你: 帮我搜索北京的 AI 产品经理，100 个

Claude: 搜到 100 人，筛选后 65 人符合条件...
  #1 张** (博雅数智) 32岁 7年 硕士 20-30K — AI教育科研场景
  #2 王** (海纳AI) 32岁 6年 硕士 20-25K — RAG产品经验
  ...
```

## MCP Tools

| Tool                      | 说明                                                     |
| ------------------------- | -------------------------------------------------------- |
| `boss_login`              | 登录 BOSS 直聘（检查 Cookie 有效性，失效则等待手动登录） |
| `boss_search_candidates`  | 单关键词搜索候选人（支持滚动加载、自动去重）             |
| `boss_multi_search`       | **多关键词批量搜索**（从 YAML 配置读取，跨关键词去重）   |
| `boss_view_by_index`      | 点击候选人查看简历（截图保存，配合 Claude Vision 识别）  |
| `boss_evaluate_candidate` | AI 评估候选人匹配度（0-100 分）                          |
| `boss_send_greeting`      | 向候选人发送打招呼消息                                   |
| `boss_clear_dedup`        | 清空去重记录，开始新一轮搜索                             |
| `boss_reload`             | 热重载代码，修改后无需重启 server                        |
| `boss_debug_page`         | 调试工具，扫描当前页面 DOM 结构                          |

## 搜索配置

`search_profile.yaml` 支持完整的搜索策略配置：

```yaml
job:
  title: "岗位名称"
  salary: "薪资范围"
  city: "城市"
  experience: "经验要求"

company:
  name: "公司名"
  description: "公司简介"

requirements:
  must_have: [...] # 硬性要求
  nice_to_have: [...] # 加分项

filter:
  max_age: 35 # 年龄上限
  max_salary_k: 40 # 薪资上限（K）
  exclude_status: ["暂不考虑"]

keywords: # 搜索关键词列表
  - "关键词1"
  - "关键词2"

scoring: # 评分关键词
  domain_keywords: ["教育", "学术"]
  tech_keywords: ["大模型", "llm", "rag"]
```

## 技术细节

- **浏览器连接**：通过 CDP (Chrome DevTools Protocol) 连接已登录的 Chrome，不需要在代码中处理登录
- **SPA 适配**：BOSS 直聘是 SPA 应用，搜索页在 iframe 中渲染。通过菜单点击导航 + iframe 内操作
- **Canvas 简历**：BOSS 直聘用 Canvas 渲染简历防止爬取，本工具通过 Playwright 截图 + Claude Vision OCR 提取文字
- **去重机制**：基于候选人 `expectId` 去重，持久化到 `seen_candidates.json`
- **反检测**：随机延迟（2-5s）模拟人类操作节奏

## 项目结构

```
boss-zhipin-mcp/
├── server.py                  # MCP Server 入口，注册所有 tools
├── scraper.py                 # BOSS 直聘页面抓取（SPA + iframe）
├── browser.py                 # Playwright CDP 浏览器管理
├── evaluator.py               # Claude API 候选人评估
├── ocr.py                     # 简历截图 OCR
├── config.py                  # 配置加载
├── search_profile.example.yaml # 搜索配置示例
├── requirements.txt
└── README.md
```

## 常见问题

### 连接失败 `Unexpected status 400`

系统有代理（如 Clash），localhost 请求被代理拦截。在 MCP 配置中添加：

```json
"env": { "NO_PROXY": "localhost,127.0.0.1" }
```

### 搜索返回空结果

1. 确认 Chrome 已登录 BOSS 直聘招聘者账号
2. 确认 Chrome 启动时带了 `--remote-debugging-port=9222`
3. 检查 `curl http://localhost:9222/json/version` 是否有响应

### 简历内容为空

BOSS 直聘用 Canvas 渲染简历，无法直接提取文字。使用 `boss_view_by_index` 截图后，让 Claude 直接看图识别。

## License

MIT
