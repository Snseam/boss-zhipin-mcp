# 多平台 MCP 客户端配置指南

`boss-zhipin-mcp` 是标准的本地 MCP Server（默认 `stdio` 传输，同时支持本地 `streamable-http`），理论上可接入任何支持 MCP 协议的**桌面端 AI 客户端**（如 WorkBuddy、Claude Code、Claude Desktop、Cursor、VS Code、Codex、Cherry Studio、Trae、Windsurf、Cline 等）。

## 通用前置条件与注意事项

在配置任意客户端前，请先确认以下 6 点：

1. **仅限本机桌面端运行**：本服务依赖本机 Python / Playwright 环境以及本地 Chrome 的 CDP 调试端口（`localhost:9222`），无法作为纯云端无本地环境的远程 MCP 使用。
2. **使用虚拟环境 Python 的绝对路径**：大多数桌面客户端启动 `stdio` 子进程时不会加载 shell 环境变量（`PATH`），`command` 请填写绝对路径：
   - macOS / Linux：`/path/to/boss-zhipin-mcp/.venv/bin/python`
   - Windows：`C:\\path\\to\\boss-zhipin-mcp\\.venv\\Scripts\\python.exe`（JSON 中反斜杠需双写）
3. **系统代理绕过 `localhost`**：如果本机开启了 Clash 等代理工具，请在 `env` 中配置 `"NO_PROXY": "localhost,127.0.0.1"`，避免连接 Chrome CDP 时出现 `Unexpected status 400`。
4. **先做启动自检**：配置客户端前，建议在仓库目录先执行一次自检，确认返回 `"status": "ok"`：
   ```bash
   .venv/bin/python server.py --check
   ```
5. **长耗时工具的超时处理**：
   - `boss_multi_search`（多关键词批量搜索 + `auto_view`）与 `boss_login` 可能耗时超过 30 秒。
   - 如果你使用的客户端默认工具调用超时较短（例如 30 秒）且无法调大，建议改用单关键词 `boss_search_candidates`，或先运行 `boss_multi_search(auto_view=False)` 分批处理——结果会实时写入 `candidates_db.json`，支持断点续跑。
6. **Canvas 简历截图说明**：`boss_view_by_index` / `boss_view_by_expect_id` 返回本地 PNG 截图路径；若当前客户端或模型不支持直接读取本地图片，可结合返回的文本字段与 `share_url` 进行筛选与跟进。

## 客户端配置速查

| 客户端 | 配置文件位置 | 顶层字段 | 传输方式 |
| --- | --- | --- | --- |
| **WorkBuddy（桌面端）** | `~/.workbuddy/mcp.json` 或 `<项目>/.workbuddy/mcp.json` | `mcpServers` | `stdio`（推荐） / `streamable-http` |
| **Claude Code** | `<项目>/.mcp.json`（或 `claude mcp add`） | `mcpServers` | `stdio` |
| **Claude Desktop** | `claude_desktop_config.json` | `mcpServers` | `stdio` |
| **Cursor** | `~/.cursor/mcp.json` 或 `<项目>/.cursor/mcp.json` | `mcpServers` | `stdio` |
| **VS Code（Copilot harness）** | `~/.copilot/mcp-config.json` 或工作区根目录 `.mcp.json` | `mcpServers` | `stdio` / `http` |
| **VS Code（旧 Local harness）** | `<项目>/.vscode/mcp.json` | `servers` | `stdio` / `http`（旧格式） |
| **Codex CLI / Desktop** | `~/.codex/config.toml` | `[mcp_servers.<name>]` | `stdio` |
| **Cherry Studio / Trae / Windsurf / Cline** | 客户端内「MCP 服务器」设置 | 通常为 `mcpServers` 或表单录入 | `stdio` / `streamable-http` |

---

## 1. WorkBuddy（桌面端）

WorkBuddy 原生支持自定义本地 `stdio` MCP Server。

### 配置文件路径

- **全局生效**：
  - macOS / Linux：`~/.workbuddy/mcp.json`
  - Windows：`%USERPROFILE%\.workbuddy\mcp.json`
- **仅当前项目生效**：`<项目目录>/.workbuddy/mcp.json`
- **界面入口**：侧边栏「插件 → MCP 服务器 → 配置 MCP」（或「连接器 → 自定义连接器 → 配置 MCP」）。

> 注意文件名是 `mcp.json`，与 Claude Code 的 `.mcp.json` 不同。

### 配置示例（macOS / Linux）

```json
{
  "mcpServers": {
    "boss-recruiter": {
      "command": "/path/to/boss-zhipin-mcp/.venv/bin/python",
      "args": ["/path/to/boss-zhipin-mcp/server.py"],
      "env": {
        "NO_PROXY": "localhost,127.0.0.1"
      },
      "timeout": 300000
    }
  }
}
```

### 配置示例（Windows）

```json
{
  "mcpServers": {
    "boss-recruiter": {
      "command": "C:\\path\\to\\boss-zhipin-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\boss-zhipin-mcp\\server.py"],
      "env": {
        "NO_PROXY": "localhost,127.0.0.1"
      },
      "timeout": 300000
    }
  }
}
```

### WorkBuddy 使用提示

