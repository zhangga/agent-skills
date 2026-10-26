# Agent Skills

[![skills.sh](https://skills.sh/b/zhangga/agent-skills)](https://skills.sh/zhangga/agent-skills)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

我的个人 Agent Skills 集合，把日常技术工作中可以复用的流程整理成独立能力，方便自己使用，也方便分享给其他人。

目前从 **Codex 工作记忆** 开始：让做过的工作、解决过的问题和重要决策，在会话结束后仍然能找到。后续新增的 Skill 会继续按用途收录在这里。

[快速安装](#快速安装) · [Skills 目录](#skills-目录) · [开始使用](#开始使用) · [仓库结构](#仓库结构) · [常见问题](#常见问题)

## 快速安装

从合集选择需要的 Skill，并安装到 Codex 的用户级目录：

```bash
npx skills add zhangga/agent-skills -a codex -g
```

只安装 **Codex 工作记忆**：

```bash
npx skills add zhangga/agent-skills --skill codex-work-memory -a codex -g
```

后续更新这个 Skill：

```bash
npx skills update codex-work-memory -g
```

`-g` 表示跨项目使用；省略它则安装到当前项目。想先查看可安装的内容，可运行 `npx skills add zhangga/agent-skills --list`。各 Skill 的适用环境和依赖见对应说明；目前的工作记忆 Skill 面向 Codex 本地会话。

## Skills 目录

### 🧠 知识与记忆

#### [codex-work-memory · Codex 工作记忆](skills/codex-work-memory/README.md)

每天开了很多会话，过一段时间却记不起问题在哪里解决、为什么选了某个方案？这个 Skill 把分散的工作会话整理成按项目分类、带日期和来源的长期档案。

**适合这些场景：**

- **回顾工作**：从上次进度接着整理，生成每日简报和每周总结。
- **查找旧问题**：按项目、日期、状态或关键词搜索，再回到对应来源。
- **沉淀经验**：保留处理过程、技术决策、验证范围、产物和未完成事项。
- **交给个人 Agent**：每周导出一份完整文档，供助理收录，后续按来源查询。

| 你要做什么 | 对应产物 |
| --- | --- |
| 快速浏览、筛选历史 | 离线 `index.html` 阅读页 |
| 查一个项目的长期进展 | 按项目整理的 Markdown 档案 |
| 看本周成果与后续事项 | 完整周报与精简简报 |
| 让个人 Agent 收录 | 独立周汇总 Markdown / JSON，以及增量清单 |
| 在飞书接收提醒 | 可选的机器人卡片，带真实来源入口 |

**依赖与范围：** Python 3.10+，核心脚本使用标准库。Windows 为主要验证平台；时间展示和自然周口径为上海时区。本机 Codex 跳转桥接仅支持 Windows，飞书推送需另行配置 `lark-cli` 和自己的机器人。

[使用指南](skills/codex-work-memory/README.md) · [配置目录](skills/codex-work-memory/references/configuration.md) · [Agent 收录约定](skills/codex-work-memory/references/agent-export.md) · [在 skills.sh 查看](https://skills.sh/zhangga/agent-skills/codex-work-memory)

## 开始使用

安装后，在 Codex 中直接描述目标：

**首次整理**

> 使用 $codex-work-memory 初始化，把完整档案保存到我指定的目录，周汇总另存到个人 Agent 的资料目录。先整理最近 7 天，不发送消息。

**之后补记**

> 使用 $codex-work-memory 补齐上次以来的工作，按项目整理，保留问题的处理过程和验证结果。

**每周归总**

> 使用 $codex-work-memory 总结本周，生成供个人 Agent 收录的完整汇总文档，保留来源和未完成事项。

**查询历史**

> 使用 $codex-work-memory 查找上个月某项目的超时问题，说明最后如何解决，并附日期和原会话来源。

完整档案与 Agent 汇总可以保存到不同目录。自动日报、周报和飞书投递需按自己的时间及接收账号配置，安装不会直接启用它们。详细步骤见 [工作记忆使用指南](skills/codex-work-memory/README.md)。

## 仓库结构

```text
agent-skills/
├── README.md                    # 合集介绍、安装与导航
├── LICENSE
└── skills/
    └── codex-work-memory/
        ├── README.md            # 给使用者看的指南
        ├── SKILL.md             # 给 Agent 执行的工作流程
        ├── config.example.json  # 不含个人信息的配置模板
        ├── agents/              # Codex 展示与调用配置
        ├── scripts/             # 采集、存档、导出与可选投递
        ├── references/          # 格式、配置和写作约定
        ├── assets/              # 本地阅读页样式与交互
        └── tests/               # 合成数据测试
```

每个 Skill 是一个独立目录。以后增加新能力时，在 `skills/` 下新增目录并补充这里的分类介绍即可。

## 常见问题

**一定要有 `skills/` 这一层吗？**

不需要。根目录直接放多个 Skill 文件夹也可以被发现。本仓库保留 `skills/`，把能力目录集中起来，根目录留给合集说明和共享文件。[官方发现规则](https://github.com/vercel-labs/skills/blob/main/docs/creating-skills.md)明确支持 `skills/<name>/SKILL.md`，也有其他目录和递归回退机制。安装后，用户仍通过 Skill 名称调用。

**能不能只复制文件安装？**

可以。把 `skills/codex-work-memory/` 整个复制到自己的 Codex skills 目录，再按指南初始化个人配置。通过 CLI 安装则更方便获取更新。

**更新 Skill 会影响我的工作档案吗？**

新配置默认放在 Skill 目录外，完整档案和 Agent 汇总也按个人配置保存。旧版若仍把 `config.json` 放在安装目录内，建议先备份并按[迁移说明](skills/codex-work-memory/references/configuration.md)迁出，再使用安装器更新。保留原有采集和投递账本，才能延续进度与去重。

**会把会话上传到 GitHub 或 skills.sh 吗？**

发布的是 Skill 代码与文档，工作档案保存在自己的目录。可选飞书投递只发送用户授权的简报。本 Skill 由宿主 Agent 执行，模型如何处理会话内容仍取决于你的 Codex 配置。默认读取范围为实际可访问的本地活动与归档会话，未连接的远程和纯云端会话不在其中。

## 开发与反馈

在仓库根目录运行：

```bash
python -m unittest discover -s skills/codex-work-memory/tests -v
node --test skills/codex-work-memory/tests/filter.test.cjs
```

测试使用合成数据。提交改动时只包含可分享的程序、文档和示例，个人配置、会话记录及投递账本留在仓库外。问题和建议可通过 [Issues](https://github.com/zhangga/agent-skills/issues) 提交。

## License

[MIT](LICENSE)。
