# 个人配置与目录

首次使用先运行 `python -X utf8 <skill>/scripts/setup.py init --archive-dir <绝对目录>`。可附 `--agent-export-dir <目录>`、`--codex-home <Codex目录>`、`--config <个人配置文件>`、`--exclude-thread <维护会话ID>`。不覆盖已有配置，不迁移或删除已有档案。`setup.py show --config <配置>` 可查看生效目录。

配置解析顺序：显式 `--config` > 环境变量 `CODEX_WORK_MEMORY_CONFIG` > 已存在的 `$CODEX_HOME/codex-work-memory/config.json`（CODEX_HOME 默认 `~/.codex`）> 旧版 `<skill>/config.json`。没有配置时初始化外部个人配置文件。配置模板见 [config.example.json](../config.example.json)；真实配置不得提交到公共仓库。

| 配置 | 用途 |
| --- | --- |
| `archive_dir` | 完整档案根目录；包含 state、runs、报告、索引和投递账本 |
| `agent_export_dir` | 供个人 Agent 收录的周汇总目录；省略时为 archive_dir 下的 agent |
| `codex_home` / `source_roots` | 本地 Codex 元数据和活动/归档会话日志 |
| `initial_since` | 首次采集起点，需明确时区；首次也可用 collect 的 --since 指定 |
| `projects` / `thread_metadata` | 项目和会话元数据，可由 Codex 工具刷新 |
| `classification_overrides` | 用户纠正的分类，优先于自动归类 |
| `excluded_thread_ids` | 排除当前维护会话等；每次运行检查是否已包含当前会话 |
| `delivery` | 可选机器人、收件人、卡片偏好和本机跳转桥接；新配置默认禁用 |

路径支持 `~`；相对路径以配置文件所在目录为基准。脚本加载后返回绝对路径，命令中含空格的路径必须加引号。当前展示与周口径仅支持上海时区，不要把 timezone 改成其他值后假定所有视图都已切换。

## 更改输出位置

仅更换 Agent 收录目录：编辑个人配置中的 `agent_export_dir`，再运行 `memory.py export --config <config> --run-id <已有周报ID>` 补导出。原目录文件保留；避免把旧、新目录同时当作新的资料重复收录。

迁移完整档案：暂停或等候正在写入的任务结束，复制**整个** archive_dir（包括 state.json、runs、enrichments、delivery 和报告），核对文件后再更改配置。不要只复制 Markdown；否则会丢失去重、采集进度和投递状态。新目录确认可读后再恢复自动化。Skill 不会自动删除旧目录。

自动化及命令始终引用同一份个人配置。个人配置迁出 Skill 目录时，同时更新自动化和可选本机跳转服务引用的配置路径。只更新程序不会重置档案。

## 可选飞书

核心档案流程无需飞书。要推送时准备已登录的 `lark-cli`，配置自己的 bot_app_id、recipient_open_id，并按用户授权设置 delivery.enabled。不要复制其他人的配置或授权语句，不把 token/app secret 写入文件。消息发送逻辑见 [cards.md](cards.md)。

本机 Codex 跳转桥接依赖 Windows 桌面 Codex 的协议处理器，默认关闭。启用桥接及登录启动项需符合用户已授权范围；不通过安装 Skill 自动启用。非 Windows 环境使用实际已有的网页链接，不能伪造可点击的本机路径。
