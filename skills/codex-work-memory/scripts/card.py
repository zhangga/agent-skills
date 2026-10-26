#!/usr/bin/env python3
"""Build a bounded, read-only Feishu Card 2.0 from a plain-text brief spec.

CLI: python -X utf8 card.py --config ../config.json --spec brief.card-spec.json --out brief.card.json
All text fields are plain text, not Markdown. Source links are explicit objects.
Only verified Codex UUIDs may be used as source URLs or desktop overrides; the caller must
verify those IDs from real source records before putting them in the allowlist.
The pure builder does not send messages, read credentials, or advance checkpoints.
For Codex sources, the CLI prepares the loopback bridge before generating links.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
MAX_CARD_BYTES = 28_000
MAX_ELEMENTS = 190


def text(value, name, limit, optional=False):
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: expected non-empty plain text")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name}: exceeds {limit} characters; shorten the brief")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise ValueError(f"{name}: control characters are not allowed")
    return value


def sequence(value, name, maximum, minimum=0):
    if value is None:
        value = []
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name}: expected {minimum}..{maximum} items")
    return value


def mapping(value, name, allowed):
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"{name}: unknown fields {sorted(unknown)}")
    return value


def escape_md(value):
    """HTML numeric escaping is the documented Card markdown literal syntax."""
    specials = set("&<>*~[]()#:_`!|\\")
    return "".join(f"&#{ord(ch)};" if ch in specials else ch for ch in value)


def validate_url(value, name, verified_ids=(), allow_codex=False, allow_unsupported=False):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"{name}: URL must contain 1..2048 characters")
    if re.search(r"\s|[\x00-\x1f\x7f\\]", value):
        raise ValueError(f"{name}: whitespace, controls and backslashes are not allowed")
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"}:
        if not parts.hostname or parts.username or parts.password:
            raise ValueError(f"{name}: URL must have a hostname and no credentials")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError(f"{name}: invalid port") from exc
        return value
    if allow_unsupported and value == "lark://msgcard/unsupported_action":
        return value
    match = re.fullmatch(r"codex://threads/(" + UUID_PATTERN + r")", value)
    if allow_codex and match and match.group(1).lower() in verified_ids:
        return value
    raise ValueError(f"{name}: use http(s), or an allowlisted Codex thread desktop override")


def link(value, name, verified_ids, codex_bridge_base_url=None):
    mapping(value, name, {"label", "url", "pc_url", "ios_url", "android_url"})
    result = {"label": text(value.get("label"), name + ".label", 20)}
    result["url"] = validate_url(value.get("url"), name + ".url", verified_ids, allow_codex=True)
    for key in ("pc_url", "ios_url", "android_url"):
        if value.get(key):
            result[key] = validate_url(value[key], name + "." + key, verified_ids,
                                       allow_codex=key == "pc_url", allow_unsupported=key != "pc_url")
    if result["url"].startswith("codex://"):
        if result.get("pc_url", result["url"]) != result["url"]:
            raise ValueError(f"{name}: Codex desktop override must match its source URL")
        result["pc_url"] = result["url"]
        for key in ("ios_url", "android_url"):
            if result.get(key, "lark://msgcard/unsupported_action") != "lark://msgcard/unsupported_action":
                raise ValueError(f"{name}: Codex source buttons must disable mobile opening")
            result[key] = "lark://msgcard/unsupported_action"
        if codex_bridge_base_url is None:
            raise ValueError("Codex source links require the local link bridge; direct Feishu deep links failed on this desktop")
        base = urlsplit(codex_bridge_base_url)
        if (base.scheme != "http" or base.hostname != "127.0.0.1" or not base.port
                or base.username or base.password or base.path or base.query or base.fragment):
            raise ValueError("Codex link bridge must be http://127.0.0.1:<port>")
        result["url"] = result["pc_url"] = codex_bridge_base_url + "/thread/" + result["url"].rsplit("/", 1)[1]
    elif result.get("pc_url", "").startswith("codex://"):
        raise ValueError("Use a Codex source URL with the local link bridge, not a direct desktop override")
    return result


def paragraph(value, *, size="normal", color="default", lines=None, align="left"):
    result = {"tag": "div", "text": {"tag": "plain_text", "content": value,
              "text_size": size, "text_color": color, "text_align": align}, "margin": "0px"}
    if lines is not None:
        result["text"]["lines"] = lines
    return result


def heading(value, *, focus=False, color=None):
    content = escape_md(value)
    if color:
        content = "<font color='" + color + "'>" + content + "</font>"
    return {"tag": "markdown", "content": "**" + content + "**",
            "text_size": "focus" if focus else "normal", "margin": "0px"}


def block(elements, *, background=None, padding="0px", spacing="8px"):
    column = {"tag": "column", "width": "weighted", "weight": 1,
              "padding": padding, "vertical_spacing": spacing, "elements": elements}
    if background:
        column["background_style"] = background
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
            "columns": [column], "margin": "0px"}


def button(item, *, primary=False, compact=False):
    behavior = {"type": "open_url", "default_url": item["url"]}
    behavior.update({key: item[key] for key in ("pc_url", "ios_url", "android_url") if key in item})
    return {"tag": "button", "text": {"tag": "plain_text", "content": item["label"]},
            "type": "primary_filled" if primary else "default",
            "width": "default" if compact else "fill",
            "size": "small" if compact else "medium", "behaviors": [behavior], "margin": "0px"}


def buttons(items, *, primary_first=False):
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
            "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                         "elements": [button(item, primary=primary_first and i == 0)]}
                        for i, item in enumerate(items)], "margin": "0px"}


def project(value, name, verified_ids, codex_bridge_base_url=None):
    mapping(value, name, {"name", "summary", "status", "followup", "sources", "highlights"})
    highlights = []
    for h in sequence(value.get("highlights"), name + ".highlights", 4):
        mapping(h, name + ".highlights", {"title", "text"})
        highlights.append({"title": text(h.get("title"), name + ".highlights.title", 24),
                           "text": text(h.get("text"), name + ".highlights.text", 100)})
    return {"name": text(value.get("name"), name + ".name", 36),
            "summary": text(value.get("summary"), name + ".summary", 200),
            "status": text(value.get("status"), name + ".status", 16, optional=True),
            "followup": text(value.get("followup"), name + ".followup", 120, optional=True),
            "highlights": highlights,
            "sources": [link(s, name + ".sources", verified_ids, codex_bridge_base_url)
                        for s in sequence(value.get("sources"), name + ".sources", 2)]}


def project_elements(item):
    elements = [heading(item["name"], color="cus-primary")]
    if item["status"]:
        elements.append({"tag": "markdown", "content": "<text_tag color='neutral'>" +
                         escape_md(item["status"]) + "</text_tag>", "text_size": "notation", "margin": "0px"})
    elements.append(paragraph(item["summary"], color="cus-muted" if item["highlights"] else "default"))
    for h in item["highlights"]:
        elements.append({"tag": "markdown", "content": "**" + escape_md(h["title"]) + "**\n" + escape_md(h["text"]),
                         "text_size": "normal", "margin": "0px"})
    if item["followup"]:
        elements.append(paragraph("后续：" + item["followup"]))
    if item["sources"]:
        elements.extend(button(s, compact=True) for s in item["sources"])
    return elements


def statistics(items):
    """Supporting counts stay smaller than the single editorial headline."""
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
            "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                         "background_style": "cus-stat-bg", "padding": "8px",
                         "vertical_spacing": "4px", "elements": [
                             {"tag": "markdown", "content": "**<font color='cus-primary'>" + escape_md(value) + "</font>**",
                              "text_size": "normal", "text_align": "center", "margin": "0px"},
                             paragraph(label, size="notation", color="cus-muted", align="center")]}
                        for label, value in items], "margin": "0px"}


def count_elements(value):
    if isinstance(value, dict):
        return (1 if "tag" in value else 0) + sum(count_elements(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_elements(v) for v in value)
    return 0


def build_card(spec, codex_bridge_base_url=None):
    mapping(spec, "spec", {"title", "subtitle", "window", "headline", "summary", "stats", "projects",
                          "priorities", "additional", "footer", "actions", "verified_codex_thread_ids"})
    verified_ids = sequence(spec.get("verified_codex_thread_ids"), "verified_codex_thread_ids", 100)
    if any(not isinstance(v, str) or not re.fullmatch(UUID_PATTERN, v) for v in verified_ids):
        raise ValueError("verified_codex_thread_ids: only source-verified UUIDs are allowed")
    verified_ids = {v.lower() for v in verified_ids}
    title = text(spec.get("title"), "title", 36)
    subtitle = text(spec.get("subtitle"), "subtitle", 60)
    window = text(spec.get("window"), "window", 90)
    summary = text(spec.get("summary"), "summary", 96)
    headline = text(spec.get("headline"), "headline", 36, optional=True)
    footer = text(spec.get("footer"), "footer", 350)
    projects = [project(p, "projects", verified_ids, codex_bridge_base_url) for p in sequence(spec.get("projects"), "projects", 5)]
    additional = [project(p, "additional", verified_ids, codex_bridge_base_url) for p in sequence(spec.get("additional"), "additional", 8)]
    priorities = [text(p, "priorities", 150) for p in sequence(spec.get("priorities"), "priorities", 3)]
    actions = [link(a, "actions", verified_ids, codex_bridge_base_url) for a in sequence(spec.get("actions"), "actions", 2, minimum=1)]
    stats = []
    for s in sequence(spec.get("stats"), "stats", 3):
        mapping(s, "stats", {"label", "value"})
        stats.append((text(s.get("label"), "stats.label", 16), text(s.get("value"), "stats.value", 16)))

    overview = [heading(headline or summary, focus=True)]
    if headline:
        overview.append(paragraph(summary))
    overview.append(paragraph(window, size="notation", color="cus-muted"))
    if stats:
        overview.append(statistics(stats))
    elements = [block(overview, background="cus-primary-bg", padding="12px", spacing="12px")]
    if projects:
        project_blocks = [heading("项目进展")]
        project_blocks += [block(project_elements(p), background="cus-surface", padding="12px", spacing="8px") for p in projects]
        elements.append(block(project_blocks, spacing="12px"))
    if priorities:
        elements.append(block([heading("下一步")] + [paragraph("• " + p) for p in priorities],
                              background="cus-surface", padding="12px"))
    details = [block(project_elements(p), padding="8px") for p in additional]
    details.append(paragraph(footer, size="notation", color="cus-muted"))
    elements.append({"tag": "collapsible_panel", "expanded": False,
                     "header": {"title": {"tag": "plain_text", "content": "其他项目与档案" if additional else "档案与来源"},
                                "width": "fill"},
                     "border": {"color": "grey-200", "corner_radius": "8px"},
                     "padding": "8px", "vertical_spacing": "8px", "elements": details, "margin": "0px"})
    elements.append(block([buttons(actions, primary_first=True)], spacing="8px"))
    card = {"schema": "2.0", "config": {"update_multi": True, "width_mode": "default",
            "summary": {"content": title + "｜" + summary}, "style": {
              "text_size": {"focus": {"default": "heading-3", "pc": "heading-3", "mobile": "heading-3"}},
              "color": {
              "cus-primary": {"light_mode": "rgba(36,91,219,1)", "dark_mode": "rgba(135,174,255,1)"},
              "cus-primary-bg": {"light_mode": "rgba(36,91,219,0.06)", "dark_mode": "rgba(100,150,255,0.12)"},
              "cus-stat-bg": {"light_mode": "rgba(255,255,255,0.78)", "dark_mode": "rgba(150,180,255,0.08)"},
              "cus-surface": {"light_mode": "rgba(36,91,219,0.025)", "dark_mode": "rgba(150,175,220,0.06)"},
                "cus-muted": {"light_mode": "rgba(100,106,115,1)", "dark_mode": "rgba(166,171,180,1)"}}}},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "subtitle": {"tag": "plain_text", "content": subtitle},
                       "template": "blue", "icon": {"tag": "standard_icon", "token": "calendar_colorful"}},
            "body": {"direction": "vertical", "padding": "12px", "vertical_spacing": "12px", "elements": elements}}
    metrics = inspect_card(card)
    if metrics["elements"] > MAX_ELEMENTS or metrics["bytes"] > MAX_CARD_BYTES:
        raise ValueError(f"Card too large: {metrics}; shorten the brief or link to full detail")
    return card


def inspect_card(card):
    """Structural checks of the generated vocabulary, not a substitute for server validation."""
    nodes = []
    container_depth = 0
    def visit(value, depth=0):
        nonlocal container_depth
        if isinstance(value, dict):
            depth += int(value.get("tag") in {"column_set", "column", "collapsible_panel"})
            container_depth = max(container_depth, depth)
            nodes.append(value)
            for val in value.values():
                visit(val, depth)
        elif isinstance(value, list):
            for val in value:
                visit(val, depth)
    visit(card)
    count = count_elements(card)
    size = len(json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    top_blocks = len(card["body"]["elements"])
    focus_count = sum(n.get("tag") == "markdown" and n.get("text_size") == "focus" for n in nodes)
    if not 2 <= top_blocks <= 5 or focus_count != 1:
        raise ValueError("Card must have 2..5 visual blocks and exactly one body text focus")
    if container_depth > 5:
        raise ValueError("Card container nesting exceeds 5 levels")
    if any(n.get("type") == "callback" for n in nodes):
        raise ValueError("Callbacks are not allowed")
    return {"schema": "2.0", "top_level_blocks": top_blocks, "body_text_focus": focus_count,
            "elements": count, "bytes": size, "buttons": sum(n.get("tag") == "button" for n in nodes),
            "container_depth": container_depth, "callbacks": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
        bridge_base = None
        def has_codex(value):
            if isinstance(value, dict):
                return any(has_codex(v) for v in value.values())
            if isinstance(value, list):
                return any(has_codex(v) for v in value)
            return isinstance(value, str) and value.startswith("codex://")
        if has_codex(spec):
            import link_bridge_manager
            bridge_base = link_bridge_manager.prepare(args.config, required_ids=spec.get("verified_codex_thread_ids", []))["base_url"]
        card = build_card(spec, codex_bridge_base_url=bridge_base)
        # Compact JSON keeps the written payload identical to the measured size.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(card, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(json.dumps({"ok": True, "output": str(args.out), **inspect_card(card)}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
