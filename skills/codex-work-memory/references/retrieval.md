# 查询与跨天事项关联

脚本路径相对 Skill 目录，执行时替换为实际路径。查询与候选匹配不修改档案；`--out` 可将结果 JSON 写入单独的工作文件。回答仍需保留发生日期、原会话来源与验证边界。

## 查历史与查当前状态

```bash
python -X utf8 <skill>/scripts/memory.py query --config <config> --project <project-id> --keyword "超时 连接" --since 2026-10-01T00:00:00+08:00 --until 2026-11-01T00:00:00+08:00 --limit 20
```

- `--project` 接受精确项目 ID 或完整项目名，可重复。项目名重名时要求改用 ID；未匹配项目返回空结果。
- `--keyword` 的空白分隔词按 AND 匹配，不区分大小写和全半角。它按字面搜索正文、详情、文件路径、标签和来源会话标题/路径，不执行正则、不分解中文语义。事项 ID 与事件 ID 不参与正文搜索。
- `--status`、`--item-id`、`--thread-id` 为精确筛选，可重复；同类筛选为 OR，不同类为 AND。已关联的旧 item_id 会找到同项目下整个规范事项的历史。
- `--since` / `--until` 要求带时区的 ISO 时间，窗口为 `[since, until)`；省略则不限制对应边界。
- 默认返回最新 20 条命中进展的摘要，日期、状态、关键词必须在同一条进展上成立。`matches` 与 `records` 一一对应，包含原运行及记录位置、`is_current` 和该事项的当前记录状态。历史命中不可冒充当前状态。
- **`--latest` 先选每个事项当前最新进展，再应用筛选**。查询“目前受阻/进行中”时使用它，已经解决的历史阻碍不会出现在结果中。查询一段过去时间的过程时通常不加它。
- `--order newest|oldest` 按发生时间排序，默认 newest。同一时间沿用提交时间、run-id 和记录数组位置的确定性次序。
- `--limit <正整数>`、`--offset <非负整数>` 支持分页，未指定 limit 时为 20，指定 --out 也不会自动取消分页。`total` 是分页前命中数，`returned` 为本页数量，`has_more` 表示仍有结果；继续使用 offset + returned 取下一页，不能把一页当作全部结果。分页期间避免同时修改档案。
- `--view summary|full` 默认为 summary：保留 ID、日期、项目、标题、摘要、状态与后续，不输出 details/knowledge/outcome 正文或证据节选。details_available 提示是否有这些详情；正文每字段最多预览 600 字符，来源最多预览 5 个会话、每会话 5 个事件；source_ref_count/event_count 保留总数，缩短处注明 truncated_fields。摘要查询仍在完整内容中检索。需要准确引用原文、查看完整过程或验证时，按事项用 --view full 读取原记录与对应 evidence_catalog。
- `source_catalog` 只附本页返回记录所引用的来源/事件。默认 coverage 为空列表，coverage_summary 以固定大小返回窗口内扫描次数、完整/不完整扫描次数、错误次数及有待处理事件的扫描次数；它涵盖所有项目，不能据此证明整个时间区间连续覆盖。需要逐运行覆盖详情时用 --coverage full --out 保存查看。
- `--all --out <文件>` 显式取消分页，仅写文件并在 stdout 返回保存路径；不能与 limit/offset 同用。完整导出再加 --view full，需要覆盖详情再加 --coverage full。未指定 --all 的 full 查询依然只返回一页。全量与覆盖详情都不能直接输出到 stdout。

完整周报输入可以导出为文件，再按项目分批阅读，不能把文件整段注入上下文：

```bash
python -X utf8 <skill>/scripts/memory.py query --config <config> --since <周一ISO时间> --until <本次截止ISO时间> --all --view full --coverage full --out <work>/weekly-input.json
```

也可以逐页查询至 has_more=false，再按命中事项补查 full 详情。摘要里的省略不等于原始档案缺少证据。

例如查询当前仍未完成的事项：

```bash
python -X utf8 <skill>/scripts/memory.py query --config <config> --project <project-id> --latest --status 进行中 --status 受阻 --limit 20
```

## 提炼前查找已有事项

每日处理同一问题的新进展时，先用已确定的项目 ID、问题标题和实际来源会话查询候选：

```bash
python -X utf8 <skill>/scripts/memory.py candidates --config <config> --project <project-id> --title "修复请求超时" --thread-id <thread-id> --limit 10
```

至少提供 title 或 thread-id，thread-id 可重复。默认最多返回 10 个候选，也可使用 offset 分页。候选仅来自同项目，依据标题完全匹配、标题词汇相似度和共享来源会话排序；返回 `match_reasons`、最新进展、历史数量、来源、旧 ID 别名和规范 `item_id`。分数只是确定性排序依据，不是模型置信度，也不能证明两个事项相同。

1. 先确认项目归属，查候选；不能只因共享一个会话或标题相似就合并不同问题。
2. 必要时用 `query --project <id> --item-id <候选id>` 查看历史，核对问题目标、产物与上下文。
3. 确认延续同一事项时，新记录直接复用候选的规范 item_id，写本次发生时间和新事件来源；review 中的 item_ids 同步使用它。没有合适候选才创建新 ID，不按日期生成同一事项的新 ID。
4. 同一批尚未提交的相关记录也复用同一 ID。候选只读取已保存档案，不含本批草稿。

新提交若仍使用已被关联的旧 ID，会被拒绝并提示规范 ID，避免继续分裂历史。

## 合并已重复建档的事项与纠正关联

确认两个已保存事项属于同一项目、同一工作后，可把重复 ID 指向保留的 ID：

```bash
python -X utf8 <skill>/scripts/memory.py link --config <config> --project <project-id> --item-id <duplicate-id> --target-item <canonical-id> --reason "来源会话表明二者是同一问题的后续验证" --run-id link-20261026-example
```

项目参数必须为精确 ID，两个事项必须均已存在于该项目。只建立事项归属，不修改事实、状态或项目分类；不允许跨项目合并、自关联或循环关联。遇到多个可能候选时保留独立记录，待证据明确再关联。

关联保存到不可变的 `item_links/<run-id>.json`，重建阅读视图后，查询、项目档案与以后生成的周汇总使用统一 item_id。原 `runs/`、已保存简报、采集检查点、投递账本及已经冻结的周汇总不改写；旧记录的 `original_item_id` 保留来源 ID。命中的 `matches.run_id/record_index` 可定位原始记录，enrich 仍使用原始运行中的 ID。

发现误合并时撤销该 ID 的直接关联：

```bash
python -X utf8 <skill>/scripts/memory.py unlink --config <config> --project <project-id> --item-id <duplicate-id> --reason "经回查是另一个独立故障" --run-id unlink-20261026-example
```

也可使用新的 link run-id 改指另一个已有事项。修订保留全部关联历史。链式关联中，撤销某条边会使其上游 ID 一起跟随新的终点；先查询相关历史再操作。关联后的新进展已经写在规范 ID 下，unlink 只撤销原别名关系，不猜测这些新进展应如何重新分配。

失败重试沿用同一 run-id 与相同参数，可修复派生视图；需要纠正用新的 run-id。关联不会重新消费会话或发送消息，已有冻结周汇总仍保持生成时的归属口径。
