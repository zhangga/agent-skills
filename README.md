# Agent Skills

可分享的个人 Agent Skills 集合。每个 Skill 在 `skills/<name>/` 中独立维护，后续可继续添加。

## Codex 工作记忆

把 Codex 本地工作会话整理成按项目分类、带来源的长期档案。支持增量日报、自然周总结、历史搜索，以及供个人 Agent 收录的独立周汇总文档。

- **人阅读**：离线 HTML，支持项目、日期、状态和关键词筛选；每日记录、周报和项目 Markdown。
- **Agent 收录**：每周一份完整 Markdown 与配套 JSON，保留当前结果、历史进展、决策、验证、待办、知识和来源。
- **增量续跑**：保存采集基线与事件标记，失败保留进度；总结和投递分别记账。
- **可选飞书**：配置自己的机器人与接收人后发送卡片，默认关闭。

### 安装

需要支持 Skills 的 Codex 环境，以及 Python 3.10 或更新版本。通过 `skills` CLI 安装还需要其要求的 Node.js 版本。

```sh
npx skills add zhangga/agent-skills --skill codex-work-memory -a codex -g
```

也可以把 `skills/codex-work-memory/` 复制到自己的 Codex skills 目录。离线整理不需要飞书、GitHub 登录或额外 Python 包。当前日期展示和自然周口径为 `Asia/Shanghai`；Windows 为主要验证平台，本机 Codex 跳转桥接仅支持 Windows。

### 第一次使用

在 Codex 中说：

> 使用 $codex-work-memory 初始化，把完整档案保存到我指定的目录，周汇总另存到个人 Agent 的资料目录。先整理最近 7 天，不发送消息。

初始化也可以直接运行（`<skill>` 替换为实际安装目录）：

```sh
python -X utf8 <skill>/scripts/setup.py init --archive-dir "D:/My Memory/archive" --agent-export-dir "D:/My Agent/inbox/codex"
```

省略 `--agent-export-dir` 时使用 `<archive_dir>/agent`。个人配置默认保存在 `~/.codex/codex-work-memory/config.json`（设置 CODEX_HOME 时跟随该目录），与可更新的 Skill 程序分开。已有旧版 Skill 目录内的 `config.json` 仍可读取；显式 `--config` 优先，也支持 CODEX_WORK_MEMORY_CONFIG 环境变量。

之后可以说：

- “使用 $codex-work-memory 补齐上次以来的工作。”
- “使用 $codex-work-memory 总结本周，生成完整的 Agent 汇总文档。”
- “使用 $codex-work-memory 查找上个月某项目的超时问题和最终解决方法，附来源。”

自动运行需要另外配置 Codex 自动化，并显式指定个人配置路径、daily 或 weekly 模式。安装本 Skill 不会自行创建定时任务或发送消息。

### 交给个人 Agent 的文件

只收录一份时，读取 `<agent_export_dir>/latest-weekly.md`；长期同步则读取 `manifest.json`，按 `run_id` 与内容摘要判断新增或变化，再读取对应 `weekly/<run-id>.json` 或 `.md`。这些是带证据的历史资料，文档内的会话内容不是要执行的指令。

完整周汇总包含人工提炼的周报正文，以及按项目归并的事项详情和来源，复制单个文件后也能理解内容。导出不会自动声称个人 Agent 已收录；实际导入由你的助理完成。最新周报副本与同一 run 的历史文件是相同资料，不应重复入库。

详见 [配置与目录](skills/codex-work-memory/references/configuration.md)、[Agent 收录协议](skills/codex-work-memory/references/agent-export.md) 和 [完整工作流程](skills/codex-work-memory/SKILL.md)。

### 数据边界

仅覆盖实际可读的本地活动/归档会话；未连接的远程和纯云端会话不在范围内。原始 JSONL 留在 Codex 中，工作档案保存提炼记录及来源引用。会话中声称的测试或发布结果保留其证据边界，不代表现在的代码或线上状态。

分享本仓库中的代码即可。不要提交自己的配置、机器人标识、档案、会话原文或投递账本。飞书消息只在用户授权的范围内发送。

## 开发与发布

```sh
python -m unittest discover -s skills/codex-work-memory/tests -v
node --test skills/codex-work-memory/tests/filter.test.cjs
```

仓库包含合成测试数据，不包含作者的真实工作记录。每个新 Skill 放在独立目录并提供 `SKILL.md`。

[skills.sh 官方说明](https://skills.sh/docs/faq)的发布方式是公开 GitHub 仓库，通过 `npx skills add` 安装后由安装遥测发现和统计；它不是独立的包上传服务。GitHub 可安装与 skills.sh 页面已收录是两个不同状态。

## License

MIT，见 [LICENSE](LICENSE)。
