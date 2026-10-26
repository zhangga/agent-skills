#!/usr/bin/env python3
"""Manage the loopback-only Codex work-memory link bridge (stdlib only).

No command installs startup implicitly. ``prepare`` updates a title-only allowlist
and starts the bridge if this specific service is not already running.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

SERVICE = "codex-work-memory-link-bridge"
VERSION = 1
THREAD_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_TITLE = 240

_config_spec = importlib.util.spec_from_file_location("codex_work_memory_configuration", Path(__file__).with_name("configuration.py"))
configuration = importlib.util.module_from_spec(_config_spec)
_config_spec.loader.exec_module(configuration)


def settings(config_path: Path) -> dict:
    config_path = configuration.resolve_config_path(config_path)
    config = configuration.load_config(config_path)
    bridge = config.get("delivery", {}).get("codex_link_bridge", {})
    if bridge.get("enabled") is not True:
        raise ValueError("Codex link bridge is not enabled in this config")
    port = bridge.get("port", 48871)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Codex link bridge port must be an integer from 1024 to 65535")
    archive = Path(config["archive_dir"])
    if not archive.is_absolute():
        archive = config_path.parent / archive
    directory = archive.resolve() / "link-bridge"
    return {"config": config, "config_path": config_path, "archive": archive.resolve(),
            "directory": directory, "catalog": directory / "catalog.json", "port": port,
            "base_url": f"http://127.0.0.1:{port}"}


def catalog_id(path: Path) -> str:
    normalized = str(Path(path).resolve())
    if os.name == "nt":
        normalized = normalized.casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def local_absolute_path(value) -> Path | None:
    if not isinstance(value, str) or not value or value.startswith(("//", "\\\\")):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, ValueError):
        return None
    if str(candidate).startswith(("//", "\\\\")):
        return None
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(candidate.anchor)) == 4:
            return None
    return candidate


def build_catalog(config: dict, archive: Path, memory_module=None) -> dict:
    """Only record-referenced, existing local sources may become open targets."""
    memory_module = memory_module or importlib.import_module("memory")
    ledger = memory_module.resolved_runs(Path(archive))
    source_catalog = memory_module.merged_catalog(ledger)
    referenced = {
        ref.get("thread_id")
        for run in ledger for record in run.get("records", [])
        for ref in record.get("source_refs", []) if isinstance(ref, dict)
    }
    roots = [root for value in config.get("source_roots", [])
             if (root := local_absolute_path(value)) is not None and root.is_dir()]
    allowed = {}
    for thread_id in sorted(ref for ref in referenced if isinstance(ref, str)):
        if not THREAD_ID.fullmatch(thread_id):
            continue
        metadata = source_catalog.get(thread_id, {})
        source = local_absolute_path(metadata.get("source_path"))
        if source is None or not source.is_file() or not any(source.is_relative_to(root) for root in roots):
            continue
        title = metadata.get("title")
        allowed[thread_id.lower()] = (title if isinstance(title, str) and title else thread_id)[:MAX_TITLE]
    return allowed


def atomic_json(path: Path, value) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".catalog-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def manager_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / ".manager.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 15
        while not acquired:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another link bridge manager is running")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def probe(port: int, expected_catalog_id: str) -> str:
    """Return running/unready/stopped/conflict; never reuse another listener."""
    # Windows can take ~2 seconds to report WSAECONNREFUSED even on loopback.
    # A shorter connect timeout mistakes a stopped helper for an occupied port.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/health", headers={"Connection": "close"})
        response = connection.getresponse()
        payload = response.read(4097)
        if response.status not in (200, 503) or len(payload) > 4096:
            return "conflict"
        body = json.loads(payload)
        if not (isinstance(body, dict) and body.get("service") == SERVICE
                and body.get("version") == VERSION
                and body.get("catalog_id") == expected_catalog_id):
            return "conflict"
        return "running" if response.status == 200 and body.get("catalog_ready", True) else "unready"
    except ConnectionRefusedError:
        return "stopped"
    except (OSError, ValueError, http.client.HTTPException):
        return "conflict"
    finally:
        connection.close()


def python_background() -> Path:
    executable = Path(sys.executable).resolve()
    if os.name == "nt":
        executable = executable.with_name("pythonw.exe")
    if not executable.is_file():
        raise RuntimeError("A local Python background executable is required")
    return executable


def start_bridge(options: dict):
    script = Path(__file__).resolve().with_name("codex_link_bridge.py")
    if not script.is_file():
        raise RuntimeError("The adjacent codex_link_bridge.py is missing")
    command = [str(python_background()), str(script), "--port", str(options["port"]),
               "--catalog", str(options["catalog"]), "--log", str(options["directory"] / "access.jsonl")]
    kwargs = {"stdin": subprocess.DEVNULL, "shell": False, "close_fds": True,
              "cwd": str(script.parent)}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    with (options["directory"] / "process.log").open("ab") as output:
        return subprocess.Popen(command, stdout=output, stderr=output, **kwargs)


def prepare(config_path: Path, required_ids=()) -> dict:
    if os.name != "nt":
        raise RuntimeError("The Codex desktop link bridge currently supports Windows only; keep it disabled on other platforms")
    options = settings(config_path)
    with manager_lock(options["directory"]):
        identity = catalog_id(options["catalog"])
        current = probe(options["port"], identity)
        if current == "conflict":
            raise RuntimeError(f"Loopback port {options['port']} is occupied by a different or unresponsive service")
        catalog = build_catalog(options["config"], options["archive"])
        missing = [thread_id for thread_id in required_ids
                   if not isinstance(thread_id, str) or thread_id.lower() not in catalog]
        if missing:
            raise ValueError("Required Codex conversation sources are not verified in the local catalog")
        atomic_json(options["catalog"], catalog)
        started = current == "stopped"
        process = None
        if started:
            process = start_bridge(options)
        if current != "running":
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                current = probe(options["port"], identity)
                if current == "running":
                    break
                if current == "conflict" or (process is not None and process.poll() is not None):
                    raise RuntimeError("Link bridge did not start; inspect its local process.log")
                time.sleep(0.15)
            if current != "running":
                raise RuntimeError("Timed out starting the local link bridge; inspect its process.log")
    return {"ok": True, "base_url": options["base_url"], "catalog_count": len(catalog), "started": started}


def startup_path(config_path: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("Startup shortcuts are supported only on Windows")
    directory = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    key = hashlib.sha256(os.path.normcase(str(Path(config_path).resolve())).encode("utf-8")).hexdigest()[:12]
    return directory / f"Codex Work Memory Link Bridge-{key}.lnk"


def status(config_path: Path) -> dict:
    options = settings(config_path)
    state = probe(options["port"], catalog_id(options["catalog"]))
    count = 0
    if options["catalog"].is_file():
        saved = json.loads(options["catalog"].read_text(encoding="utf-8"))
        count = len(saved) if isinstance(saved, dict) else 0
    return {"ok": state not in ("conflict", "unready"), "state": state, "base_url": options["base_url"],
            "catalog_count": count,
            "startup_installed": startup_path(config_path).is_file() if os.name == "nt" else False}


def install_startup(config_path: Path) -> dict:
    options = settings(config_path)
    shortcut = startup_path(options["config_path"])
    manager = Path(__file__).resolve()
    values = {"path": str(shortcut), "target": str(python_background()),
              "arguments": subprocess.list2cmdline([str(manager), "prepare", "--config", str(options["config_path"])]),
              "cwd": str(manager.parent)}
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    script = """param([Parameter(Mandatory=$true)][string]$DataPath)
$ErrorActionPreference = 'Stop'
$bridgeShortcutData = Get-Content -LiteralPath $DataPath -Raw -Encoding UTF8 | ConvertFrom-Json
$bridgeShell = New-Object -ComObject WScript.Shell
$bridgeShortcut = $bridgeShell.CreateShortcut($bridgeShortcutData.path)
$bridgeShortcut.TargetPath = $bridgeShortcutData.target
$bridgeShortcut.Arguments = $bridgeShortcutData.arguments
$bridgeShortcut.WorkingDirectory = $bridgeShortcutData.cwd
$bridgeShortcut.WindowStyle = 7
$bridgeShortcut.Description = 'Start the local Codex work-memory link bridge'
$bridgeShortcut.Save()
"""
    with tempfile.TemporaryDirectory(prefix="codex-bridge-shortcut-") as temporary:
        temporary = Path(temporary)
        data = temporary / "shortcut.json"
        data.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8-sig")
        source = temporary / "shortcut.ps1"
        source.write_text(script, encoding="utf-8-sig")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(source), "-DataPath", str(data)],
                                   shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=20, **kwargs)
        if completed.returncode or not shortcut.is_file():
            raise RuntimeError("Could not create the current-user startup shortcut")
    return {"ok": True, "startup_installed": True, "shortcut": str(shortcut)}


def uninstall_startup(config_path: Path) -> dict:
    # Allow disabling config first; removing the dedicated shortcut must still work.
    shortcut = startup_path(Path(config_path).resolve())
    existed = shortcut.is_file()
    if existed:
        shortcut.unlink()
    return {"ok": True, "startup_installed": False, "removed": existed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "status", "install-startup", "uninstall-startup"])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    commands = {"prepare": prepare, "status": status, "install-startup": install_startup,
                "uninstall-startup": uninstall_startup}
    try:
        args.config = configuration.resolve_config_path(args.config)
        result = commands[args.command](args.config)
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
