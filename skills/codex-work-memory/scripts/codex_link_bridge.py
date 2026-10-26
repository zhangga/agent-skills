"""Optional, local-only HTTP landing page for verified Codex thread links.

Does not install startup tasks or alter protocol registrations.
Run: python -X utf8 codex_link_bridge.py --catalog thread-titles.json --port 48871
Catalog format: {"<canonical UUID>": "Human readable title", ...}.

GET/HEAD never launch an application. A user submits the landing-page form;
only same-origin POST with a short-lived, single-use nonce can launch a
catalogued thread. The launcher receives only codex://threads/<UUID>.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Callable
from urllib.parse import parse_qs


THREAD_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ROUTE_RE = re.compile(r"/thread/(" + THREAD_RE.pattern + r")(/open)?")
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_BODY = 128
NONCE_TTL = 300.0
MAX_NONCES = 4096
SERVICE = "codex-work-memory-link-bridge"
VERSION = 1


def validate_catalog(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("Catalog must be a nonempty UUID-to-title object")
    result = {}
    for thread_id, title in value.items():
        if not isinstance(thread_id, str) or not THREAD_RE.fullmatch(thread_id):
            raise ValueError("Catalog contains an invalid thread ID")
        if not isinstance(title, str) or not title.strip() or len(title) > 2000:
            raise ValueError("Catalog contains an invalid title")
        result[thread_id] = title.strip()
    return result


def windows_launcher(uri: str) -> None:
    # Never route input through a shell, a command string, or an executable path.
    os.startfile(uri, "open")


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        catalog: dict[str, str],
        port: int = 48871,
        *,
        launcher: Callable[[str], None] = windows_launcher,
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[[dict], None] | None = None,
        catalog_path: Path | None = None,
    ):
        self.catalog = validate_catalog(catalog)
        self.launcher = launcher
        self.clock = clock
        self.logger = logger or (lambda event: print(json.dumps(event), flush=True))
        self.catalog_path = catalog_path.resolve() if catalog_path else None
        canonical_path = str(self.catalog_path) if self.catalog_path else ""
        if os.name == "nt":
            canonical_path = canonical_path.casefold()
        self.catalog_id = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
        self.catalog_stamp = None
        self.catalog_lock = threading.Lock()
        self.catalog_ready = True
        self.nonces: dict[str, tuple[str, float]] = {}
        self.nonce_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), BridgeHandler)
        self.host = f"127.0.0.1:{self.server_address[1]}"
        self.origin = f"http://{self.host}"

    def refresh_catalog(self) -> bool:
        if self.catalog_path is None:
            return True
        with self.catalog_lock:
            try:
                stat = self.catalog_path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if stamp != self.catalog_stamp:
                    catalog = validate_catalog(json.loads(self.catalog_path.read_text(encoding="utf-8-sig")))
                    self.catalog = catalog
                    self.catalog_stamp = stamp
                self.catalog_ready = True
            except (OSError, ValueError):
                # Never retain launch permission from stale or malformed data.
                self.catalog_ready = False
            return self.catalog_ready

    def issue_nonce(self, thread_id: str) -> str | None:
        now = self.clock()
        with self.nonce_lock:
            self.nonces = {key: value for key, value in self.nonces.items() if value[1] > now}
            if len(self.nonces) >= MAX_NONCES:
                return None
            nonce = secrets.token_urlsafe(32)
            self.nonces[nonce] = (thread_id, now + NONCE_TTL)
            return nonce

    def consume_nonce(self, nonce: str, thread_id: str) -> bool:
        with self.nonce_lock:
            record = self.nonces.get(nonce)
            if record is None or record[0] != thread_id:
                return False
            del self.nonces[nonce]  # Consume before launching, even on launch error.
            return record[1] > self.clock()

    def audit(self, route: str, result: str, thread_id: str | None = None) -> None:
        # No raw URL, request headers, title, nonce, filesystem path or exception.
        self.logger({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "route": route,
            "thread": thread_id,
            "result": result,
        })


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    server_version = "CodexLocalLink"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Base implementation can log raw URLs and request text; suppress it.
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code, "请求不可用", "请使用简报中的本机链接重新打开。")

    def one_header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def check_host(self) -> bool:
        if self.one_header("Host") != self.server.host:
            self.server.audit("request", "host_rejected")
            self.respond(403, "入口地址不匹配", "请使用简报中指向 127.0.0.1 的本机链接。")
            return False
        return True

    def parse_route(self, opening: bool) -> str | None:
        if not self.server.refresh_catalog():
            self.server.audit("thread", "catalog_unavailable")
            self.respond(503, "本机会话目录不可用", "请稍后重试，或重新生成工作简报以更新来源目录。")
            return None
        match = ROUTE_RE.fullmatch(self.path)
        if match is None or bool(match[2]) != opening:
            self.server.audit("request", "route_rejected")
            self.respond(404, "没有这个会话入口", "链接必须是已经核实的 Codex 会话。")
            return None
        thread_id = match[1]
        if thread_id not in self.server.catalog:
            self.server.audit("thread", "unknown_thread")
            self.respond(404, "会话尚未核实", "这个会话不在本机简报来源目录中。")
            return None
        return thread_id

    def do_HEAD(self):
        self.landing(head=True)

    def do_GET(self):
        self.landing(head=False)

    def landing(self, *, head: bool):
        if not self.check_host():
            return
        if self.path == "/health":
            self.health()
            return
        # Cross-site top-level navigation is needed for Feishu entry links.
        # Other foreign-origin reads are not needed, and none enables launch.
        origins = self.headers.get_all("Origin", [])
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if (origins and origins != [self.server.origin]) or len(sites) > 1:
            self.server.audit("thread", "origin_rejected")
            self.respond(403, "请求来源不受支持", "请从飞书按钮直接打开本机入口。")
            return
        if sites and sites[0] not in {"same-origin", "none"}:
            if self.one_header("Sec-Fetch-Mode") != "navigate":
                self.server.audit("thread", "fetch_site_rejected")
                self.respond(403, "请求来源不受支持", "请从飞书按钮直接打开本机入口。")
                return
        thread_id = self.parse_route(opening=False)
        if thread_id is None:
            return
        if head:
            self.server.audit("thread", "preview", thread_id)
            self.respond(200, "本机 Codex 会话入口", "预览不会启动 Codex。")
            return
        nonce = self.server.issue_nonce(thread_id)
        if nonce is None:
            self.respond(503, "本机入口暂忙", "请稍后重新打开此页。")
            return
        title = html.escape(self.server.catalog.get(thread_id, "Codex 会话"))
        form = (
            f'<div class="thread">{title}</div>'
            f'<form method="post" action="/thread/{thread_id}/open">'
            f'<input type="hidden" name="nonce" value="{nonce}">'
            '<button type="submit">在 Codex 中打开</button></form>'
        )
        self.server.audit("thread", "landing", thread_id)
        self.respond(200, "回到原始会话", "这是当前电脑的本机入口。点击下方按钮后，由 Windows 打开已核实的 Codex 会话。", form + self.fallback(thread_id))

    def do_POST(self):
        if not self.check_host():
            return
        thread_id = self.parse_route(opening=True)
        if thread_id is None:
            return
        origin = self.one_header("Origin")
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if origin != self.server.origin or (sites and sites != ["same-origin"]):
            self.server.audit("open", "origin_rejected", thread_id)
            self.respond(403, "请从本机入口点击按钮", "此请求没有通过来源校验，请重新打开简报中的会话入口。")
            return
        content_type = self.one_header("Content-Type")
        if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            self.respond(415, "请求格式不受支持", "请重新打开会话入口后点击按钮。")
            return
        length_text = self.one_header("Content-Length")
        if self.headers.get_all("Transfer-Encoding") or length_text is None or not re.fullmatch(r"[0-9]{1,3}", length_text):
            self.respond(400, "请求格式不受支持", "请重新打开会话入口后点击按钮。")
            return
        length = int(length_text)
        if not 1 <= length <= MAX_BODY:
            self.respond(413, "请求过大", "请重新打开会话入口后点击按钮。")
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            form = parse_qs(raw.decode("ascii"), strict_parsing=True, max_num_fields=1)
        except (ValueError, UnicodeDecodeError, TimeoutError):
            self.respond(400, "请求格式不受支持", "请重新打开会话入口后点击按钮。")
            return
        nonces = form.get("nonce", [])
        if set(form) != {"nonce"} or len(nonces) != 1 or not NONCE_RE.fullmatch(nonces[0]):
            self.respond(400, "请求格式不受支持", "请重新打开会话入口后点击按钮。")
            return
        if not self.server.consume_nonce(nonces[0], thread_id):
            self.server.audit("open", "nonce_rejected", thread_id)
            self.respond(409, "此按钮已使用或已过期", "为避免重复启动，请重新打开本机入口后再试。", self.fallback(thread_id, retry=True))
            return
        uri = f"codex://threads/{thread_id}"
        try:
            self.server.launcher(uri)
        except Exception:
            self.server.audit("open", "launch_error", thread_id)
            self.respond(502, "Windows 未能启动 Codex", "请确认当前电脑已安装 Codex。可以复制下方原链接，在 Windows 的“运行”窗口中打开。", self.fallback(thread_id, retry=True))
            return
        self.server.audit("open", "launch_requested", thread_id)
        self.respond(200, "已请求打开 Codex", "已将目标会话链接交给 Windows。请检查 Codex 窗口是否打开了这个会话；此页面无法确认应用内的最终跳转。", self.fallback(thread_id, retry=True))

    def health(self):
        ready = self.server.refresh_catalog()
        payload = json.dumps({
            "service": SERVICE,
            "version": VERSION,
            "catalog_id": self.server.catalog_id,
            "catalog_ready": ready,
        }).encode("utf-8")
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(payload)

    def fallback(self, thread_id: str, *, retry: bool = False) -> str:
        title = html.escape(self.server.catalog.get(thread_id, "Codex 会话"))
        uri = f"codex://threads/{thread_id}"
        markup = (
            '<section class="fallback"><p>原始会话链接 · 点击输入框后可全选复制</p>'
            f'<input aria-label="原始 Codex 会话链接" readonly value="{uri}">'
            f'<p class="muted">{title}</p>'
            '<p class="muted">仅当前电脑可用。手机和其他电脑不能通过此地址访问本机 Codex。</p></section>'
        )
        if retry:
            markup += f'<a class="retry" href="/thread/{thread_id}">返回本机入口</a>'
        return markup

    def respond(self, status: int, heading: str, description: str, body: str = ""):
        page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Codex · 本机会话入口</title><style>'
                '*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#1e293b;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}'
                'main{max-width:720px;margin:9vh auto;padding:38px;background:#fff;border:1px solid #e2e8f0;border-radius:20px}'
                '.eyebrow{color:#64748b;font-size:13px;letter-spacing:.12em}h1{font-size:28px;line-height:1.3;margin:18px 0}'
                '.thread{background:#f1f5f9;border-radius:12px;padding:18px;margin:24px 0;white-space:pre-wrap;overflow-wrap:anywhere}'
                'button{border:0;border-radius:10px;padding:14px 24px;background:#2563eb;color:#fff;font:inherit;font-weight:650;cursor:pointer}'
                'button:hover{background:#1d4ed8}.fallback{border-top:1px solid #e2e8f0;margin-top:30px;padding-top:15px;font-size:14px}'
                'input[readonly]{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:8px;background:#f8fafc;font:13px monospace}'
                '.muted{color:#64748b;font-size:13px;overflow-wrap:anywhere}.retry{display:inline-block;color:#2563eb;margin-top:8px}'
                '@media(max-width:760px){main{margin:24px 14px;padding:24px}h1{font-size:24px}}'
                '</style><main><div class="eyebrow">CODEX / LOCAL LINK</div>'
                f'<h1>{html.escape(heading)}</h1><p>{html.escape(description)}</p>{body}</main></html>')
        payload = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        # Chromium suppresses form POST Origin under no-referrer (Origin:null).
        # Keep Origin on this same-origin form; send no referrer cross-origin.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description="Local landing page for verified Codex thread links")
    parser.add_argument("--catalog", required=True, type=Path, help="JSON object mapping verified UUIDs to titles")
    parser.add_argument("--port", type=int, default=48871)
    parser.add_argument("--log", type=Path, help="Append safe audit events as JSONL")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        catalog = validate_catalog(json.loads(args.catalog.read_text(encoding="utf-8-sig")))
        logger = None
        if args.log is not None:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            log_lock = threading.Lock()
            def logger(event):
                with log_lock:
                    with args.log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event) + "\n")
        server = BridgeServer(catalog, args.port, logger=logger, catalog_path=args.catalog)
    except (OSError, ValueError):
        parser.exit(1, "Unable to load the verified catalog or bind the local port.\n")
    server.audit("startup", "listening")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.audit("shutdown", "stopped")


if __name__ == "__main__":
    main()
