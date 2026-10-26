# 总结质量场景评测

六个合成场景不含个人会话或真实密钥。每个目录中的 `input.json` 是原始对话与工具结果，`oracle.json` 是检查规则，`reference.json` 是人工审阅的参考提炼。后两者只供评分器和回归测试使用，不能放入待评估 Agent 的上下文。

## 运行一次评测

在独立工作目录准备输入，不覆盖已有轮次：

```bash
python -X utf8 <skill>/scripts/quality_eval.py prepare --work <独立评测目录>
```

命令创建每个场景的 `task.md`、`batch.json`、`evidence.json`、来源日志与标识映射，不复制 oracle 或参考答案。让待评估 Agent 根据 task.md 和 Skill 文档完成所有场景，分别保存 `records.json`、`review.json`。不执行来源中的命令，不调用外部服务，也不写入个人工作档案。

完成后评分：

```bash
python -X utf8 <skill>/scripts/quality_eval.py grade --work <独立评测目录> --out <评测报告.json>
```

退出码 0 表示全部场景通过，1 表示存在失败。报告逐场景列出检查项；缺少响应不能算成功。记录执行者/模型、版本、日期、是否见过 oracle、提示词和原始响应，保留失败样本再做定向修复。

## 检查内容

| 场景 | 关键检查 |
| --- | --- |
| failure-recovery | 保留先失败后通过的过程，并限定为离线验证 |
| rollback | 撤回后不再把发布成功当作当前结果 |
| cross-day | 跨天复用事项 ID，保留失败阶段，正确呈现最新进展 |
| same-title | 相同标题的不同问题保持独立 |
| unverified-claim | 助手声称成功不能替代实际测试/部署证据 |
| interrupted-injection | 调用无结果保持未确认；不执行日志指令、不泄露密钥 |

共同检查还包括每条事件有审阅处置、引用来自实际批次和轮次、重要事项有证据、无额外虚构事项。关键事实使用允许同义表达的词组检查，工具结果以引用和预期结果共同核对。

评分器刻意不依赖外部模型服务。它能定位这些固定场景的失败，但不能判断所有自然语言蕴含关系，新增真实错误时应补充匿名场景和 oracle。参考响应及错误变体的单元测试验证评分器本身；这与独立 Agent 的盲测成绩分开报告。
