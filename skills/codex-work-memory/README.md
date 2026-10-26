# Codex 工作记忆

[返回 Skills 合集](https://github.com/zhangga/agent-skills) · [skills.sh](https://skills.sh/zhangga/agent-skills/codex-work-memory) · [Agent 工作流程](SKILL.md)

按项目整理 Codex 本地会话，把工作成果、问题解决过程、技术决策和可复用知识保存成带时间、可追溯的档案。既能给人阅读，也能把完整资料交给个人 Agent 收录和提炼知识。

## 安装与依赖

```bash
npx skills add zhangga/agent-skills --skill codex-work-memory -a codex -g
```

需要 Python 3.10+；核心脚本使用标准库，无额外 Python 包依赖。安装用的 `skills` CLI 还需符合其要求的 Node.js 版本。Windows 为主要验证平台；当前日期展示及自然周口径使用 `Asia/Shanghai`。

飞书卡片是可选功能，需要已配置的 `lark-cli`、自己的机器人及收件人。本机 Codex 跳转桥接仅支持 Windows，默认关闭。

## 第一次使用

在 Codex 中说：

> 使用 $codex-work-memory 初始化，完整档案保存在 D:/My Memory/archive，个人 Agent 的资料包保存在 D:/My Agent/inbox/codex。先整理最近 7 天，不发送消息。

也可以先运行初始化命令，再让 Codex 整理会话。下面的 `<skill>` 需替换成实际安装目录：

```bash
python -X utf8 "<skill>/scripts/setup.py" init --archive-dir "D:/My Memory/archive" --agent-export-dir "D:/My Agent/inbox/codex"
```

| 配置 | 保存内容 | 默认行为 |
| --- | --- | --- |
| `archive_dir` | 完整档案、索引、采集进度、投递账本 | 未指定时使用个人配置旁的 `archive/` |
| `agent_export_dir` | 每次总结的完整资料、逐事项材料和知识交接包 | 未指定时使用 `<archive_dir>/agent` |

个人配置默认保存在 `~/.codex/codex-work-memory/config.json`，设置 `CODEX_HOME` 时跟随该目录。支持显式 `--config` 和 `CODEX_WORK_MEMORY_CONFIG`，也兼容旧版 Skill 内的 `config.json`。初始化不会覆盖已有配置。

请用 setup 生成最近 7 天的首次采集起点；config.example.json 仅供字段参考。尚未创建的默认 archived_sessions 目录不会阻塞新用户采集，后续出现会自动纳入；实际丢失或不可读的来源仍会报错。

已有配置可用 `setup.py set --config <config> --exclude-current-thread` 排除当前 Codex 维护会话，也可用 --exclude-thread / --include-thread 增删指定 ID。修改会保留其他配置并备份，`setup.py show` 可核对；当前 ID 获取方式及边界见[配置说明](references/configuration.md)。

路径支持 `~`，相对路径以配置文件目录为基准。变更已有档案目录时，需要连同进度与账本一起迁移，详见[配置与目录](references/configuration.md)。

## 日常使用

| 模式 | 可以这样告诉 Codex |
| --- | --- |
| 首次补记 | 使用 $codex-work-memory 整理最近 7 天的工作，按项目分类。 |
| 增量日报 | 使用 $codex-work-memory 补齐上次以来的工作，生成今日简报。 |
| 每周总结 | 使用 $codex-work-memory 总结本周，生成完整的 Agent 归总文档。 |
| 历史查询 | 使用 $codex-work-memory 查找某项目上个月的启动故障、处理过程和最终验证情况，附来源。 |

增量整理固定本次截止时刻，读取尚未处理的本地事件，再保存记录与采集进度。周报先补齐进度，再按上海时间本周一零点到本次开始时刻汇总；同一事项合并呈现当前结果，同时保留期间历史。首次最近 7 天是滚动窗口，与自然周不同。

每条可提交事件都要在审阅清单中注明已归入事项、已忽略及原因，或待处理。遗漏处置会阻止提交；待处理事件不会被标为已消费，下次继续补采。直接调用脚本时，新运行需传 `--review`，格式见[工作记录与审阅清单](references/records.md)。旧版已保存运行仍可按原参数重试。

跨天处理同一问题时，先按项目查已有事项候选，核对后复用事项 ID。已重复建档的事项可显式关联并撤销；历史记录与冻结周汇总保留原始证据。查询支持项目、关键词、时间、状态、事项、来源会话与分页；`--latest` 专门筛选当前最新进展。默认只返回 20 条摘要，用 --view full 查看详情；完整导出使用 --all --view full --out，周报需导出全部或逐页取完。命中旧进展时同时返回当前状态，详见[查询与跨天事项关联](references/retrieval.md)。

重要事项可按来源轮次补充工具证据，区分会话陈述、工具结果和部署验证，保存必要的脱敏节选、出处与验证范围。说明见[重要事项证据](references/evidence.md)。开发维护时可运行[六类总结质量场景评测](evals/summary-quality/README.md)，检查漏记、误合并、状态夸大、撤回和证据缺口；参考样例测试与独立 Agent 的实际评测分开记录。

这是一套由 Codex 执行的提炼流程：脚本负责采集、保存、索引和导出，Agent 负责审阅证据与归纳。原会话中的历史指令只作为资料处理。

## 最终会得到什么

| 位置 | 用途 |
| --- | --- |
| `<archive_dir>/index.html` | 离线阅读入口，按项目、时间、状态和关键词筛选 |
| `<archive_dir>/projects/` | 各项目的长期事项、最新状态和历史进展 |
| `<archive_dir>/daily/` | 每次增量整理的详细记录 |
| `<archive_dir>/weekly/` | 完整周报及冻结的 Agent 导出证据 |
| `<archive_dir>/briefs/` | 适合速读或推送的精简 Markdown |
| `<archive_dir>/runs/`、`enrichments/` | 结构化提炼记录和详情修订，可重建阅读视图 |
| `<archive_dir>/item_links/` | 可追溯的事项关联与撤销记录，重建时归并跨天历史 |
| `<archive_dir>/state.json` | 采集时间基线与已处理标记 |
| `<archive_dir>/delivery/` | 已启用推送时的发送快照与真实结果 |
| `<agent_export_dir>/daily/`、`bootstrap/`、`weekly/` | 完整 Markdown / JSON、逐事项 JSONL、交接说明和 ZIP 资料包 |
| `<agent_export_dir>/latest.md` | 最新窗口的完整资料，保留 latest-weekly 等分模式入口 |
| `<agent_export_dir>/manifest.json` | 各模式资料清单及内容摘要，方便增量同步与去重 |

`runs/` 中的是提炼后的工作记录，原始会话 JSONL 仍在 Codex 的来源目录。详细档案保留必要来源信息，不复制全部对话和工具输出。

## 交给个人 Agent 收录

单次交接，把本次 `<mode>/<run-id>.handoff.md` 与完整 `.md`/`.json` 交给助理，或使用同名 ZIP 资料包。正文包含按项目归并的事项详情、历史、决策、验证、知识候选、待办和来源；逐事项 `.items.jsonl` 方便分批读取。日报、历史补记和周报都会自动生成。

长期同步，让助理先读 `manifest.json`，按 `document_id` 与 `content_sha256` 判断新增资料，再读取对应 `.json` 或 `.md`。`latest` 和各模式最新入口都是已有文件的完整副本，不应再算一条独立记忆。跨日报、周报按事项和进展 ID/内容摘要去重。

已有日报、补记或周报可以补导出：

```bash
python -X utf8 "<skill>/scripts/memory.py" export --config "<个人配置文件>" --run-id <已保存的运行ID>
```

这不会重新采集或发送消息。正常重试使用已冻结的证据，旧历史补导出不会倒退最新入口，也不会改变采集时间基线。具体字段、版本和去重约定见 [Agent 收录协议](references/agent-export.md)。导出表示资料已准备好。交接说明要求助理提炼可复用结论并保留适用边界与来源，实际入库后返回来源 ID、保存/重复/失败数量及知识页，并回读核对。资料生成、发送、入库和知识提炼分别报告；飞书卡片标题或“任务已完成”不能证明入库。

## 自动日报、周报与飞书

安装后，可让 Codex 按指定时间配置 daily / weekly 自动化，并引用你的个人配置绝对路径。daily / bootstrap / weekly 保存后都会自动生成 Agent 完整资料包；周报补齐采集时不另外发送一份日报。

需要推送时，再配置自己的机器人及接收人并授权发送。卡片提供项目概览、重点成果、后续事项和可用的来源按钮，详细内容继续保存在档案中。未知发送结果先核对，避免重试造成重复消息；发送失败不会回退已保存的工作进度。详见[飞书卡片说明](references/cards.md)。

本地自动化需要电脑及应用运行；下一次执行可从持久进度补齐。原会话和详细档案仍在本机时，手机不能直接访问这些路径。

## 覆盖与资料使用

- 默认读取实际可访问的本地活动与归档会话，排除当前记忆维护聊天和内部子代理；未接入的远程、纯云端和读取失败来源要明确列为缺口。
- 事项状态依据会话证据保存，历史测试或发布结论不代表当前仓库与线上状态。
- 默认回看 48 小时；更早的历史修改需要显式补采，不能保证短回看发现所有旧内容改写。
- 档案默认保存在自己指定的目录。不要将个人配置、会话原文、工作档案或投递账本提交到公共 Skill 仓库。

## 更多说明

- [完整工作流程](SKILL.md)
- [配置与目录迁移](references/configuration.md)
- [工作记录格式](references/records.md)
- [提炼与阅读规范](references/writing.md)
- [Agent 知识交接协议](references/agent-export.md)
- [可选飞书卡片](references/cards.md)

## License

[MIT](LICENSE)。