- 保存配置后，在 MCP / 连接器面板确认 `boss-recruiter` 状态灯变绿；首次加载如弹出信任提示，请点击 **Trust（信任）**。
- WorkBuddy 默认工具调用超时为 **30 秒（30000 ms）**，不同版本对用户级 `mcp.json` 中自定义 `timeout` 字段的支持可能存在差异。若运行 `boss_multi_search` 或 `boss_login` 触发超时，可先在本地 Chrome 手动完成 BOSS 直聘登录，并改用 `boss_search_candidates` 单关键词分批调用。

---

## 2. Claude Code

### 方式 A：项目级 `.mcp.json`

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

### 方式 B：命令行添加

```bash
claude mcp add boss-recruiter \
  -e NO_PROXY=localhost,127.0.0.1 \
  -- /path/to/boss-zhipin-mcp/.venv/bin/python /path/to/boss-zhipin-mcp/server.py
```

---

## 3. Claude Desktop

编辑 Claude Desktop 配置文件：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

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

修改保存后，完全退出并重启 Claude Desktop 生效。

---

## 4. Cursor

编辑 Cursor MCP 配置文件：

- 全局：`~/.cursor/mcp.json`
- 项目级：`<项目目录>/.cursor/mcp.json`

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

保存后可在 Cursor `Settings → Tools & MCP` 中刷新并确认工具列表已加载。

---

## 5. VS Code（GitHub Copilot）

新建的 Copilot harness 使用标准 `mcpServers` 格式。可在 VS Code 中打开 **Chat: Open Customizations → MCP Servers → Add Server**，选择 Command (stdio)，也可手动编辑：

- 用户级：`~/.copilot/mcp-config.json`（若设置了 `COPILOT_HOME`，使用 `$COPILOT_HOME/mcp-config.json`）
- 工作区级：仓库根目录 `.mcp.json`

```json
{
  "mcpServers": {
    "boss-recruiter": {
      "type": "stdio",
      "command": "/path/to/boss-zhipin-mcp/.venv/bin/python",
      "args": ["/path/to/boss-zhipin-mcp/server.py"],
      "env": {
        "NO_PROXY": "localhost,127.0.0.1"
      }
    }
  }
}
```

旧版 **Local harness** 使用 `.vscode/mcp.json` 与 `servers` 字段，属于已弃用的编辑器管理格式：

```json
{
  "servers": {
    "boss-recruiter": {
      "type": "stdio",
      "command": "/path/to/boss-zhipin-mcp/.venv/bin/python",
      "args": ["/path/to/boss-zhipin-mcp/server.py"],
      "env": {
        "NO_PROXY": "localhost,127.0.0.1"
      }
    }
  }
}
```

新配置请优先使用 Copilot harness 格式；如需继续维护旧 Local harness 配置，按该模式现有配置运行。

---

## 6. Codex CLI / Codex Desktop

在 `~/.codex/config.toml` 中添加：

```toml
[mcp_servers.boss-recruiter]
command = "/path/to/boss-zhipin-mcp/.venv/bin/python"
args = ["/path/to/boss-zhipin-mcp/server.py"]
tool_timeout_sec = 300

[mcp_servers.boss-recruiter.env]
NO_PROXY = "localhost,127.0.0.1"
```

---

## 7. 其他 MCP 客户端（Cherry Studio / Trae / Windsurf / Cline 等）

### 方式 A：`stdio` 本地启动（推荐）

如果客户端提供图形化表单，按以下字段填写：

- **名称**：`boss-recruiter`
- **类型 / 传输协议**：`stdio`
- **命令（Command）**：`/path/to/boss-zhipin-mcp/.venv/bin/python`
- **参数（Args）**：`/path/to/boss-zhipin-mcp/server.py`
- **环境变量（Env）**：`NO_PROXY=localhost,127.0.0.1`

如果客户端支持粘贴 JSON，直接使用与 Claude Desktop / Cursor 相同的 `mcpServers` 结构即可。

### 方式 B：本地 `Streamable HTTP` 模式

如果某些客户端更适合通过本地 HTTP URL 连接（例如需要复用同一个常驻 MCP 进程），可先在终端手动启动 HTTP 服务：

```bash
NO_PROXY=localhost,127.0.0.1 /path/to/boss-zhipin-mcp/.venv/bin/python /path/to/boss-zhipin-mcp/server.py --transport streamable-http --port 8090
```

然后在客户端中选择 `Streamable HTTP` 类型，填入本地端点地址：

```text
http://127.0.0.1:8090/mcp
```

---

## 排查清单

- **客户端里看不到工具 / 状态灯为红**：先运行 `.venv/bin/python server.py --check`，确认虚拟环境、依赖与配置无误，再检查 JSON/TOML 语法是否有效。
- **找不到 `python` 或模块**：确认 `command` 是 `.venv` 内 Python 的绝对路径，而非裸命令 `python` / `python3`。
- **连接 Chrome 报错 `Unexpected status 400`**：在对应客户端的 `env` 中补充 `NO_PROXY=localhost,127.0.0.1`。
- **单次调用超时**：优先在本地 Chrome 提前登录 BOSS 直聘，减少 `boss_login` 等待；搜索时改用 `boss_search_candidates` 或 `boss_multi_search(auto_view=False)` 分批执行。
