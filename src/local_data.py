# -*- coding: utf-8 -*-
"""Inventory, export, redact, and explicitly delete app-managed local data."""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from .config import CACHE_DIR, LOCAL_CONFIG, PROJECT_ROOT

GENERATED_DIR = PROJECT_ROOT / "data"
DELETE_CONFIRM_TEXT = "删除本地数据"
_TOKEN_KEY = re.compile(r"token|authorization|secret|api[_-]?key", re.I)
_PERSONAL_KEY = re.compile(
    r"phone|mobile|telephone|address|nickname|contact|recipient|receiver|note|"
    r"order.?id|order.?no|order.?product|store.?code|store.?name|^store$|"
    r"product.?name|product.?code|coupon.?id|coupon.?code|^name$|^items?$|"
    r"favorite.?stores?|aliases|buy.?count",
    re.I,
)
_TOKEN_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:mcp[_-]?token|access[_-]?token|refresh[_-]?token|authorization|secret|api[_-]?key)\b"
    r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:bearer\s+)?[^\s,;&<>]+)"
)
_CACHE_FILES = {
    "ledger.jsonl", "order_receipts.jsonl", "price_baseline.json",
    "product_profile.json", "store_names.json", "processed_order_ids.json",
    "schedules.json", ".schedule-fire.lock",
}
_CACHE_DIRS = {"drafts", "meals_snapshot", "outbox"}


def _roots() -> list[tuple[str, Path]]:
    return [("cache", CACHE_DIR), ("config", LOCAL_CONFIG), ("generated", GENERATED_DIR)]


def _walk_files(root: Path) -> tuple[list[Path], int]:
    """Return regular files without following symlinks."""
    if root.is_symlink() or not root.exists():
        return [], int(root.is_symlink())
    if root.is_file():
        return [root], 0
    files: list[Path] = []
    skipped = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for name in dirs:
            path = current_path / name
            if path.is_symlink():
                skipped += 1
            else:
                kept_dirs.append(name)
        dirs[:] = sorted(kept_dirs)
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink():
                skipped += 1
            elif path.is_file():
                files.append(path)
    return files, skipped


