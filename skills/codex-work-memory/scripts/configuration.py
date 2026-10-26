"""Resolve personal configuration independently from the installed skill."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path


MANAGED_DIRS = {
    "runs", "daily", "bootstrap", "weekly", "briefs", "projects", "delivery", "enrichments",
    "cards", "verification", "link-bridge", "item_links",
}


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser().resolve()


def default_config_path() -> Path:
    return default_codex_home() / "codex-work-memory" / "config.json"


def resolve_config_path(config_path=None) -> Path:
    """Explicit paths and the environment override never silently fall back."""
    explicit = config_path or os.environ.get("CODEX_WORK_MEMORY_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    external = default_config_path()
    legacy = Path(__file__).resolve().parent.parent / "config.json"
    if external.is_file() or not legacy.is_file():
        return external
    return legacy


def absolute_path(value, base: Path, field: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError(f"{field} must be a non-empty filesystem path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def normalize_config(config: dict, config_path: Path, *, require_archive=True) -> dict:
    """Resolve relative paths against the config file, never the calling cwd."""
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    cfg = copy.deepcopy(config)
    base = Path(config_path).expanduser().resolve().parent
    codex_home = absolute_path(cfg.get("codex_home", default_codex_home()), base, "codex_home")
    cfg["codex_home"] = str(codex_home)
    source_roots = cfg.get("source_roots", [codex_home / "sessions", codex_home / "archived_sessions"])
    if not isinstance(source_roots, list):
        raise ValueError("source_roots must be a list of paths")
    roots = [absolute_path(path, base, "source_roots") for path in source_roots]
    cfg["source_roots"] = [str(path) for path in roots]
    for project in cfg.get("projects", []):
        if project.get("path") and project.get("hostId", "local") in (None, "local"):
            project["path"] = str(absolute_path(project["path"], base, "projects[].path"))
    if "archive_dir" not in cfg:
        if require_archive:
            raise ValueError("archive_dir is required; run setup.py init to create a personal configuration")
        return cfg
    archive = absolute_path(cfg["archive_dir"], base, "archive_dir")
    export = absolute_path(cfg.get("agent_export_dir", archive / "agent"), base, "agent_export_dir")
    for source in roots:
        if archive.is_relative_to(source) or source.is_relative_to(archive):
            raise ValueError("archive_dir must not overlap Codex source_roots")
        if export.is_relative_to(source) or source.is_relative_to(export):
            raise ValueError("agent_export_dir must not overlap Codex source_roots")
    if export == archive or any(
        export.is_relative_to(archive / name) or (archive / name).is_relative_to(export)
        for name in MANAGED_DIRS
    ):
        raise ValueError("agent_export_dir must be a dedicated directory outside managed archive folders")
    cfg["archive_dir"], cfg["agent_export_dir"] = str(archive), str(export)
    # The current archive renderer uses Shanghai time. Reject misleading config
    # rather than silently presenting another timezone's label over UTC+08 data.
    cfg.setdefault("timezone", "Asia/Shanghai")
    if cfg["timezone"] != "Asia/Shanghai":
        raise ValueError("This version supports timezone Asia/Shanghai only")
    return cfg


def load_config(config_path=None, *, require_archive=True) -> dict:
    path = resolve_config_path(config_path)
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Configuration not found: {path}. Run setup.py init first.") from exc
    return normalize_config(config, path, require_archive=require_archive)
