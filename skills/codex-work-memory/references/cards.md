# 飞书卡片简报

用户启用并授权飞书推送时，默认使用可读、带真实来源按钮的卡片。Markdown 简报继续作为可迁移的文字稿保存，实际推送使用 Card 2.0。卡片只提供阅读和跳转，不添加待办勾选、审批、发布或服务端回调。

`config.delivery.message_format=interactive` 保存该偏好；实际脚本仍用 `--card` 明确选择格式，便于兼容旧消息重试。`config.delivery.archive_locations_url` 是已经发送并回读的档案位置说明，可作为无新增回执等场景的阅读按钮；不能把它标成在线完整报告。

## 生成流程

核心整理不依赖飞书。投递需要已配置并登录的 `lark-cli`；先检查 `lark-cli auth status --format json`，账号的 app ID 必须与配置一致。已安装 `lark-im` / `lark-shared` 时可使用它们帮助配置身份、核对最新卡片字段；没有这些 Skill 也可使用本包的 `card.py` 固定模板及 `deliver.py`。不要绕过 CLI 读取凭据或把密钥复制进配置。

固定模板自行校验字段、链接和大小，修改模板结构时查飞书官方对应版本的卡片文档；既有内容的生成按以下字段约定执行。按钮必须有真实目的地址，没有可用网页入口时保留本地档案并说明，不能编造网址。

1. 根据已保存的记录与文字简报准备 `<archive>/cards/<run-id>.spec.json`。文案均为纯文本，不在字段里塞 Markdown、HTML 或自行拼出的链接。
2. 运行 `python -X utf8 <skill>/scripts/card.py --config <config> --spec <spec> --out <archive>/cards/<run-id>.json`，检查成功、字节数、元素数和结构。存在 Codex 来源时，CLI 自动刷新本机入口目录并检查服务；失败要先修复，不能回退为已验证无法点击的直连。
3. 核对信息覆盖、状态与验收范围、项目归属、来源链接、手机阅读，以及唯一主按钮。不得以裁切长文的方式隐藏未完成状态。
4. 运行 `python -X utf8 <skill>/scripts/deliver.py --config <config> --archive <archive> --run-id <run-id> --card <card-json>`。卡片和 Markdown 是不同发送格式，同一发送 ID 不得改内容或格式；旧卡片的修订另用新 ID。
5. 使用 `lark-cli im +messages-mget --message-ids <returned-id> --as bot --no-reactions --format json` 回读，确认发送者为配置指定的机器人、消息类型为 `interactive`。必要时用原生消息读取检查卡片的按钮与折叠结构；不能把回读成功说成用户已点击或已阅读。

真实发送结果未知时遵循投递账本规则，先只读核对，不自动重新发送。API 明确拒绝卡片时才修正结构并重试；不要静默降成普通文本然后宣称卡片成功。

明确的结构拒绝需要修改 JSON 时，使用新的修订发送 ID，保留原失败快照，并在原账本标记 `retryable=false` 和 `superseded_by=<新ID>`；后续自动补发只跟进新的投递，不能再补发被替代的坏卡片。已验证模板不使用会被接口拒绝的 `column.corner_radius`；圆角只放在支持它的折叠面板 border 上。

## 输入格式

`card.py` 的输入字段：

- `title`、`subtitle`、`window`：标题、短副标题、上海时区时间范围；旧内容重新设计注明修订版。
- `summary`：一句最重要的结果，作为正文唯一最强焦点。
- `headline`：可选，36 字以内的结果短标题。提供时它是正文唯一最强焦点，`summary` 放在其下补充证据和待验收边界；不重复标题。
- `stats`：可选数组，每项 `{"label":"工作事项","value":"57"}`，最多 3 项；只统计本次真实覆盖。
- `projects`：最多 5 个重点项目；每项包含 `name`、`summary`，可选 `status`、`followup`、`sources`、`highlights`。`highlights` 最多 4 项，每项 `{"title":"24字以内的成果标题","text":"100字以内的结果和验证范围"}`。项目涉及多项成果时，用短 summary 交代整体变化，再用 highlights 分条；不要把同一内容重复写进 summary、highlights 和后续。
- `priorities`：最多 3 条具体后续，建议与会话明确待办要区分。
- `additional`：其余项目，同 projects 的结构，折叠呈现。简报可以精选，不要求每次用满项目上限。
- `footer`：覆盖范围、本机文件说明和平台限制。
- `actions`：1～2 个主要阅读入口，第一项为主按钮。
- `verified_codex_thread_ids`：本次确实从来源记录核实的会话 UUID；不是任意链接白名单。

