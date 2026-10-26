# 个人配置与目录

首次使用先运行 `python -X utf8 <skill>/scripts/setup.py init --archive-dir <绝对目录>`。可附 `--agent-export-dir <目录>`、`--codex-home <Codex目录>`、`--config <个人配置文件>`、`--exclude-thread <维护会话ID>`。不覆盖已有配置，不迁移或删除已有档案。`setup.py show --config <配置>` 可查看生效目录。

配置解析顺序：显式 `--config` > 环境变量 `CODEX_WORK_MEMORY_CONFIG` > 已存在的 `$CODEX_HOME/codex-work-memory/config.json`（CODEX_HOME 默认 `~/.codex`）> 旧版 `<skill>/config.json`。没有配置时初始化外部个人配置文件。[config.example.json](../config.example.json) **仅供字段参考，请用 setup.py init 生成可用配置**：它会写入初始化时刻前 7 天的 initial_since。示例中的 null 不是有效采集起点；手动复制必须填写带时区的起点，或首次 collect 传 --since。真实配置不得提交到公共仓库。

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

## 来源目录与首次采集

默认仍保留 sessions 和 archived_sessions 两个来源路径，每次都重新检查。`<codex_home>/archived_sessions` 从未被扫描到、且元数据中没有应存在而丢失的会话日志时，目录尚未创建不会阻止完整基线推进；coverage.absent_optional_source_roots 会说明此情况。以后目录出现即可自动采集，不用重新初始化。

活动目录、自定义来源目录缺失，目录变成文件、没有权限、枚举失败，或元数据指向的近期日志丢失，仍算覆盖缺口。曾经存在的归档目录会记录在 state.source_roots_seen 中，后来消失也会报错。旧版已有成功基线但尚未记录目录历史时先保守处理；rebuild 可从旧运行恢复目录历史。不要为了消除错误而跳过所有缺失路径。

## 增删排除会话

已有配置使用 set，无需再次 init。命令只更新 excluded_thread_ids，保留其他配置、已有档案及进度；每次实际修改在原配置旁保存 `.bak-<时间>-<标识>` 备份并原子替换，重复添加不会重复写入。它不回溯删除已保存的事项或修改已经提交的审阅状态。

```bash
python -X utf8 "<skill>/scripts/setup.py" set --config "<config>" --exclude-current-thread
python -X utf8 "<skill>/scripts/setup.py" set --config "<config>" --exclude-thread "<thread-id>"
python -X utf8 "<skill>/scripts/setup.py" set --config "<config>" --include-thread "<thread-id>"
python -X utf8 "<skill>/scripts/setup.py" show --config "<config>"
```

`--exclude-thread` / `--include-thread` 可重复，分别追加排除和移除排除。同一个 ID 不能在一次调用中同时添加和移除。set 不接受 archive-dir 等迁移参数。

在当前 Codex 聊天执行的命令可通过 `CODEX_THREAD_ID` 获取当前会话 ID；`--exclude-current-thread` 使用该变量，show 同时显示 current_thread_id 与生效的 excluded_thread_ids。普通终端或其他宿主未提供变量时，show 返回 null，自动排除会明确失败。此时从宿主提供的当前聊天标识/链接取得 ID，或用 list_threads 核对标题和项目后显式传 --exclude-thread；不能把最新会话或 CODEX_SESSION_ID 猜作当前 thread ID。

## 更改输出位置

仅更换 Agent 收录目录：编辑个人配置中的 `agent_export_dir`，再运行 `memory.py export --config <config> --run-id <已有周报ID>` 补导出。原目录文件保留；避免把旧、新目录同时当作新的资料重复收录。

迁移完整档案：暂停或等候正在写入的任务结束，复制**整个** archive_dir（包括 state.json、runs、enrichments、item_links、delivery 和报告），核对文件后再更改配置。不要只复制 Markdown；否则会丢失事项关联、去重、采集进度和投递状态。新目录确认可读后再恢复自动化。Skill 不会自动删除旧目录。

自动化及命令始终引用同一份个人配置。个人配置迁出 Skill 目录时，同时更新自动化和可选本机跳转服务引用的配置路径。只更新程序不会重置档案。

## 可选飞书

核心档案流程无需飞书。要推送时准备已登录的 `lark-cli`，配置自己的 bot_app_id、recipient_open_id，并按用户授权设置 delivery.enabled。不要复制其他人的配置或授权语句，不把 token/app secret 写入文件。消息发送逻辑见 [cards.md](cards.md)。

本机 Codex 跳转桥接依赖 Windows 桌面 Codex 的协议处理器，默认关闭。启用桥接及登录启动项需符合用户已授权范围；不通过安装 Skill 自动启用。非 Windows 环境使用实际已有的网页链接，不能伪造可点击的本机路径。
