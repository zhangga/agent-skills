"""Readable, dependency-free Markdown and HTML views of the work ledger."""
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def when(value, day=False):
    if not value:
        return "时间未记录"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)
    return dt.strftime("%Y-%m-%d" if day else "%Y-%m-%d %H:%M")


def anchor(value):
    return "item-" + hashlib.sha256(value.encode()).hexdigest()[:12]


def project_filename(pid):
    return (re.sub(r"[^A-Za-z0-9_-]", "_", pid) + ".md")[:100] + "-" + hashlib.sha256(json.dumps(pid, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8] + ".md"


def items_grouped(records):
    projects = {}
    for record in records:
        project = projects.setdefault(record["project_id"], {"name": record["project_name"], "items": {}})
        project["items"].setdefault(record["item_id"], []).append(record)
    for project in projects.values():
        for history in project["items"].values():
            history.sort(key=lambda item: datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")), reverse=True)
        project["items"] = dict(sorted(project["items"].items(), key=lambda pair: datetime.fromisoformat(pair[1][0]["occurred_at"].replace("Z", "+00:00")), reverse=True))
    return dict(sorted(projects.items(), key=lambda pair: pair[1]["name"].casefold()))


def source_name(ref, catalog):
    metadata = catalog.get(ref["thread_id"], {})
    title = " ".join(metadata.get("title", "").split())
    if not title:
        title = "原始会话（标题未收录）"
    return title, metadata


def source_time(ref, metadata):
    events = metadata.get("events", {})
    times = [events[e] for e in ref["event_ids"] if e in events]
    if times and len(times) == len(ref["event_ids"]):
        times = sorted(times, key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")))
        first, last = when(times[0]), when(times[-1])
        return "引用时间 " + first + (" — " + last if first != last else "")
    if metadata.get("last_event_at"):
        return "会话最近时间 " + when(metadata["last_event_at"])
    return ""


def status_class(status):
    if status in {"受阻"}:
        return "blocked"
    if status in {"进行中", "已修改"}:
        return "progress"
    if status in {"已完成", "已发布"}:
        return "complete"
    return "neutral"


def md_text(value):
    # Source titles are plain data, not Markdown instructions or headings.
    return str(value).replace("\n", " ").replace("[", "\\[").replace("]", "\\]")


def detail_md(record, catalog):
    lines = [record["summary"]]
    for key, label in [("outcome", "结果与证据"), ("follow_up", "下一步"), ("knowledge", "可复用结论")]:
        if record.get(key):
            lines += ["", f"**{label}**", "", record[key]]
    details = record.get("details", {})
    if details.get("context"):
        lines += ["", "**背景与目标**", "", details["context"]]
    for key, label in [("approach", "解决过程"), ("decisions", "关键判断"), ("validation", "验证记录"), ("limitations", "边界与未完成项")]:
        if details.get(key):
            lines += ["", "**" + label + "**", ""] + ["- " + value for value in details[key] if value]
    if details.get("artifacts"):
        lines += ["", "**成果与文件**", ""]
        for artifact in details["artifacts"]:
            lines.append(f"- {md_text(artifact['label'])}：`{artifact['path']}`")
    lines += ["", "**来源会话**", ""]
    for ref in record["source_refs"]:
        title, meta = source_name(ref, catalog)
        date = f" · {source_time(ref, meta)}" if source_time(ref, meta) else ""
        lines.append(f"- {md_text(title)}{date} · 引用 {len(ref['event_ids'])} 条事件")
        # Keep machine identifiers available for traceability, below the human title.
        lines.append(f"  - 会话标识：`{ref['thread_id']}`")
        if meta.get("source_path"):
            lines.append(f"  - 原始日志：`{meta['source_path']}`")
    return "\n".join(lines)


def item_md(item_id, history, catalog):
    latest = history[0]
    lines = [f'<a id="{anchor(latest["project_id"] + ":" + item_id)}"></a>', "", "### " + md_text(latest["title"]), "", f"**{latest['status']}** · {latest['category']} · 最近进展 {when(latest['occurred_at'])}", "", detail_md(latest, catalog)]
    if len(history) > 1:
        lines += ["", f"#### 历史进展（{len(history) - 1} 条）", "", "以下是较早记录；当前状态以上面的最近进展为准。"]
        for earlier in history[1:]:
            lines += ["", f"##### {when(earlier['occurred_at'])} · {earlier['status']}", "", detail_md(earlier, catalog)]
    return "\n".join(lines) + "\n"


def project_md(pid, project, catalog, heading=1):
    items = project["items"]
    count = sum(len(h) for h in items.values())
    lines = ["#" * heading + " " + md_text(project["name"]), "", f"{len(items)} 个工作事项 · {count} 条进展记录 · 时间均为上海时间", "", "**事项目录**", ""]
    for iid, history in items.items():
        latest = history[0]
        lines.append(f"- [{md_text(latest['title'])}](#{anchor(pid + ':' + iid)}) · {latest['status']} · {when(latest['occurred_at'], True)}")
    lines += [""] + [item_md(iid, history, catalog) for iid, history in items.items()]
    return "\n".join(lines)


def report_md(run):
    catalog = run.get("source_catalog", {})
    groups = items_grouped(run["records"])
    counts = run.get("coverage", {}).get("counts", {})
    total = sum(len(p["items"]) for p in groups.values())
    window = run["window"]
    lines = ["# Codex 工作记录", "", f"{when(window['since'])} — {when(window['until'])}（上海时间，结束时刻不含）", "", f"**{total} 个事项 · {len(groups)} 个分类 · {len(run['records'])} 条进展**", "", "按项目归档；每个事项先呈现最新状态，再保留历史进展。", "", "## 项目导航", ""]
    for pid, project in groups.items():
        lines.append(f"- [{md_text(project['name'])}](#project-{anchor(pid)}) · {len(project['items'])} 个事项")
    for pid, project in groups.items():
        lines += ["", f'<a id="project-{anchor(pid)}"></a>', "", project_md(pid, project, catalog, 2)]
    if not run["records"]:
        lines += ["本次无新增工作事项。", ""]
    coverage = run.get("coverage", {})
    errors = coverage.get("errors", [])
    lines += ["", "## 来源与覆盖范围", "", "本机活动与已归档 Codex 会话；远程及纯云端会话未覆盖。", "", f"读取 {counts.get('files_read', 0)} 个日志文件；{counts.get('threads_with_new_events', 0)} 个会话包含新增内容，共 {counts.get('new_events', 0)} 条新增事件；读取或完整性问题 {len(errors)} 项。", "", "本次本地扫描完整。" if coverage.get("complete_local") is True else "本次存在覆盖缺口，完整采集基线尚未越过缺口；后续将补跑。", "", f"生成时间：{when(run['created_at'])}", ""]
    if errors:
        labels = {"missing_source_root": "来源目录不可用", "enumeration": "目录枚举失败", "metadata_read": "会话索引读取失败", "metadata_parse": "会话索引解析失败", "source_read": "日志读取失败", "source_parse": "日志解析失败", "source_changed": "读取期间日志变化", "missing_thread_id": "日志缺少会话标识", "metadata_source_unavailable": "近期会话日志缺失"}
        kinds = {}
        for error in errors:
            label = labels.get(error.get("kind"), "其他来源问题")
            kinds[label] = kinds.get(label, 0) + 1
        lines += ["缺口：" + "；".join(f"{label} {count} 项" for label, count in kinds.items()) + "。", ""]
    return "\n".join(lines)


def html_index(records, catalog, ledger):
    """A self-contained archive; queries match a single dated revision at a time."""
    from pathlib import Path
    from urllib.parse import urlsplit

    esc = lambda value: html.escape(str(value), quote=True)
    groups = items_grouped(records)

    def paragraph(value, css=''):
        return f"<p class='{css}'>" + esc(value).replace('\n', '<br>') + '</p>'

    def section(label, body, css=''):
        return f"<section class='field {css}'><h4>{label}</h4>{body}</section>"

    def path_row(path):
        return f"<div class='path-row'><code>{esc(path)}</code><button type='button' class='copy-path' data-copy='{esc(path)}' aria-label='复制路径'>复制</button></div>"

    def safe_web_url(value):
        try:
            url = urlsplit(value)
            return url.scheme in {'http', 'https'} and bool(url.hostname) and not url.username and not url.password and not re.search(r'[\x00-\x20]', value)
        except ValueError:
            return False

    def record_body(record):
        body = paragraph(record['summary'], 'revision-summary')
        for key, label in [('outcome', '结果与证据'), ('follow_up', '下一步'), ('knowledge', '可复用结论')]:
            if record.get(key):
                body += section(label, paragraph(record[key]), 'field-followup' if key == 'follow_up' else '')
        details = record.get('details', {})
        if details.get('context'):
            body += section('背景与目标', paragraph(details['context']))
        for key, label in [('approach', '解决过程'), ('decisions', '关键判断'), ('validation', '验证记录'), ('limitations', '边界与未完成项')]:
            if details.get(key):
                body += section(label, '<ul>' + ''.join('<li>' + esc(v) + '</li>' for v in details[key] if v) + '</ul>')
        if details.get('artifacts'):
            artifacts = []
            for artifact in details['artifacts']:
                path = artifact['path']
                link = f" <a class='artifact-link' href='{esc(path)}' target='_blank' rel='noopener noreferrer'>打开链接</a>" if safe_web_url(path) else ''
                artifacts.append(f"<li><strong>{esc(artifact['label'])}</strong>{link}{path_row(path)}</li>")
            body += section('成果与文件', "<ul class='artifacts'>" + ''.join(artifacts) + '</ul>')
        sources = []
        for ref in record['source_refs']:
            title, meta = source_name(ref, catalog)
            sources.append(f"<li><strong>{esc(title)}</strong><p class='muted'>{esc(source_time(ref, meta))} · 引用 {len(ref['event_ids'])} 条事件</p><details class='trace'><summary>原始日志与会话标识</summary>{path_row(meta.get('source_path', '日志位置未收录'))}<code>会话 {esc(ref['thread_id'])}</code><code>事件 {' / '.join(esc(e) for e in ref['event_ids'])}</code></details></li>")
        return body + section('来源会话', "<ul class='sources'>" + ''.join(sources) + '</ul>')

    def plain_values(value):
        if isinstance(value, dict):
            return '\n'.join(plain_values(v) for v in value.values())
        if isinstance(value, list):
            return '\n'.join(plain_values(v) for v in value)
        return str(value) if value is not None else ''

    items, cards, templates = [], [], []
    for pid, project in groups.items():
        for iid, history in project['items'].items():
            item_id = anchor(pid + ':' + iid)
            latest = history[0]
            revisions = []
            bodies = []
            for index, record in enumerate(history):
                template_id = item_id + '-r' + str(index)
                body = record_body(record)
                bodies.append(body)
                templates.append(f"<template id='{template_id}'>{body}</template>")
                source_text = '\n'.join(source_name(ref, catalog)[0] + '\n' + source_name(ref, catalog)[1].get('source_path', '') for ref in record['source_refs'])
                revisions.append({'title': record['title'], 'status': record['status'], 'category': record['category'],
                    'summary': record['summary'], 'next': record.get('follow_up', ''), 'time': when(record['occurred_at']),
                    'day': when(record['occurred_at'], True), 'occurredAt': record['occurred_at'],
                    'stamp': int(datetime.fromisoformat(record['occurred_at'].replace('Z', '+00:00')).timestamp() * 1000),
                    'search': plain_values(record) + '\n' + source_text, 'template': template_id})
            items.append({'id': item_id, 'project': pid, 'projectName': project['name'], 'revisions': revisions})
            history_html = ''.join(f"<details class='history-entry'><summary>{when(earlier['occurred_at'])} · {esc(earlier['status'])}</summary>{bodies[n]}</details>" for n, earlier in enumerate(history[1:], 1))
            history_hidden = '' if len(history) > 1 else ' hidden'
            next_hidden = '' if latest.get('follow_up') else ' hidden'
            card = f"""<article class='card' data-project='{esc(pid)}' data-selected='0'>
<details class='item' id='{item_id}'><summary>
<div class='card-top'><span class='project-label'>{esc(project['name'])}</span><span class='category-label'>{esc(latest['category'])}</span><time datetime='{esc(latest['occurred_at'])}'>{when(latest['occurred_at'])}</time></div>
<h3>{esc(latest['title'])}</h3><div class='meta'><span class='status {status_class(latest['status'])}'>{esc(latest['status'])}</span><span class='revision-count'>{len(history)} 条进展</span></div>
<p class='match-note' hidden></p><p class='preview'>{esc(latest['summary'])}</p><p class='search-excerpt' hidden></p>
<p class='next'{next_hidden}><b>下一步</b><span>{esc(latest.get('follow_up', ''))}</span></p>
<span class='expand-label'><span class='closed-label'>查看详细记录与来源</span><span class='open-label'>收起详细记录</span></span></summary>
<div class='record-content body'><div class='selected-revision'>{bodies[0]}</div>
<section class='history'{history_hidden}><h4 class='history-heading'>历史进展 · {len(history) - 1} 条</h4><p class='muted'>各条状态反映当时进展；最近一条为当前记录状态。</p><div class='history-list'>{history_html}</div></section></div></details></article>"""
            cards.append((revisions[0]['stamp'], card))
    cards.sort(key=lambda entry: entry[0], reverse=True)
    count = len(items)
    bounds = [when(r['occurred_at'], True) for r in records]
    period = f"{min(bounds)} — {max(bounds)}" if bounds else '尚无记录'
    scans = [run for run in ledger if run.get('mode') != 'weekly']
    until = max((run['window']['until'] for run in scans), key=lambda t: datetime.fromisoformat(t.replace('Z', '+00:00'))) if scans else None
    updated = '最近采集 ' + when(until) if until else '尚无采集记录'
    options = ''.join(f"<option value='{esc(pid)}'>{esc(p['name'])}</option>" for pid, p in groups.items())
    status_options = ''.join(f"<option value='{esc(status)}'>{esc(status)}</option>" for status in sorted({r['status'] for r in records}))
    nav = f"<button type='button' class='project-link active' data-project='' aria-pressed='true'><span class='name'>全部项目</span><span class='project-count'>{count}</span></button>"
    nav += ''.join(f"<button type='button' class='project-link' data-project='{esc(pid)}' aria-pressed='false'><span class='name'>{esc(p['name'])}</span><span class='project-count'>{len(p['items'])}</span></button>" for pid, p in groups.items())
    reports = ''.join(f"<li><a href='{('weekly' if r['mode'] == 'weekly' else 'daily')}/{esc(r['run_id'])}.md'>{'周报' if r['mode'] == 'weekly' else '详细记录'} · {when(r['window']['since'], True)} — {when(r['window']['until'], True)}</a> <a href='runs/{esc(r['run_id'])}.json'>原始 JSON</a></li>" for r in reversed(ledger))
    assets = Path(__file__).resolve().parent.parent / 'assets'
    css = (assets / 'archive.css').read_text(encoding='utf-8')
    script = (assets / 'archive.js').read_text(encoding='utf-8')
    data = json.dumps({'items': items}, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    return f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><meta name='color-scheme' content='light dark'><title>Codex 工作档案</title><style>{css}</style></head>
<body><a class='skip-link' href='#search'>跳到搜索</a><div class='app-shell'>
<header class='masthead'><div class='brand-icon' aria-hidden='true'>记</div><div class='brand-copy'><h1>工作档案</h1><p>Codex 工作记忆</p></div><div class='archive-meta'>{esc(updated)}<br>上海时间 · 本机档案</div></header>
<section class='overview' aria-label='档案概览'><div class='overview-copy'><h2>做过的工作，随时找得回来。</h2><p>按项目回看成果，用时间定位进展，沿来源找回解决过程。</p><p class='muted'>记录范围 {esc(period)}</p></div><div class='stats'><div class='stat'><strong>{count}</strong><span>工作事项</span></div><div class='stat'><strong>{len(groups)}</strong><span>项目分类</span></div><div class='stat'><strong>{len(records)}</strong><span>进展记录</span></div></div></section>
<div class='workspace'><aside class='sidebar' aria-label='项目导航'><h2 class='nav-heading'>项目与分类</h2><nav id='project-nav'>{nav}</nav><p class='sidebar-note'>同一事项的多次进展合并展示，历史过程和原始来源完整保留。</p></aside>
<main class='content'><section class='filter-panel' aria-label='筛选历史记录'><form id='filters'>
<div class='search-row'><div class='search-box'><span class='search-icon' aria-hidden='true'>⌕</span><label class='sr-only' for='search'>搜索历史记录</label><input id='search' type='search' placeholder='搜索问题、结论、文件或来源会话' autocomplete='off'></div></div>
<div class='filter-row'><div class='control'><label for='project'>项目</label><select id='project'><option value=''>全部项目</option>{options}</select></div><div class='control'><label for='status'>记录状态</label><select id='status'><option value=''>全部状态</option>{status_options}</select></div><div class='control'><label for='sort'>排序</label><select id='sort'><option value='newest'>进展时间 · 最新优先</option><option value='oldest'>进展时间 · 最早优先</option><option value='project'>按项目名称</option></select></div></div>
<div class='date-range'><div class='control'><label for='from'>开始日期</label><input id='from' type='date' aria-describedby='date-help date-error'></div><span class='date-separator' aria-hidden='true'>至</span><div class='control'><label for='to'>结束日期</label><input id='to' type='date' aria-describedby='date-help date-error'></div><div class='quick-ranges' aria-label='快捷时间范围'><button type='button' class='range-chip active' data-range='all' aria-pressed='true'>全部时间</button><button type='button' class='range-chip' data-range='7' aria-pressed='false'>最近 7 天</button><button type='button' class='range-chip' data-range='30' aria-pressed='false'>最近 30 天</button><button type='button' class='range-chip' data-range='week' aria-pressed='false'>本周</button></div></div>
<div class='filter-footer'><p class='filter-note' id='date-help'>按进展发生日筛选，起止日期均包含；多个关键词用空格分开。</p><button type='button' id='reset' class='btn-quiet'>清空筛选</button></div><p id='date-error' role='alert'></p></form></section>
<div id='active-filters' aria-label='当前筛选'></div><div class='results-bar'><div><h2 id='results-title'>全部记录</h2><span id='count' aria-live='polite'>{count} 个事项 · {len(records)} 条进展</span></div><div class='view-actions'><button type='button' id='expand'>展开结果</button><button type='button' id='collapse'>收起</button><button type='button' id='print'>打印 / PDF</button></div></div>
<div id='cards' class='cards'>{''.join(card for _, card in cards)}</div><div id='empty' class='empty-state' hidden><h3>没有找到匹配的记录</h3><p>试试缩短关键词、扩大日期范围，或切换到全部项目。</p><button type='button' id='empty-reset'>清空筛选</button></div>
<footer class='support'><details><summary>档案位置、运行报告与覆盖范围</summary><p>本机活动与已归档 Codex 会话；远程及纯云端会话未覆盖。记录保留当时的验证范围，不代表项目目前仍处于相同状态。</p><p><a href='index.md'>Markdown 索引</a> · 原始记录 runs/ · 详情修订 enrichments/ · 采集基线 state.json</p><ul>{reports}</ul></details></footer></main></div></div>
<div id='toast' role='status' hidden></div><noscript><p>JavaScript 未启用：仍可展开阅读全部记录，筛选与搜索需要在浏览器中启用 JavaScript。</p></noscript>
{''.join(templates)}<script id='archive-data' type='application/json'>{data}</script><script>{script}</script></body></html>"""