def _cache_files(root: Path) -> tuple[list[Path], int, list[Path]]:
    """Return known cache files and report unrelated files without managing them."""
    if root.is_symlink() or not root.exists() or not root.is_dir():
        return [], int(root.is_symlink()), []
    files: list[Path] = []
    all_files, skipped = _walk_files(root)
    for name in sorted(_CACHE_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            files.append(path)
    for name in sorted(_CACHE_DIRS):
        path = root / name
        if not path.is_symlink() and path.is_dir():
            found, _ = _walk_files(path)
            files.extend(found)
    known = {os.path.normcase(str(p.resolve())) for p in files}
    other_files = [p for p in all_files if os.path.normcase(str(p.resolve())) not in known]
    return files, skipped, other_files


def status() -> dict:
    groups = []
    for name, root in _roots():
        if name == "cache":
            files, skipped, other_files = _cache_files(root)
        else:
            files, skipped = _walk_files(root)
            other_files = []
        all_files = files + other_files
        groups.append({
            "name": name,
            "path": str(root),
            "exists": root.exists(),
            "files": len(all_files),
            "bytes": sum(p.stat().st_size for p in all_files),
            "managedFiles": len(files),
            "managedBytes": sum(p.stat().st_size for p in files),
            "skippedSymlinks": skipped,
            "unmanagedFiles": len(other_files),
            "unmanagedBytes": sum(p.stat().st_size for p in other_files),
        })
    return {"groups": groups, "totalFiles": sum(g["files"] for g in groups),
            "totalBytes": sum(g["bytes"] for g in groups)}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _source_files(redact: bool = False) -> tuple[list[tuple[str, Path]], list[str], list[str], list[str]]:
    entries: list[tuple[str, Path]] = []
    excluded_groups: set[str] = set()
    excluded_files: set[str] = set()
    renamed_groups: set[str] = set()
    seen: set[str] = set()
    snapshot_index = 0
    for name, root in _roots():
        if redact and name == "generated":
            excluded_groups.add(name)
            continue
        if name == "cache":
            files, _, _ = _cache_files(root)
        else:
            files, _ = _walk_files(root)
        for path in files:
            resolved = path.resolve()
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            if name == "config":
                arcname = "project/config.local.json"
            else:
                rel = path.relative_to(root)
                arcname = f"{name}/{rel.as_posix()}"
            if redact and arcname in {
                "cache/product_profile.json", "cache/store_names.json",
                "cache/processed_order_ids.json",
            }:
                excluded_files.add(arcname)
                continue
            if redact and arcname.startswith("cache/meals_snapshot/"):
                snapshot_index += 1
                renamed_groups.add("cache/meals_snapshot")
                arcname = f"cache/meals_snapshot/snapshot-{snapshot_index:03}.json"
            entries.append((arcname, path))
    return (entries, sorted(excluded_groups), sorted(excluded_files),
            sorted(renamed_groups))


def _secret_values() -> list[str]:
    values = [os.environ.get("MCD_MCP_TOKEN", "")]
    try:
        raw = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = None

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if _TOKEN_KEY.search(str(key)) and isinstance(child, (str, int, float)):
                    values.append(str(child))
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(raw)
    return sorted({v for v in values if v and v != "YOUR_MCP_TOKEN"}, key=len, reverse=True)


def _sanitize_value(value, redact: bool, secrets: list[str]):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            key_text = str(key)
            if _TOKEN_KEY.search(key_text):
                continue
            if redact and _PERSONAL_KEY.search(key_text):
                result[key_text] = "[已脱敏]"
            else:
                result[key_text] = _sanitize_value(child, redact, secrets)
        return result
    if isinstance(value, list):
        return [_sanitize_value(child, redact, secrets) for child in value]
    if isinstance(value, str):
        return _sanitize_text(value, redact, secrets)
    return value


def _sanitize_text(text: str, redact: bool, secrets: list[str]) -> str:
    text = _TOKEN_ASSIGNMENT.sub(r"\1[已移除]", text)
    for secret in secrets:
        text = text.replace(secret, "[已移除]")
    if redact:
        text = re.sub(r"(?i)(手机号?|电话|mobile|phone|address|地址|收货人|联系人)\s*[:：=]\s*([^\s,，;；<>]+)",
                      r"\1: [已脱敏]", text)
    return text


def _export_bytes(path: Path, redact: bool, secrets: list[str]) -> bytes:
    if redact:
        if path.name in {"product_profile.json", "store_names.json"}:
            return b"{}\n"
        if path.name == "processed_order_ids.json":
            return b"[]\n"
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        if redact:
            raise ValueError(f"脱敏导出暂不支持二进制文件：{path.name}")
        raw = path.read_bytes()
        for secret in secrets:
            raw = raw.replace(secret.encode("utf-8"), b"[REDACTED]")
        return raw

    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _sanitize_text(text, redact, secrets).encode("utf-8")
        return (json.dumps(_sanitize_value(data, redact, secrets), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if suffix == ".jsonl":
        lines = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                lines.append(_sanitize_text(line, redact, secrets))
            else:
                lines.append(json.dumps(_sanitize_value(value, redact, secrets), ensure_ascii=False))
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return _sanitize_text(text, redact, secrets).encode("utf-8")


def export_archive(out_path: str | Path, redact: bool = False, overwrite: bool = False) -> dict:
    out = Path(out_path).expanduser().resolve()
    entries, excluded_groups, excluded_files, renamed_groups = _source_files(redact=redact)
    for _, source in entries:
        if out == source.resolve():
            raise ValueError("导出文件不能覆盖本地数据")
    for _, root in _roots():
        if root.is_dir() and _inside(out, root):
            raise ValueError("导出文件必须放在受管理的数据目录之外")
    if out.exists() and not overwrite:
        raise FileExistsError(f"导出文件已存在；如需覆盖请加 --force：{out}")

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    os.close(fd)
    secrets = _secret_values()
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = {"formatVersion": 1, "redacted": redact, "credentialFieldsRemoved": True,
                        "excludedGroups": excluded_groups, "excludedFiles": excluded_files,
                        "renamedGroups": renamed_groups, "files": []}
            for arcname, source in entries:
                payload = _export_bytes(source, redact, secrets)
                archive.writestr(arcname, payload)
                manifest["files"].append({"path": arcname, "bytes": len(payload)})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp_name, out)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return {"path": str(out), "files": len(entries), "redacted": redact,
            "bytes": out.stat().st_size}


def _delete_tree(root: Path) -> int:
    files, _ = _walk_files(root)
    deleted = 0
    for path in files:
        if _inside(path, root):
            path.unlink()
            deleted += 1
    if root.is_dir() and not root.is_symlink():
        for current, dirs, _ in os.walk(root, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                child = current_path / name
                if not child.is_symlink():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
        try:
            root.rmdir()
        except OSError:
            pass
    return deleted


def _delete_cache(root: Path) -> int:
    files, _, _ = _cache_files(root)
    deleted = 0
    for path in files:
        if _inside(path, root):
            path.unlink()
            deleted += 1
    for name in sorted(_CACHE_DIRS):
        directory = root / name
        if directory.is_dir() and not directory.is_symlink():
            for current, dirs, _ in os.walk(directory, topdown=False, followlinks=False):
                current_path = Path(current)
                for child_name in dirs:
                    child = current_path / child_name
                    if not child.is_symlink():
                        try:
                            child.rmdir()
                        except OSError:
                            pass
            try:
                directory.rmdir()
            except OSError:
                pass
    return deleted


def delete(include_config: bool = False, include_generated: bool = False) -> dict:
    deleted = {"cache": _delete_cache(CACHE_DIR)}
    if include_config:
        deleted["config"] = _delete_tree(LOCAL_CONFIG)
    if include_generated:
        deleted["generated"] = _delete_tree(GENERATED_DIR)
    return {"deletedFiles": sum(deleted.values()), "byGroup": deleted}
