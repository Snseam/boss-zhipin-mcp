# boss-recruiter-mcp

BOSS 直聘招聘者 MCP Server — 候选人搜索、简历查看、AI 评估、批量筛选。

## 功能

| Tool                      | 说明                                                  |
| ------------------------- | ----------------------------------------------------- |
| `boss_login`              | 登录 BOSS 直聘（首次需手动扫码，之后自动复用 Cookie） |
| `boss_search_candidates`  | 在「找人才」页面搜索候选人                            |
| `boss_view_candidate`     | 查看候选人详细简历                                    |
| `boss_evaluate_candidate` | AI 评估候选人与岗位匹配度（0-100 分）                 |
| `boss_batch_screen`       | 批量搜索 + 评估，一键输出排序清单                     |
| `boss_send_greeting`      | 向候选人发送打招呼消息                                |

## 安装

```bash
cd ~/boss-recruiter-mcp
pip install -r requirements.txt
playwright install chromium
```

## 配置

设置环境变量：

```bash
export ANTHROPIC_API_KEY="sk-ant-..."  # AI 评估用
```

## 使用

### Claude Code（stdio 模式）

在 `~/.claude/settings.json` 中添加：

```json
{
  "mcpServers": {
    "boss-recruiter": {
      "command": "python",
      "args": ["server.py"],
      "cwd": "/Users/jiansheng/boss-recruiter-mcp"
    }
  }
}
```

### Agent / HTTP 模式

```bash
python server.py --transport http --port 8090
```

## 示例

```
用户: 帮我在 BOSS 直聘搜索杭州的产品经理，3-5年经验，筛选 20 个

Claude:
  #1 张三 (85分) — 4年 AI 产品经验，匹配度高
  #2 李四 (78分) — 3年 B端产品，缺 AI 经验
  ...
```
