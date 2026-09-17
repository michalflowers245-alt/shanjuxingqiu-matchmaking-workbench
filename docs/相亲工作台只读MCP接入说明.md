# 相亲工作台：本机只读 MCP 接入

工作台新增了一个独立的本机 MCP Server：

```text
scripts/mcp/matchmaking_readonly_mcp.py
```

它让支持 MCP 的客户端查询当前工作台的真实业务数据，同时把权限限制在固定的只读查询工具内。

当前相亲工作台的生产数据源是本机 `.workbench/copy_workbench.sqlite3`，因此本实现先提供“本机真实数据只读访问”。它没有假装连接不存在的远程服务器；如果以后要接入远程业务库，应单独创建只读数据库账号、字段白名单和网络访问策略，再把数据库路径通过 `WORKBENCH_MCP_DB_PATH` 指向受控副本。

## 能查询什么

- 工作区和品牌资料摘要
- 文案任务、草稿、审稿结果和研究来源
- 内容雷达已经保存的热点、竞品、平台、链接和可见互动数据
- 已保存的小红书正文提取状态（需要 `include_body=true` 才返回正文）
- 已严格归档的小红书原始正文、图片顺序、字节大小、哈希和完整性状态（需要 `include_body=true` 才返回正文）
- 发布后的阅读、互动、咨询、成交和收入指标
- 生活案例的标题、时间和图片数量（需要 `include_content=true` 才返回案例正文）
- 资料库全文片段和对应来源

工具列表固定为：

```text
list_workspaces
get_workspace_context
list_content_tasks
get_task_result
search_knowledge
list_radar_items
list_social_extractions
list_verified_xhs_archives
list_performance
list_life_cases
```

`list_social_extractions` 用于查看旧页面提取和 OCR/字幕辅助证据；需要做事实分析时，优先调用 `list_verified_xhs_archives`。后者只返回 V2 归档表中已经保存的正文与媒体证据，并明确区分 `COMPLETE`、`PARTIAL` 和验证状态，不返回图片文件路径、浏览器登录态或带签名地址。

不提供任意 SQL、写入、删除、发布、登录、上传或账号修改工具。

## 安全边界

- 数据库以 SQLite `mode=ro` 打开，并设置 `PRAGMA query_only=ON`。
- 所有查询都使用参数化 SQL，限制返回数量（最多 100 条）。
- 常见密钥、Token、Cookie、密码、授权字段会递归脱敏。
- 不读取或输出工作台密钥文件，不读取浏览器 Cookie。
- 审计日志只记录工具名、查询哈希、耗时、行数和成功/失败状态，不记录查询正文或返回内容。
- 审计日志位置：`.workbench/mcp_audit.log`。
- 默认仅监听本地标准输入输出，不开放局域网端口，也不影响工作台 API 和任务队列。

## 启动测试

在终端运行：

```bash
cd "/Users/mac/Applications/AI文案工作台"
.venv/bin/python scripts/mcp/matchmaking_readonly_mcp.py
```

然后按 MCP 客户端的标准输入输出方式发送 `initialize`、`tools/list` 和 `tools/call` 请求。工作台启动器不会自动拉起该服务，避免后台增加常驻进程；只有需要外部 MCP 客户端读取数据时再启动。

## MCP 客户端配置示例

支持通过本地命令配置 MCP Server 的客户端，可以使用以下配置。请把路径保持为本机实际路径：

```json
{
  "mcpServers": {
    "相亲工作台只读数据": {
      "command": "/Users/mac/Applications/AI文案工作台/.venv/bin/python",
      "args": [
        "/Users/mac/Applications/AI文案工作台/scripts/mcp/matchmaking_readonly_mcp.py"
      ],
      "env": {
        "WORKBENCH_MCP_DB_PATH": "/Users/mac/Applications/AI文案工作台/.workbench/copy_workbench.sqlite3"
      }
    }
  }
}
```

如果接收方电脑路径不同，只需要修改 `command`、脚本路径和 `WORKBENCH_MCP_DB_PATH`；不需要复制或填写任何 API Key。

## 工作区 ID

先调用 `list_workspaces`，再把返回的 `id` 传给其他工具。当前本机默认工作区由工作台首次启动时自动创建。若返回“品牌工作区不存在”，说明 MCP 客户端传入了另一台电脑或旧数据库里的工作区 ID，应重新调用 `list_workspaces` 获取当前 ID。
