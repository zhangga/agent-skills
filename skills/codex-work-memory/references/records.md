# 工作事项格式

`records.json` 是数组。每条记录包含：

```json
{
  "item_id": "example-model-catalog",
  "occurred_at": "2026-10-23T12:00:00+08:00",
  "project_id": "project-id-or-unassigned",
  "project_name": "示例项目",
  "category": "功能开发",
  "status": "已验证",
  "title": "同步模型配置",
  "summary": "完成了什么以及为什么。",
  "outcome": "证据支持的结果和验证范围。",
  "follow_up": "未完成内容；没有则空字符串。",
  "knowledge": "仅填写已证实、值得复用的结论及适用条件；否则空字符串。",
  "source_refs": [{"thread_id": "actual-id", "event_ids": ["actual-event-id"]}]
}
```

`source_refs` 必须引用 batch 中实际新增的非 context_only 事件。一个事项可关联多个事件和会话。`occurred_at` 必须在此次采集窗口内并来自事件时间，跨多天进展可拆为同 item_id 的多条修订。状态用 `已讨论`、`调研结论`、`已修改`、`已验证`、`已发布`、`进行中`、`受阻` 或 `已完成`，只有会话有相应证据时才能提升状态。

同一事项以发生时间较晚的记录为当前结果；时间相同时，后提交的运行优先，同一运行中数组位置靠后的记录优先。旧运行提交时间完全相同时以 run-id 排序作为确定性次序。阅读页、项目 Markdown 和 Agent 周汇总使用相同口径，历史记录仍保留。

提炼前通过已有事项候选核对归属，延续同一事项时复用规范 item_id。历史重复 ID 的显式关联、撤销与查询筛选见 [retrieval.md](retrieval.md)。关联是独立的可追溯修订，不改写原记录的事实或发生日期。

## 事件审阅清单

新建 daily/bootstrap 运行必须向 `memory.py commit` 传入 `--review <review.json>`。清单为数组，对 batch 中 **eligible 来源的每条非 context_only 新事件**恰好填写一次：

```json
[
  {"thread_id": "actual-id", "event_id": "actual-event-id", "disposition": "recorded", "item_ids": ["example-model-catalog"]},
  {"thread_id": "actual-id", "event_id": "status-event-id", "disposition": "ignored", "reason": "仅重复询问状态，没有新的工作内容。"},
  {"thread_id": "actual-id", "event_id": "pending-event-id", "disposition": "pending", "reason": "尚未审阅，需要下一批读取来源上下文。"}
]
```

- `recorded` 的 `item_ids` 必须与引用该事件的本批记录完全对应；一个事件可以支撑多个事项。
- `ignored` 与 `pending` 必须给出非空原因，且不能被本批记录引用。只有确认无需收录才用 ignored，不能批量把未读事件标为忽略。
- `context_only`、ineligible、未知和重复事件不能加入清单。缺项或引用不一致会拒绝提交；无 eligible 新事件时传 `[]`。
- 只有 recorded/ignored 进入已消费检查点。pending 及其原因保存在运行中，可由 `state.json` 的 `pending_reviews` 查询；后续默认采集会回到未处理起点，不受 48 小时回看限制。首次部分提交即使没有 `initial_since` 也可从保存的窗口恢复。
- 部分提交成功仅代表已审阅部分保存成功，简报须说明待处理数量。继续处理先重新采集，再用新的 run-id 提交；失败重试同一 run-id 必须沿用原清单，不能改写已冻结处置。

新采集运行使用 schema_version 2，保存 `event_reviews`、原始 `batch_checkpoints` 和实际消费的 `checkpoints`。旧版运行与档案仍可读取和重建；重试已经存在的旧版运行时按原参数执行，不传 `--review`，其内容和消费历史保持冻结。`enrich` 仍使用原 batch，不能通过补充详情改变审阅处置。

## 事项详情

重要事项增加可选的 `details` 对象，写作标准见 [writing.md](writing.md)。只填写有证据的字段，兼容没有 details 的历史记录：

重要排障与验证事项使用 `importance: "high"`，并按 [evidence.md](evidence.md) 增加 `details.evidence`；保存时带同批次的 `--evidence` 文件。证据条目保存 claim、level、result、scope 和实际 evidence_ids，部署验证另有 environment。旧记录不要求补填这些字段才能读取。

```json
{
  "details": {
    "context": "发生问题的背景、触发条件或用户目标。",
    "approach": ["关键定位步骤或修改范围。"],
    "decisions": ["方案选择及其理由。"],
    "validation": ["会话记录显示的测试条件、结果及验证边界。"],
    "artifacts": [{"label": "实际产物及用途", "path": "来源中真实出现的路径或地址"}],
    "limitations": ["尚未验收的环节或已知适用限制。"]
  }
}
```

已有记录补充细节使用 enrich 命令及独立 `enrichments/` 目录，不修改原 `runs/`。补充数组中的 `item_id` 和 `occurred_at` 精确匹配原记录；其他原字段保持原样，仅补充 `details`。原 batch 可提供会话标题、事件时间和 JSONL 路径等来源元数据。补充详情不新增事项、不重复计数、不推进 state.json 中的采集检查点。

日期和状态描述必须可核对，助手声称通过的验证写明“会话记录显示”；无需每次重新运行历史测试。没有结果的用户请求只能记为待办或进行中，不可补写结果。知识更正保留新旧来源，查询时指出新结论替代旧结论。

`memory.py` 将不可变运行记录保存到 `runs/<run-id>.json`，详情修订到 `enrichments/`，日报到 `daily/`，周报到 `weekly/`，项目视图到 `projects/`，简报文字稿到 `briefs/`；`index.html` 为可搜索阅读入口，`state.json` 可从运行记录重建。卡片输入与生成 JSON 保存在 `cards/`，由 card.py 生成；delivery 状态与实际发送快照独立保存在 `delivery/`。

每周另自动输出可独立收录的完整文档至 `agent_export_dir`，格式与同步约定见 [agent-export.md](agent-export.md)。这是基于同一证据的周快照，不是新增工作记录。