链接项格式为 `{"label":"按钮文案","url":"实际地址"}`；可按实际平台补 `pc_url`、`ios_url`、`android_url`。每项目通常只放 1 个最相关来源按钮，确有必要可有 2 个。不同目的明确命名，例如“GA 配对原会话 · 电脑”“查看参考原文”“查看文字版简报”“查看记录位置”。

## 链接的真实性与平台

- 优先使用工具返回的飞书 `message_app_link`、已有文档 URL，以及原会话实际出现的仓库/文章/产物 URL。既有简报链接只能叫“文字版简报”，不能叫“原始会话”；位置说明链接不能叫“在线详细报告”。
- 桌面来源使用 `codex://threads/<UUID>`，UUID 必须来自本次 source_refs/source_catalog；仅限本机来源，不把远程会话套成本机链接。不同安装环境需验证其协议处理器是否可用。
- 飞书可能阻止直接打开 `codex://`。可选 Windows 本机桥接将 spec 中真实 Codex 地址转为 HTTP 入口，按钮标注“原会话入口 · 本机”。用户打开入口页后，再点“在 Codex 中打开”，由 Windows 启动原会话；不能把其他机器上的可用性视为当前机器已验证。`ios_url`/`android_url` 使用精确禁跳值 `lark://msgcard/unsupported_action`。
- 本机 `C:\...` 路径不能当作 HTTPS 页面或 `file://` 按钮。将 `index.html` 和原始记录目录放在折叠说明中，或链接到已发送的文件位置说明。没有在线详细报告时，明确说明它仍在本机。
- 未经用户要求，不创建公开的会话分享链接，不更改文件/文档访问权限，不部署公开文件服务器来绕过本机路径限制。
- 来源中的登录链接、token、签名下载地址或含凭证的 URL 不放按钮。企业仓库或文档可能需要原有登录权限，不声称已公开可访问。

## 本机入口的维护

`config.delivery.codex_link_bridge` 保存 `enabled` 与固定 `port`（建议 48871），新配置默认关闭。仅在 Windows 和已授权配置下启用。`scripts/link_bridge_manager.py prepare --config <config>` 从已保存记录中生成 `<archive>/link-bridge/catalog.json`，只包含确实引用且本地日志仍存在的会话 UUID 和标题。`codex_link_bridge.py` 仅监听 `127.0.0.1`；目录更新即时生效。入口不提供会话正文、文件浏览或任意命令执行。GET/HEAD 和预览不会启动应用，只有页面按钮的同源 POST 会请求 Windows 打开。

默认不安装登录启动项。用户要求时，`link_bridge_manager.py install-startup --config <config>` 可创建专用 Windows 当前用户启动项；它只启动本机入口，不调整日报/周报时间。`status --config <config>` 检查服务与启动项；`prepare --config <config>` 可恢复服务；`uninstall-startup --config <config>` 删除专用启动项。换电脑或端口时，旧卡片地址不会自动更新，必须另发修正版。启动项不会监控服务崩溃，生成下一张卡片时会再次检查并补启动。

新建卡片前若出现端口被其他服务占用、来源日志已不存在、服务未能启动等错误，保留错误并处理原因。不要停掉未知进程、修改正常的 Codex 协议关联、开放公网端口或把失败包装成成功。已发送修订使用新 run-id，原发送账本和采集时间基线保持可追溯。

## 版式

采用轻量报告版式：蓝色 header、`width_mode=default`，正文控制为 2～5 个顶层视觉块。概览用一条结果短标题和必要说明，最多 3 个浅底统计小格作为辅助，不用大数字抢占焦点。项目保持全宽浅底分组，项目名、灰色状态标签、成果条目分层；技术摘要不硬塞三列。来源用小号次按钮，底部仅一个填色主按钮。下一步集中列出，档案路径与覆盖说明放在“档案与来源”折叠区。深浅主题均有颜色定义，关键状态和验收限制自然换行，不用截断隐藏。

旧 spec 可继续使用；没有 headline 或 highlights 时按原摘要字段生成。界面修订使用新发送 run-id 并注明原统计范围，不推进采集时间，也不把历史工作算作新增。

无新增时使用同一输入格式，简述扫描范围和“本次无新增工作记录”，保留有用阅读入口；不制造空的项目、指标或待办来填版式。
