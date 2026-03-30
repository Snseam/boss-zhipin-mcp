# BOSS 直聘 MCP Server

> 用 AI 自动化 BOSS 直聘招聘流程 — 批量搜索、自动获取链接、智能筛选评分、一键导出报告

[![MCP](https://img.shields.io/badge/MCP-Compatible-blue)](https://modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/Python-3.12+-green)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**BOSS 直聘 (zhipin.com)** 招聘者端自动化工具，基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io)，让 Claude Code / Claude Desktop 等 AI 助手直接操作 BOSS 直聘招聘后台。

## 功能特性

- **候选人搜索** — 在 BOSS 直聘「找人才」页面搜索，支持滚动加载 100+ 候选人
- **多关键词批量搜索** — 12+ 关键词自动轮询 + 跨关键词去重 + **每个关键词搜索后自动获取分享链接**
- **候选人数据库** — 搜索结果自动持久化到本地 JSON 数据库，中断可恢复，数据不丢失
- **简历查看** — 点击候选人卡片，自动截图 Canvas 渲染的简历（绕过防爬）
- **分享链接提取** — 自动点击转发按钮，解码 QR 码获取 `zpurl.cn` 永久链接
- **一键打招呼** — 在搜索结果中直接对候选人发起沟通，进入 BOSS 聊天列表
- **自动筛选评分** — 按 YAML 配置的条件（年龄/薪资/状态）过滤 + 多维度评分
- **报告导出** — 一键生成 Markdown 候选人报告（表格 + 跟进表 + 详情卡片）
- **浏览器自动启动** — 未检测到 Chrome debug 端口时，自动启动系统 Chrome
- **YAML 配置** — 公司信息、JD、搜索关键词、筛选条件全部可配置

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

## 完整招聘流水线

```
boss_multi_search(auto_view=True)
│
│  ┌─── 关键词循环 (12次) ────────────────────────┐
│  │  1. 搜索关键词 → 页面加载候选人卡片           │
│  │  2. 去重 + 存入数据库                         │
│  │  3. 全量 view 所有新候选人 → 获取 share_url   │
│  │  4. 切换下一个关键词                          │
│  └──────────────────────────────────────────────┘
│  结果: 所有候选人有 share_url + 完整数据在 DB
▼
boss_filter_and_score(top_n=15)     ← 自动筛选+评分
▼
boss_export_report(top_n=15)        ← 生成 Markdown 报告
▼
boss_greet_by_index() / boss_send_greeting()  ← 联系候选人
```

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

filter:
  max_age: 35
  max_salary_k: 40
  exclude_status: ["暂不考虑"]

scoring:
  domain_keywords: ["教育", "学术", "科研"]
  tech_keywords: ["大模型", "llm", "rag", "agent"]
```

### 3. 启动 Chrome（可选，MCP 会自动启动）

```bash
# macOS — MCP 未检测到 debug 端口时会自动启动系统 Chrome
# 如需手动启动：
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222
```

在浏览器中打开 [zhipin.com](https://www.zhipin.com) 并登录你的招聘者账号。

### 4. 连接 Claude Code

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "boss-recruiter": {
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
你: 帮我搜索北京的 AI 产品经理

Claude: boss_multi_search(auto_view=True)
  → 12 关键词搜索，537 新候选人，全部获取 share_url
  → boss_filter_and_score(top_n=15)
  → boss_export_report(top_n=15)
  → 生成候选人报告文档
```

## MCP Tools

### 搜索与查看

| Tool                     | 说明                                                               |
| ------------------------ | ------------------------------------------------------------------ |
| `boss_login`             | 登录 BOSS 直聘（检查 Cookie，失效则等待手动登录）                  |
| `boss_search_candidates` | 单关键词搜索候选人（支持滚动加载、自动去重、自动存入数据库）       |
| `boss_multi_search`      | **多关键词批量搜索**（自动去重 + 自动 view 获取链接，`auto_view`） |
| `boss_view_by_index`     | 点击候选人查看简历（截图 + 提取分享链接 + 自动存入数据库）         |
| `boss_view_by_expect_id` | 通过 expectId 在当前页面查找并查看候选人                           |
| `boss_greet_by_index`    | 在搜索结果中直接打招呼（候选人进入沟通列表）                       |
| `boss_send_greeting`     | 向候选人发送打招呼消息（通过 URL）                                 |

### 数据库与筛选

| Tool                    | 说明                                                              |
| ----------------------- | ----------------------------------------------------------------- |
| `boss_query_db`         | 查询候选人数据库（按状态/链接/关键词/日期筛选，无需浏览器）       |
| `boss_update_candidate` | 更新候选人状态/评分/备注                                          |
| `boss_filter_and_score` | **自动筛选+评分**（按 YAML 条件过滤，多维度评分，Top N 标记入库） |
| `boss_export_report`    | **导出 Markdown 报告**（表格 + 跟进表 + 详情卡片）                |
| `boss_pipeline_status`  | 查看招聘流水线进度（统计 + 恢复建议）                             |
| `boss_clear_dedup`      | **选择性**清除去重记录（按 ID/状态/日期，或全量清除）             |

### 工具

| Tool                      | 说明                                               |
| ------------------------- | -------------------------------------------------- |
| `boss_evaluate_candidate` | 简易关键词匹配评估（建议直接在 Claude 对话中评估） |
| `boss_reload`             | 热重载代码，修改后无需重启 server                  |
| `boss_debug_page`         | 调试工具，扫描当前页面 DOM 结构                    |

## 候选人数据库

搜索结果自动持久化到 `candidates_db.json`，不再丢失数据：

```json
{
  "candidates": {
    "<expectId>": {
      "name": "张**",
      "age": "27岁",
      "salary": "20-25K",
      "company": "某公司",
      "school": "某大学",
      "fullText": "...",
      "share_url": "https://zpurl.cn/...",
      "status": "shortlisted",
      "score": 95,
      "first_seen": "2026-03-30"
    }
  }
}
```

**状态流转**: `new` → `shortlisted`（评分后）→ `viewed`（获取链接后）→ `greeted`（打招呼后）

**中断恢复**: 任意步骤中断后，`boss_pipeline_status()` 显示进度和恢复建议。

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
  bonus_keywords: ["0-1", "从0到1"]
```

## 技术细节

- **浏览器连接**：CDP → 已登录 Chrome。未检测到 debug 端口时自动启动系统 Chrome
- **SPA 适配**：搜索页在 iframe 中渲染，自动导航到招聘者后台
- **Canvas 简历**：截图 + QR 码解码获取永久链接
- **候选人数据库**：JSON 文件，原子写入，基于 `expectId` 去重
- **反检测**：随机延迟（2-5s）模拟人类操作节奏

## 项目结构

```
boss-zhipin-mcp/
├── server.py                  # MCP Server 入口，注册所有 tools
├── scraper.py                 # BOSS 直聘页面抓取（SPA + iframe）
├── browser.py                 # Playwright CDP 浏览器管理 + 自动启动 Chrome
├── candidate_db.py            # 候选人数据库（持久化 + 去重 + 查询）
├── evaluator.py               # 候选人评估（关键词匹配）
├── config.py                  # 配置加载
├── search_profile.example.yaml # 搜索配置示例
├── candidates_db.json         # 候选人数据库文件（自动生成）
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
2. 确认 Chrome 启动时带了 `--remote-debugging-port=9222`（或让 MCP 自动启动）
3. 检查 `curl http://localhost:9222/json/version` 是否有响应

### 简历内容为空

BOSS 直聘用 Canvas 渲染简历，无法直接提取文字。使用 `boss_view_by_index` 截图后，让 Claude 直接看图识别。

## License

MIT
