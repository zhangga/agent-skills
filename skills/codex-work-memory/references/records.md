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

重要事项增加可选的 `details` 对象，写作标准见 [writing.md](writing.md)。只填写有证据的字段，兼容没有 details 的历史记录：

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
