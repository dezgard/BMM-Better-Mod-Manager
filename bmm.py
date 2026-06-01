#!/usr/bin/env python3
"""BMM - Better Mod Manager prototype for Ostranauts mods.

This is intentionally dependency-free. It is a first working CLI prototype for:
- reading a shared mod index
- checking GitHub releases
- installing BepInEx plugin zip files
- installing Ostranauts JSON data mod zip files
- disabling/enabling BMM-managed DLLs
- uninstalling BMM-managed files with backups
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


APP_NAME = "Better Mod Manager"
INDEX_SCHEMA = "bmm-index-v1"
NEST_SCHEMA = "bmm-nest-v1"
STATE_SCHEMA = "bmm-state-v1"
DEFAULT_INDEX_NAME = "bmm-index.example.json"
NEST_FILE_NAME = "bmm.nest.json"
MOD_INDEX_DIR_NAME = "Mod_index"
MOD_INDEX_BACKUP_FILE_NAME = "mod-index.backup.json"
GITHUB_API_ROOT = "https://api.github.com"
INSTALL_ROOTS = {
    "bepinex_plugins": Path("BepInEx") / "plugins",
    "bepinex_patchers": Path("BepInEx") / "patchers",
    "bepinex_config": Path("BepInEx") / "config",
    "data_mods": Path("Ostranauts_Data") / "Mods",
}
INSTALL_ROOT_ALIASES = {
    "bepinex_plugins": "bepinex_plugins",
    "bepinex/plugins": "bepinex_plugins",
    "plugins": "bepinex_plugins",
    "bepinex_patchers": "bepinex_patchers",
    "bepinex/patchers": "bepinex_patchers",
    "patchers": "bepinex_patchers",
    "bepinex_config": "bepinex_config",
    "bepinex/config": "bepinex_config",
    "config": "bepinex_config",
    "data_mods": "data_mods",
    "data/mods": "data_mods",
    "ostranauts_data/mods": "data_mods",
    "ostranauts_data\\mods": "data_mods",
    "mods": "data_mods",
}
RELATIONSHIP_KEYS = ("depends", "recommends", "suggests", "conflicts", "provides")
DATA_MOD_ROOT = "data_mods"
LOADING_ORDER_FILE_NAME = "loading_order.json"
OSTRANAUTS_SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / ".." / "LocalLow" / "Blue Bottle Games" / "Ostranauts" / "settings.json"


class BmmError(RuntimeError):
    pass


@dataclass
class Runtime:
    data_dir: Path
    config_path: Path
    state_path: Path
    cache_dir: Path
    backup_dir: Path


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def default_data_dir() -> Path:
    if os.environ.get("BMM_HOME"):
        return Path(os.environ["BMM_HOME"]).expanduser()
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / MOD_INDEX_DIR_NAME


def make_runtime(data_dir_arg: str | None) -> Runtime:
    data_dir = Path(data_dir_arg).expanduser() if data_dir_arg else default_data_dir()
    return Runtime(
        data_dir=data_dir,
        config_path=data_dir / "settings.json",
        state_path=data_dir / "installed.json",
        cache_dir=data_dir / "cache",
        backup_dir=data_dir / "backups",
    )


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BmmError(f"Invalid JSON in {path}: {exc}") from exc


def read_json_backup_source(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "invalid_json": True,
            "error": str(exc),
            "text": text,
        }


def update_mod_index_backup(path: Path) -> None:
    if path.name == MOD_INDEX_BACKUP_FILE_NAME or path.parent.name.lower() != MOD_INDEX_DIR_NAME.lower() or not path.exists():
        return
    backup_path = path.parent / MOD_INDEX_BACKUP_FILE_NAME
    if backup_path.exists():
        backup_raw = read_json(backup_path, default={})
        backup = backup_raw if isinstance(backup_raw, dict) else {}
    else:
        backup = {}
    files = backup.get("files")
    if not isinstance(files, dict):
        files = {}
    files[path.name] = {
        "backed_up_utc": stamp(),
        "content": read_json_backup_source(path),
    }
    backup["schema"] = "bmm-mod-index-backup-v1"
    backup["updated_utc"] = stamp()
    backup["files"] = files
    write_json_atomic(backup_path, backup)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_json_with_backup(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.name.lower() == MOD_INDEX_DIR_NAME.lower():
        update_mod_index_backup(path)
    elif path.exists():
        backup = path.with_name(path.name + ".bak-" + stamp())
        shutil.copy2(path, backup)
    write_json_atomic(path, data)


def load_config(rt: Runtime) -> dict[str, Any]:
    config = read_json(rt.config_path, default={})
    if not isinstance(config, dict):
        raise BmmError(f"Config must be a JSON object: {rt.config_path}")
    return config


def load_state(rt: Runtime) -> dict[str, Any]:
    state = read_json(rt.state_path, default=None)
    if state is None:
        return {"schema": STATE_SCHEMA, "installed": {}, "profiles": {}}
    if not isinstance(state, dict):
        raise BmmError(f"State must be a JSON object: {rt.state_path}")
    state.setdefault("schema", STATE_SCHEMA)
    state.setdefault("installed", {})
    state.setdefault("profiles", {})
    return state


def save_state(rt: Runtime, state: dict[str, Any]) -> None:
    state["schema"] = STATE_SCHEMA
    write_json_with_backup(rt.state_path, state)


def http_get(url: str, *, accept: str = "application/json") -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "BMM-Ostranauts-Prototype",
    }
    if "api.github.com" in url:
        headers["X-GitHub-Api-Version"] = os.environ.get("BMM_GITHUB_API_VERSION", "2022-11-28")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("BMM_GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    context = None
    if os.environ.get("BMM_INSECURE_SSL") == "1":
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=context) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise BmmError(f"HTTP {exc.code} for {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise BmmError(f"Could not reach {url}: {exc}") from exc


def http_get_json(url: str) -> Any:
    return json.loads(http_get(url).decode("utf-8"))


def strip_json_comments(text: str) -> str:
    result: list[str] = []
    i = 0
    in_string = False
    escape = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                result.append("\n" if text[i] in "\r\n" else " ")
                i += 1
            i += 2 if i + 1 < len(text) else 0
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def loads_json_with_comments(text: str, source: str) -> Any:
    try:
        return json.loads(strip_json_comments(text))
    except json.JSONDecodeError as exc:
        raise BmmError(f"Invalid JSON in {source}: {exc}") from exc


def is_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def load_index(index_ref: str | None, config: dict[str, Any]) -> dict[str, Any]:
    if index_ref is None:
        indexes = config.get("indexes") or []
        if indexes:
            index_ref = indexes[0]
        else:
            index_ref = str(Path(__file__).with_name(DEFAULT_INDEX_NAME))

    if is_url(index_ref):
        index = http_get_json(index_ref)
    else:
        index = read_json(Path(index_ref).expanduser())

    if not isinstance(index, dict):
        raise BmmError("Index must be a JSON object.")
    return index


def normalize_install_root(value: Any) -> str:
    if value is None or value == "":
        return "bepinex_plugins"
    key = str(value).replace("\\", "/").strip().lower()
    if key in INSTALL_ROOT_ALIASES:
        return INSTALL_ROOT_ALIASES[key]
    raise BmmError(f"Unsupported install root: {value!r}")


def is_safe_relative_path(value: Any, *, allow_directory_marker: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
        return False
    if allow_directory_marker and text.endswith("/"):
        text = text[:-1]
    parts = PurePosixPath(text).parts
    return all(part not in ("", ".", "..", "/") for part in parts)


def validate_relationship_item(item: Any, *, allow_any_of: bool = True) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if not isinstance(item, dict):
        return False
    if isinstance(item.get("id"), str) and item["id"].strip():
        return True
    if allow_any_of and isinstance(item.get("any_of"), list) and item["any_of"]:
        return all(validate_relationship_item(child, allow_any_of=False) for child in item["any_of"])
    return False


def relationship_targets(item: Any) -> list[str]:
    if isinstance(item, str):
        return [item]
    if not isinstance(item, dict):
        return []
    if isinstance(item.get("id"), str):
        return [item["id"]]
    if isinstance(item.get("any_of"), list):
        targets: list[str] = []
        for child in item["any_of"]:
            targets.extend(relationship_targets(child))
        return targets
    return []


def relationship_label(item: Any) -> str:
    targets = relationship_targets(item)
    if not targets:
        return str(item)
    if isinstance(item, dict) and "any_of" in item:
        return "one of " + ", ".join(targets)
    return targets[0]


def merged_relationships(mod: dict[str, Any], version: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    merged: dict[str, list[Any]] = {key: [] for key in RELATIONSHIP_KEYS}
    for source in (mod, version or {}):
        rel = source.get("relationships") if isinstance(source, dict) else None
        if isinstance(rel, dict):
            for key in RELATIONSHIP_KEYS:
                values = rel.get(key)
                if isinstance(values, list):
                    merged[key].extend(values)
        legacy_deps = source.get("dependencies") if isinstance(source, dict) else None
        if isinstance(legacy_deps, list):
            merged["depends"].extend(legacy_deps)
        top_level_provides = source.get("provides") if isinstance(source, dict) else None
        if isinstance(top_level_provides, list):
            merged["provides"].extend(top_level_provides)
    return merged


def validate_relationships(container: dict[str, Any], prefix: str, errors: list[str]) -> None:
    rel = container.get("relationships")
    if rel is not None and not isinstance(rel, dict):
        errors.append(f"{prefix}.relationships must be an object")
        return
    if isinstance(rel, dict):
        for key in rel:
            if key not in RELATIONSHIP_KEYS:
                errors.append(f"{prefix}.relationships.{key} is not a known relationship type")
        for key in RELATIONSHIP_KEYS:
            values = rel.get(key)
            if values is None:
                continue
            if not isinstance(values, list):
                errors.append(f"{prefix}.relationships.{key} must be an array")
                continue
            for ri, item in enumerate(values):
                if key == "provides":
                    if not isinstance(item, str) or not item.strip():
                        errors.append(f"{prefix}.relationships.provides[{ri}] must be a string")
                elif not validate_relationship_item(item):
                    errors.append(f"{prefix}.relationships.{key}[{ri}] must be a string, id object, or any_of object")
    legacy_deps = container.get("dependencies")
    if legacy_deps is not None and not isinstance(legacy_deps, list):
        errors.append(f"{prefix}.dependencies must be an array")
    provides = container.get("provides")
    if provides is not None:
        if not isinstance(provides, list) or not all(isinstance(item, str) and item.strip() for item in provides):
            errors.append(f"{prefix}.provides must be an array of strings")


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_mod_type(value: Any, *, has_bepinex: bool = False, has_data: bool = False) -> str:
    raw = str(value or "").replace("-", "_").strip().lower()
    aliases = {
        "": "",
        "bep": "bepinex",
        "bepinex": "bepinex",
        "plugin": "bepinex",
        "bepinex_plugin": "bepinex",
        "data": "data",
        "data_mod": "data",
        "hybrid": "hybrid",
        "both": "hybrid",
        "bepinex_data": "hybrid",
        "data_bepinex": "hybrid",
    }
    if raw not in aliases:
        raise BmmError(f"Unsupported mod type: {value!r}")
    normalized = aliases[raw]
    if normalized:
        return normalized
    if has_bepinex and has_data:
        return "hybrid"
    if has_data:
        return "data"
    return "bepinex"


def normalize_nest_install_entries(raw_install: Any, default_root: str = "bepinex_plugins") -> list[dict[str, str]]:
    if raw_install is None:
        return []
    if isinstance(raw_install, list):
        raw_entries = raw_install
    elif isinstance(raw_install, dict):
        raw_entries = raw_install.get("entries")
        if raw_entries is None and "source" in raw_install and "target" in raw_install:
            raw_entries = [raw_install]
        if raw_entries is None:
            raw_entries = []
        default_root = str(raw_install.get("root") or raw_install.get("strategy") or default_root)
    else:
        raise BmmError("nest install must be an array or object")

    entries: list[dict[str, str]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise BmmError("nest install entries must be objects")
        source = str(entry.get("source") or "").replace("\\", "/").strip()
        target = str(entry.get("target") or "").replace("\\", "/").strip()
        if not source or not target:
            raise BmmError("nest install entries need source and target")
        root = normalize_install_root(entry.get("root") or default_root)
        entries.append({"source": source, "target": target, "root": root})
    return entries


def infer_nest_install_entries(nest_mod: dict[str, Any], mod_type: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    bepinex = nest_mod.get("bepinex") if isinstance(nest_mod.get("bepinex"), dict) else {}
    data = nest_mod.get("data") if isinstance(nest_mod.get("data"), dict) else {}
    if mod_type in ("bepinex", "hybrid"):
        dll = str(bepinex.get("dll") or "").replace("\\", "/").strip()
        if dll:
            source = str(bepinex.get("source") or dll).replace("\\", "/").strip()
            target = str(bepinex.get("target") or PurePosixPath(dll).name).replace("\\", "/").strip()
            root = normalize_install_root(bepinex.get("root") or "bepinex_plugins")
            entries.append({"source": source, "target": target, "root": root})
    if mod_type in ("data", "hybrid"):
        folder = str(data.get("folder") or nest_mod.get("data_mod_folder") or "").replace("\\", "/").strip("/")
        if folder:
            source = str(data.get("source") or f"{folder}/").replace("\\", "/").strip()
            target = str(data.get("target") or f"{folder}/").replace("\\", "/").strip()
            if not source.endswith("/"):
                source += "/"
            if not target.endswith("/"):
                target += "/"
            entries.append({"source": source, "target": target, "root": DATA_MOD_ROOT})
    return entries


def github_repo_file_json(repo: str, path: str, ref: str | None = None) -> Any | None:
    query = f"?ref={urllib.parse.quote(ref)}" if ref else ""
    url = f"{GITHUB_API_ROOT}/repos/{repo}/contents/{urllib.parse.quote(path)}{query}"
    try:
        payload = http_get_json(url)
    except BmmError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    if not isinstance(payload, dict):
        raise BmmError(f"Unexpected GitHub contents response for {repo}/{path}")
    encoding = str(payload.get("encoding") or "").lower()
    content = str(payload.get("content") or "")
    if encoding != "base64" or not content:
        raise BmmError(f"GitHub file is not base64 content: {repo}/{path}")
    try:
        raw = base64.b64decode(content).decode("utf-8-sig")
        return loads_json_with_comments(raw, f"{repo}/{path}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise BmmError(f"Invalid JSON in {repo}/{path}: {exc}") from exc


def github_fetch_nest(repo: str, ref: str | None = None) -> dict[str, Any] | None:
    raw = github_repo_file_json(repo, NEST_FILE_NAME, ref)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BmmError(f"{NEST_FILE_NAME} must be a JSON object")
    if raw.get("schema") != NEST_SCHEMA:
        raise BmmError(f"{NEST_FILE_NAME} schema must be {NEST_SCHEMA!r}")
    return raw


def nest_mod_to_index_mod(
    nest_mod: dict[str, Any],
    *,
    repo: str,
    repo_data: dict[str, Any] | None = None,
    release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo_data = repo_data or {}
    owner, repo_name = repo.split("/", 1)
    bepinex = nest_mod.get("bepinex") if isinstance(nest_mod.get("bepinex"), dict) else {}
    data = nest_mod.get("data") if isinstance(nest_mod.get("data"), dict) else {}
    mod_type = normalize_mod_type(nest_mod.get("type"), has_bepinex=bool(bepinex), has_data=bool(data))
    mod_id = str(nest_mod.get("id") or "").strip()
    if not mod_id:
        raise BmmError("Every nest mod needs an id")
    name = str(nest_mod.get("name") or mod_id).strip()
    version_label = str(nest_mod.get("version") or (release or {}).get("tag_name") or "github-latest").lstrip("v")
    asset_pattern = str(nest_mod.get("asset_pattern") or "").strip()
    release_block = nest_mod.get("release") if isinstance(nest_mod.get("release"), dict) else {}
    if not asset_pattern:
        asset_pattern = str(release_block.get("asset_pattern") or "").strip()
    include_prereleases = bool(release_block.get("include_prereleases", False))

    install_entries = normalize_nest_install_entries(nest_mod.get("install"))
    if not install_entries:
        install_entries = infer_nest_install_entries(nest_mod, mod_type)
    install_block = {"entries": install_entries} if install_entries else {}

    plugin: dict[str, Any] | None = None
    if mod_type in ("bepinex", "hybrid"):
        plugin = {
            "guid": str(bepinex.get("plugin_guid") or bepinex.get("guid") or mod_id).strip(),
            "name": str(bepinex.get("name") or name).strip(),
            "dll": str(bepinex.get("dll") or "").strip(),
        }

    rel = nest_mod.get("relationships") if isinstance(nest_mod.get("relationships"), dict) else {}
    relationships = {key: list(rel.get(key) or []) if isinstance(rel.get(key), list) else [] for key in RELATIONSHIP_KEYS}
    for provided in string_list(nest_mod.get("provides")):
        relationships["provides"].append(provided)
    if plugin and plugin["guid"] and plugin["guid"] not in relationships["provides"]:
        relationships["provides"].append(plugin["guid"])
    if mod_id not in relationships["provides"]:
        relationships["provides"].append(mod_id)

    website = str(nest_mod.get("website") or repo_data.get("html_url") or f"https://github.com/{repo}").strip()
    notes = string_list(nest_mod.get("notes"))
    notes.insert(0, f"BMM nest file: {NEST_FILE_NAME}")
    notes.insert(1, f"Package type: {mod_type}")
    if asset_pattern:
        notes.append(f"Release asset pattern: {asset_pattern}")

    index_mod: dict[str, Any] = {
        "id": mod_id,
        "type": mod_type,
        "name": name,
        "summary": str(nest_mod.get("summary") or repo_data.get("description") or f"GitHub mod from {repo}").strip(),
        "authors": string_list(nest_mod.get("authors")) or [owner],
        "categories": string_list(nest_mod.get("categories")) or ["github", mod_type],
        "website": website,
        "notes": notes,
        "nest": {
            "schema": NEST_SCHEMA,
            "file": NEST_FILE_NAME,
            "id": mod_id,
        },
        "relationships": relationships,
        "release": {
            "provider": "github",
            "repo": repo,
            "include_prereleases": include_prereleases,
        },
        "versions": [],
    }
    if plugin:
        index_mod["plugin"] = plugin
    data_folder = str(data.get("folder") or nest_mod.get("data_mod_folder") or "").strip()
    if data_folder:
        index_mod["data_mod_folder"] = data_folder
    if asset_pattern:
        index_mod["release"]["asset_pattern"] = asset_pattern
    if install_block:
        index_mod["install"] = install_block

    bepinex_version = nest_mod.get("bepinex_version")
    if not bepinex_version and isinstance(nest_mod.get("bepinex"), str):
        bepinex_version = nest_mod.get("bepinex")

    version_entry: dict[str, Any] = {
        "version": version_label,
        "game_versions": string_list(nest_mod.get("game_versions")),
        "bepinex": str(bepinex_version or "").strip(),
        "relationships": {key: list(relationships.get(key, [])) if key != "provides" else [] for key in RELATIONSHIP_KEYS if key != "provides"},
    }
    if install_block:
        version_entry["install"] = install_block
    if release:
        try:
            asset = find_release_asset(release, asset_pattern or None)
            download: dict[str, Any] = {
                "type": "url",
                "url": str(asset["browser_download_url"]),
                "source_label": f"{repo} {release.get('tag_name')} {asset.get('name')}",
            }
            digest = str(asset.get("digest") or "")
            if digest.startswith("sha256:"):
                download["sha256"] = digest.replace("sha256:", "", 1)
            version_entry["download"] = download
        except BmmError:
            pass
    index_mod["versions"] = [version_entry]
    return index_mod


def nest_to_index_mods(
    nest: dict[str, Any],
    *,
    repo: str,
    repo_data: dict[str, Any] | None = None,
    release: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    mods = nest.get("mods")
    if not isinstance(mods, list) or not mods:
        raise BmmError(f"{NEST_FILE_NAME} needs a non-empty mods array")
    result = []
    seen: set[str] = set()
    for i, nest_mod in enumerate(mods):
        if not isinstance(nest_mod, dict):
            raise BmmError(f"{NEST_FILE_NAME} mods[{i}] must be an object")
        index_mod = nest_mod_to_index_mod(nest_mod, repo=repo, repo_data=repo_data, release=release)
        mod_id = str(index_mod.get("id") or "")
        if mod_id in seen:
            raise BmmError(f"{NEST_FILE_NAME} duplicate mod id: {mod_id}")
        seen.add(mod_id)
        result.append(index_mod)
    errors = validate_index({"schema": INDEX_SCHEMA, "game": {"id": "ostranauts"}, "mods": result})
    if errors:
        raise BmmError(f"{NEST_FILE_NAME} generated invalid BMM entries: " + "; ".join(errors[:6]))
    return result


def validate_install_block(install: Any, prefix: str, errors: list[str]) -> None:
    if install is None:
        return
    if not isinstance(install, dict):
        errors.append(f"{prefix}.install must be an object")
        return
    try:
        default_root = normalize_install_root(install.get("root") or install.get("strategy"))
    except BmmError as exc:
        errors.append(f"{prefix}.install.root {exc}")
        default_root = "bepinex_plugins"
    entries = install.get("entries")
    if entries is None:
        return
    if not isinstance(entries, list) or not entries:
        errors.append(f"{prefix}.install.entries must be a non-empty array")
        return
    for ei, entry in enumerate(entries):
        eprefix = f"{prefix}.install.entries[{ei}]"
        if not isinstance(entry, dict):
            errors.append(f"{eprefix} must be an object")
            continue
        if not is_safe_relative_path(entry.get("source"), allow_directory_marker=True):
            errors.append(f"{eprefix}.source must be a safe relative archive path")
        if not is_safe_relative_path(entry.get("target"), allow_directory_marker=True):
            errors.append(f"{eprefix}.target must be a safe relative target path")
        try:
            normalize_install_root(entry.get("root") or default_root)
        except BmmError as exc:
            errors.append(f"{eprefix}.root {exc}")


def validate_index(index: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if index.get("schema") != INDEX_SCHEMA:
        errors.append(f"schema must be {INDEX_SCHEMA!r}")
    mods = index.get("mods")
    if not isinstance(mods, list):
        errors.append("mods must be an array")
        return errors

    seen: set[str] = set()
    seen_guids: dict[str, str] = {}
    mod_id_re = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
    for i, mod in enumerate(mods):
        prefix = f"mods[{i}]"
        if not isinstance(mod, dict):
            errors.append(f"{prefix} must be an object")
            continue
        mod_id = mod.get("id")
        if not isinstance(mod_id, str) or not mod_id_re.match(mod_id):
            errors.append(f"{prefix}.id must be lowercase letters, numbers, dots, dashes, or underscores")
        elif mod_id in seen:
            errors.append(f"{prefix}.id duplicate: {mod_id}")
        else:
            seen.add(mod_id)
        for field in ("name", "summary"):
            if not isinstance(mod.get(field), str) or not mod[field].strip():
                errors.append(f"{prefix}.{field} is required")
        if not isinstance(mod.get("authors"), list) or not mod["authors"]:
            errors.append(f"{prefix}.authors must be a non-empty array")
        mod_type = str(mod.get("type") or "bepinex").strip().lower()
        if mod_type not in ("bepinex", "plugin", "data", "data_mod", "hybrid"):
            errors.append(f"{prefix}.type is unsupported: {mod.get('type')}")
        plugin = mod.get("plugin")
        if mod_type not in ("data", "data_mod"):
            if not isinstance(plugin, dict) or not isinstance(plugin.get("guid"), str):
                errors.append(f"{prefix}.plugin.guid is required")
            else:
                guid = plugin["guid"].strip()
                if not guid:
                    errors.append(f"{prefix}.plugin.guid is required")
                elif guid in seen_guids:
                    errors.append(f"{prefix}.plugin.guid duplicate with {seen_guids[guid]}: {guid}")
                else:
                    seen_guids[guid] = str(mod_id)
        release = mod.get("release")
        versions = mod.get("versions")
        if release is not None and not isinstance(release, dict):
            errors.append(f"{prefix}.release must be an object")
        elif isinstance(release, dict):
            if release.get("provider") == "github":
                repo = release.get("repo")
                if not isinstance(repo, str) or "/" not in repo:
                    errors.append(f"{prefix}.release.repo must be owner/name for GitHub releases")
            elif release.get("provider") is not None:
                errors.append(f"{prefix}.release.provider is unsupported: {release.get('provider')}")
        validate_relationships(mod, prefix, errors)
        validate_install_block(mod.get("install"), prefix, errors)
        if versions is not None:
            if not isinstance(versions, list):
                errors.append(f"{prefix}.versions must be an array")
            else:
                seen_versions: set[str] = set()
                for vi, version in enumerate(versions):
                    if not isinstance(version, dict):
                        errors.append(f"{prefix}.versions[{vi}] must be an object")
                        continue
                    version_prefix = f"{prefix}.versions[{vi}]"
                    version_value = version.get("version")
                    if not isinstance(version_value, str):
                        errors.append(f"{version_prefix}.version is required")
                    elif version_value in seen_versions:
                        errors.append(f"{version_prefix}.version duplicate: {version_value}")
                    else:
                        seen_versions.add(version_value)
                    download = version.get("download")
                    if download is not None and not isinstance(download, dict):
                        errors.append(f"{version_prefix}.download must be an object")
                    elif isinstance(download, dict):
                        dtype = download.get("type")
                        if dtype not in ("local", "url"):
                            errors.append(f"{version_prefix}.download.type must be local or url")
                        elif dtype == "local" and not isinstance(download.get("path"), str):
                            errors.append(f"{version_prefix}.download.path is required for local downloads")
                        elif dtype == "url" and not isinstance(download.get("url"), str):
                            errors.append(f"{version_prefix}.download.url is required for url downloads")
                    validate_relationships(version, version_prefix, errors)
                    validate_install_block(version.get("install"), version_prefix, errors)
        if release is None and not versions:
            errors.append(f"{prefix} needs either release or versions")
    return errors


def get_mods(index: dict[str, Any]) -> list[dict[str, Any]]:
    mods = index.get("mods") or []
    return [m for m in mods if isinstance(m, dict)]


def find_mod(index: dict[str, Any], mod_id: str) -> dict[str, Any]:
    for mod in get_mods(index):
        if mod.get("id") == mod_id:
            return mod
    raise BmmError(f"Mod not found in index: {mod_id}")


def version_key(version: str) -> tuple[Any, ...]:
    clean = version.strip().lstrip("vV")
    parts: list[Any] = []
    for part in re.split(r"[.+_-]", clean):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            match = re.match(r"^(\d+)([A-Za-z].*)$", part)
            if match:
                parts.append((0, int(match.group(1))))
                parts.append((1, match.group(2).lower()))
            elif part:
                parts.append((1, part.lower()))
    return tuple(parts)


def latest_declared_version(mod: dict[str, Any]) -> dict[str, Any] | None:
    versions = mod.get("versions") or []
    if not versions:
        return None
    return max(versions, key=lambda item: version_key(str(item.get("version", "0"))))


def installed_capabilities(
    index: dict[str, Any],
    installed: dict[str, Any],
    *,
    exclude_mod_id: str | None = None,
) -> dict[str, str]:
    capabilities: dict[str, str] = {}
    indexed = {str(mod.get("id")): mod for mod in get_mods(index)}
    for mod_id, record in installed.items():
        if mod_id == exclude_mod_id:
            continue
        capabilities[str(mod_id)] = str(mod_id)
        for provided in record.get("provides", []) if isinstance(record, dict) else []:
            if isinstance(provided, str) and provided.strip():
                capabilities[provided] = str(mod_id)
        mod = indexed.get(str(mod_id))
        if mod:
            rel = merged_relationships(mod, latest_declared_version(mod))
            for provided in rel["provides"]:
                if isinstance(provided, str) and provided.strip():
                    capabilities[provided] = str(mod_id)
    return capabilities


def ensure_relationships_ok(
    mod: dict[str, Any],
    version: dict[str, Any] | None,
    index: dict[str, Any],
    state: dict[str, Any],
) -> None:
    rel = merged_relationships(mod, version)
    capabilities = installed_capabilities(index, state.get("installed", {}), exclude_mod_id=str(mod["id"]))
    missing = []
    for dep in rel["depends"]:
        targets = relationship_targets(dep)
        if targets and not any(target in capabilities for target in targets):
            missing.append(relationship_label(dep))
    conflicts = []
    for conflict in rel["conflicts"]:
        for target in relationship_targets(conflict):
            if target in capabilities:
                conflicts.append(f"{relationship_label(conflict)} provided by {capabilities[target]}")
                break
    if missing or conflicts:
        lines = []
        if missing:
            lines.append("missing dependencies: " + ", ".join(missing))
        if conflicts:
            lines.append("conflicts: " + ", ".join(conflicts))
        raise BmmError(f"Cannot install {mod['id']}: " + "; ".join(lines))


def github_latest_release(repo: str, include_prereleases: bool = False) -> dict[str, Any]:
    if "/" not in repo:
        raise BmmError(f"GitHub repo must be owner/name: {repo}")
    if include_prereleases:
        releases = http_get_json(f"{GITHUB_API_ROOT}/repos/{repo}/releases")
        if not isinstance(releases, list):
            raise BmmError(f"Unexpected GitHub response for {repo}")
        for release in releases:
            if not release.get("draft"):
                return release
        raise BmmError(f"No published GitHub releases found for {repo}")
    return http_get_json(f"{GITHUB_API_ROOT}/repos/{repo}/releases/latest")


def find_release_asset(release: dict[str, Any], pattern: str | None) -> dict[str, Any]:
    assets = release.get("assets") or []
    if not assets:
        raise BmmError(f"Release {release.get('tag_name')} has no assets.")
    if not pattern:
        zip_assets = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
        if len(zip_assets) == 1:
            return zip_assets[0]
        raise BmmError("Release asset pattern is required when there is not exactly one zip asset.")
    matches = [a for a in assets if fnmatch.fnmatch(str(a.get("name", "")), pattern)]
    if not matches:
        names = ", ".join(str(a.get("name", "")) for a in assets)
        raise BmmError(f"No release asset matched {pattern!r}. Assets: {names}")
    if len(matches) > 1:
        names = ", ".join(str(a.get("name", "")) for a in matches)
        raise BmmError(f"Release asset pattern {pattern!r} matched multiple assets: {names}")
    return matches[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_to_cache(url: str, cache_dir: Path, expected_sha256: str | None = None) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urllib.parse.urlparse(url).path).name or "download.zip"
    target = cache_dir / name
    data = http_get(url, accept="application/octet-stream")
    target.write_bytes(data)
    if expected_sha256:
        actual = sha256_file(target)
        if actual.lower() != expected_sha256.lower():
            raise BmmError(f"SHA256 mismatch for {target}: expected {expected_sha256}, got {actual}")
    return target


def game_dir_from_config(config: dict[str, Any], override: str | None = None) -> Path:
    raw = override if override and str(override).strip() else config.get("game_dir")
    raw_text = str(raw or "").strip()
    if not raw_text:
        raise BmmError("Game folder is not configured. Select the Ostranauts game folder first.")
    return Path(raw_text).expanduser()


def ensure_game_dir(game_dir: Path) -> Path:
    if not game_dir.exists():
        raise BmmError(f"Game folder does not exist: {game_dir}")
    return game_dir


def install_root_path(game_dir: Path, root_name: str) -> Path:
    if root_name not in INSTALL_ROOTS:
        raise BmmError(f"Unsupported install root: {root_name}")
    return game_dir / INSTALL_ROOTS[root_name]


def ensure_bepinex_dir(game_dir: Path) -> Path:
    ensure_game_dir(game_dir)
    bepinex = game_dir / "BepInEx"
    if not bepinex.exists():
        raise BmmError(f"BepInEx folder does not exist: {bepinex}")
    return bepinex


def plugins_dir(game_dir: Path) -> Path:
    return install_root_path(game_dir, "bepinex_plugins")


def ensure_plugins_dir(game_dir: Path) -> Path:
    root = plugins_dir(game_dir)
    if not root.exists():
        raise BmmError(f"BepInEx plugins folder does not exist: {root}")
    return root


def data_mods_dir(game_dir: Path) -> Path:
    return install_root_path(game_dir, DATA_MOD_ROOT)


def ostranauts_settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).parent / "LocalLow" / "Blue Bottle Games" / "Ostranauts" / "settings.json"
    return OSTRANAUTS_SETTINGS_PATH


def normalize_user_path(value: str) -> Path:
    return Path(value.replace("/", os.sep)).expanduser()


def configured_mods_path() -> Path | None:
    settings_path = ostranauts_settings_path()
    raw = read_json(settings_path, default=None)
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("strPathMods") or "").strip()
    if not value:
        return None
    path = normalize_user_path(value)
    if path.name.lower() == LOADING_ORDER_FILE_NAME:
        return path.parent
    return path


def configured_loading_order_path() -> Path | None:
    mods_path = configured_mods_path()
    if not mods_path:
        return None
    if mods_path.name.lower() == LOADING_ORDER_FILE_NAME:
        return mods_path
    return mods_path / LOADING_ORDER_FILE_NAME


def loading_order_paths(game_dir: Path) -> tuple[Path, Path]:
    data_path = game_dir / "Ostranauts_Data" / LOADING_ORDER_FILE_NAME
    mods_path = data_mods_dir(game_dir) / LOADING_ORDER_FILE_NAME
    return data_path, mods_path


def loading_order_path(game_dir: Path) -> Path:
    configured = configured_loading_order_path()
    if configured:
        expected_mods = data_mods_dir(game_dir).resolve()
        if configured.parent.resolve() == expected_mods:
            return configured
    data_path, _mods_path = loading_order_paths(game_dir)
    return data_path


def legacy_loading_order_path(game_dir: Path) -> Path:
    _data_path, mods_path = loading_order_paths(game_dir)
    return mods_path


def display_game_path(game_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(game_dir)).replace("\\", "/")
    except ValueError:
        return str(path)


def loading_order_warnings(game_dir: Path) -> list[str]:
    data_path, mods_path = loading_order_paths(game_dir)
    configured = configured_loading_order_path()
    expected_mods = data_mods_dir(game_dir).resolve()
    warnings = []
    if configured:
        configured_mods = configured.parent.resolve()
        configured_label = str(configured)
        expected_label = str(expected_mods)
        if configured_mods != expected_mods:
            warnings.append(f"In-game mod folder differs from selected game folder. Ostranauts setting uses {configured_label}; selected game folder expects {expected_label}.")
        elif configured.resolve() != data_path.resolve():
            warnings.append(f"Ostranauts setting uses {display_game_path(game_dir, configured)}. BMM will use that configured load-order file.")
    if not mods_path.exists():
        return warnings
    data_label = display_game_path(game_dir, data_path)
    mods_label = display_game_path(game_dir, mods_path)
    if data_path.exists():
        active = loading_order_path(game_dir).resolve()
        ignored = data_label if active == mods_path.resolve() else mods_label
        warnings.append(f"Duplicate data load order found. BMM uses {display_game_path(game_dir, loading_order_path(game_dir))}; {ignored} is ignored.")
        return warnings
    if not configured:
        warnings.append(f"Legacy data load order found at {mods_label}. BMM will migrate entries to {data_label} on the next data-mod change.")
    return warnings


def default_loading_order() -> list[dict[str, Any]]:
    return [
        {
            "strName": "Mod Loading Order",
            "strNotes": "Controls the order mods are loaded. 'core' refers to base game data.",
            "aLoadOrder": ["core"],
            "aIgnorePatterns": [],
        }
    ]


def normalize_loading_order(raw: Any, path: Path) -> list[dict[str, Any]]:
    if raw is None:
        return default_loading_order()
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        raise BmmError(f"Invalid Ostranauts loading order file: {path}")
    item = raw[0]
    if not isinstance(item.get("aLoadOrder"), list):
        item["aLoadOrder"] = ["core"]
    if not isinstance(item.get("aIgnorePatterns"), list):
        item["aIgnorePatterns"] = []
    return raw


def load_loading_order(game_dir: Path) -> tuple[Path, list[dict[str, Any]]]:
    path = loading_order_path(game_dir)
    raw = read_json(path, default=None)
    return path, normalize_loading_order(raw, path)


def load_legacy_loading_order(game_dir: Path) -> tuple[Path, list[dict[str, Any]]] | None:
    path = legacy_loading_order_path(game_dir)
    if not path.exists():
        return None
    raw = read_json(path, default=None)
    return path, normalize_loading_order(raw, path)


def loading_order_values(order: list[dict[str, Any]], key: str) -> list[str]:
    if not order or not isinstance(order[0], dict):
        return []
    values = order[0].get(key, [])
    if not isinstance(values, list):
        return []
    clean = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in clean:
            clean.append(value)
    return clean


def merge_legacy_loading_order(game_dir: Path, order: list[dict[str, Any]]) -> bool:
    legacy = load_legacy_loading_order(game_dir)
    if not legacy:
        return False
    _legacy_path, legacy_order = legacy
    item = order[0]
    changed = False

    current = loading_order_values(order, "aLoadOrder")
    for value in loading_order_values(legacy_order, "aLoadOrder"):
        if value not in current:
            current.append(value)
            changed = True
    if "core" not in current:
        current.insert(0, "core")
        changed = True
    if current and current[0] != "core":
        current = ["core"] + [value for value in current if value != "core"]
        changed = True
    if changed:
        item["aLoadOrder"] = current

    ignore = loading_order_values(order, "aIgnorePatterns")
    for value in loading_order_values(legacy_order, "aIgnorePatterns"):
        if value not in ignore:
            ignore.append(value)
            changed = True
    if changed:
        item["aIgnorePatterns"] = ignore
    return changed


def save_loading_order(path: Path, order: list[dict[str, Any]]) -> None:
    write_json_with_backup(path, order)


def set_data_mod_load_order(game_dir: Path, folder: str, enable: bool) -> tuple[Path, bool]:
    if not is_safe_relative_path(folder):
        raise BmmError(f"Unsafe data mod folder name: {folder}")
    path, order = load_loading_order(game_dir)
    item = order[0]
    original = loading_order_values(order, "aLoadOrder")
    merge_legacy_loading_order(game_dir, order)
    current = loading_order_values(order, "aLoadOrder")
    next_order = []
    if "core" not in current:
        next_order.append("core")
    for value in current:
        if value == folder:
            continue
        if value == "core":
            if "core" not in next_order:
                next_order.insert(0, "core")
        elif value not in next_order:
            next_order.append(value)
    if enable and folder not in next_order:
        next_order.append(folder)
    if not next_order or next_order[0] != "core":
        next_order = ["core"] + [value for value in next_order if value != "core"]
    changed = next_order != original
    if changed:
        item["aLoadOrder"] = next_order
        save_loading_order(path, order)
    return path, changed


def data_mod_enabled(game_dir: Path, folder: str) -> bool:
    try:
        _path, order = load_loading_order(game_dir)
    except BmmError:
        return False
    load_order = order[0].get("aLoadOrder", [])
    return isinstance(load_order, list) and folder in load_order


def save_data_mod_load_order(game_dir: Path, folders: list[str]) -> tuple[Path, bool]:
    path, order = load_loading_order(game_dir)
    item = order[0]
    original = loading_order_values(order, "aLoadOrder")
    merge_legacy_loading_order(game_dir, order)

    clean = []
    for folder in folders:
        value = str(folder or "").strip()
        if not value or value == "core":
            continue
        if not is_safe_relative_path(value):
            raise BmmError(f"Unsafe data mod folder name: {value}")
        if value not in clean:
            clean.append(value)

    next_order = ["core"] + clean
    changed = next_order != original or not path.exists()
    if changed:
        item["aLoadOrder"] = next_order
        save_loading_order(path, order)
    return path, changed


def safe_target(root: Path, relative: str, root_label: str = "install root") -> Path:
    rel = relative.replace("\\", "/").strip("/")
    if not rel:
        raise BmmError("Empty target path is not allowed.")
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(candidate)]) != str(root_resolved):
        raise BmmError(f"Target escapes {root_label}: {relative}")
    return candidate


def normalized_zip_name(name: str) -> str:
    pure = PurePosixPath(name)
    parts = []
    for part in pure.parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise BmmError(f"Archive entry escapes root: {name}")
        parts.append(part)
    return "/".join(parts)


def read_zip_json(zf: zipfile.ZipFile, name: str) -> Any:
    try:
        with zf.open(name) as fh:
            return json.loads(fh.read().decode("utf-8-sig"))
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BmmError(f"Invalid JSON in archive file {name}: {exc}") from exc


def first_mod_info_record(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    if isinstance(raw, dict):
        return raw
    return {}


def detect_data_mod_archive(archive: Path) -> dict[str, Any] | None:
    with zipfile.ZipFile(archive) as zf:
        files = [normalized_zip_name(info.filename) for info in zf.infolist() if not info.is_dir()]
        top_folders: set[str] = set()
        for name in files:
            parts = name.split("/")
            if len(parts) == 2 and parts[1].lower() == "mod_info.json":
                top_folders.add(parts[0])
        if len(top_folders) != 1:
            return None
        folder = sorted(top_folders)[0]
        mod_info_name = f"{folder}/mod_info.json"
        metadata = first_mod_info_record(read_zip_json(zf, mod_info_name))
        return {
            "folder": folder,
            "source": folder + "/",
            "target": folder + "/",
            "metadata": metadata,
        }


def read_archive_nest(archive: Path) -> dict[str, Any] | None:
    with zipfile.ZipFile(archive) as zf:
        names = {normalized_zip_name(info.filename): info for info in zf.infolist() if not info.is_dir()}
        if NEST_FILE_NAME not in names:
            return None
        try:
            with zf.open(NEST_FILE_NAME) as fh:
                raw = loads_json_with_comments(fh.read().decode("utf-8-sig"), f"{archive}:{NEST_FILE_NAME}")
        except (KeyError, UnicodeDecodeError) as exc:
            raise BmmError(f"Invalid JSON in archive file {NEST_FILE_NAME}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BmmError(f"{NEST_FILE_NAME} in archive must be a JSON object")
    if raw.get("schema") != NEST_SCHEMA:
        raise BmmError(f"{NEST_FILE_NAME} in archive schema must be {NEST_SCHEMA!r}")
    return raw


def verify_archive_nest_for_mod(archive: Path, mod: dict[str, Any]) -> None:
    if not isinstance(mod.get("nest"), dict):
        return
    nest = read_archive_nest(archive)
    if nest is None:
        raise BmmError(f"{mod['id']} was generated from {NEST_FILE_NAME}, but the archive does not contain {NEST_FILE_NAME}.")
    mods = nest.get("mods")
    if not isinstance(mods, list):
        raise BmmError(f"{NEST_FILE_NAME} in archive needs a mods array")
    wanted = str(mod.get("id") or "")
    for item in mods:
        if isinstance(item, dict) and str(item.get("id") or "") == wanted:
            return
    raise BmmError(f"{NEST_FILE_NAME} in archive does not contain mod id {wanted}.")


def validate_data_mod_archive_json(archive: Path, folder: str) -> None:
    errors = []
    prefix = folder.rstrip("/") + "/"
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = normalized_zip_name(info.filename)
            if not name.startswith(prefix) or not name.lower().endswith(".json"):
                continue
            try:
                read_zip_json(zf, name)
            except BmmError as exc:
                errors.append(str(exc))
    if errors:
        raise BmmError("Data mod archive has invalid JSON: " + "; ".join(errors[:5]))


def plan_data_mod_folder(plan: list[dict[str, Any]]) -> str | None:
    folders = set()
    for item in plan:
        if item.get("root") != DATA_MOD_ROOT:
            continue
        target = str(item.get("target", "")).replace("\\", "/").strip("/")
        if target:
            folders.add(target.split("/", 1)[0])
    if len(folders) == 1:
        return next(iter(folders))
    return None


def mod_info_to_summary(metadata: dict[str, Any]) -> str:
    notes = str(metadata.get("strNotes") or "").strip()
    name = str(metadata.get("strName") or "").strip()
    version = str(metadata.get("strModVersion") or "").strip()
    if notes:
        return notes
    if name and version:
        return f"{name} {version}"
    return "Ostranauts data mod"


def resolve_version(mod: dict[str, Any], wanted: str | None) -> dict[str, Any] | None:
    versions = mod.get("versions") or []
    if wanted:
        for version in versions:
            if str(version.get("version")) == wanted:
                return version
        raise BmmError(f"{mod['id']} has no declared version {wanted}")
    return latest_declared_version(mod)


def resolve_archive(mod: dict[str, Any], version: dict[str, Any] | None, rt: Runtime) -> tuple[Path, str]:
    expected_sha = None
    if version and isinstance(version.get("download"), dict):
        download = version["download"]
        expected_sha = download.get("sha256") or version.get("sha256")
        dtype = download.get("type")
        if dtype == "local":
            path = Path(str(download["path"])).expanduser()
            if not path.exists():
                raise BmmError(f"Local archive does not exist: {path}")
            if expected_sha:
                actual = sha256_file(path)
                if actual.lower() != str(expected_sha).lower():
                    raise BmmError(f"SHA256 mismatch for {path}: expected {expected_sha}, got {actual}")
            return path, str(download.get("source_label") or path)
        if dtype == "url":
            path = download_to_cache(str(download["url"]), rt.cache_dir, expected_sha)
            return path, str(download["url"])
        raise BmmError(f"Unsupported download type for {mod['id']}: {dtype}")

    release_spec = mod.get("release") or {}
    if release_spec.get("provider") != "github":
        raise BmmError(f"{mod['id']} needs a version download or a GitHub release provider.")
    release = github_latest_release(
        str(release_spec["repo"]),
        bool(release_spec.get("include_prereleases", False)),
    )
    asset = find_release_asset(release, release_spec.get("asset_pattern"))
    url = asset.get("browser_download_url")
    if not url:
        raise BmmError(f"GitHub asset has no browser_download_url: {asset.get('name')}")
    path = download_to_cache(str(url), rt.cache_dir, asset.get("digest", "").replace("sha256:", "") or None)
    return path, str(url)


def install_entries_for_archive(mod: dict[str, Any], version: dict[str, Any] | None, archive: Path) -> list[dict[str, str]]:
    install = {}
    if version and isinstance(version.get("install"), dict):
        install = version["install"]
    elif isinstance(mod.get("install"), dict):
        install = mod["install"]
    default_root = normalize_install_root(install.get("root") or install.get("strategy")) if isinstance(install, dict) else "bepinex_plugins"

    entries = install.get("entries") if isinstance(install, dict) else None
    if entries:
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise BmmError(f"Invalid install entry for {mod['id']}")
            root = normalize_install_root(entry.get("root") or default_root)
            result.append({"source": str(entry["source"]), "target": str(entry["target"]), "root": root})
        return result

    data_mod = detect_data_mod_archive(archive)
    with zipfile.ZipFile(archive) as zf:
        dlls = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = normalized_zip_name(info.filename)
            lowered = name.lower()
            if not lowered.endswith(".dll"):
                continue
            if lowered.startswith("bepinex/plugins/"):
                target = name[len("BepInEx/plugins/") :]
            else:
                target = PurePosixPath(name).name
            dlls.append({"source": name, "target": target, "root": "bepinex_plugins"})
    if data_mod and not dlls:
        return [{"source": data_mod["source"], "target": data_mod["target"], "root": DATA_MOD_ROOT}]
    if data_mod and dlls:
        raise BmmError(f"{mod['id']} mixes a data mod folder and DLLs; add explicit install.entries.")
    if len(dlls) != 1:
        raise BmmError(
            f"{mod['id']} needs install.entries because archive auto-detect found {len(dlls)} DLLs and no data mod folder."
        )
    return dlls


def expand_install_plan(archive: Path, entries: list[dict[str, str]]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive) as zf:
        zip_entries = {normalized_zip_name(i.filename): i for i in zf.infolist() if not i.is_dir()}
        for entry in entries:
            source_raw = entry["source"].replace("\\", "/").strip()
            source_is_dir = source_raw.endswith("/")
            source = source_raw.strip("/")
            target = entry["target"].replace("\\", "/").strip("/")
            root = normalize_install_root(entry.get("root"))
            if source_is_dir:
                prefix = source + "/"
                matched = False
                for name, info in zip_entries.items():
                    if name.startswith(prefix):
                        rest = name[len(prefix) :]
                        if rest:
                            plan.append(
                                {
                                    "zip": name,
                                    "target": str(PurePosixPath(target) / rest),
                                    "root": root,
                                    "size": info.file_size,
                                }
                            )
                            matched = True
                if not matched:
                    raise BmmError(f"Archive source directory not found: {source}")
            else:
                if source not in zip_entries:
                    raise BmmError(f"Archive source file not found: {source}")
                plan.append({"zip": source, "target": target, "root": root, "size": zip_entries[source].file_size})
    return plan


def backup_existing(target: Path, root: Path, backup_root: Path) -> Path | None:
    if not target.exists():
        return None
    rel = target.relative_to(root)
    backup = backup_root / str(rel).replace("\\", "/")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        if backup.exists():
            backup = backup.with_name(backup.name + "-" + stamp())
        shutil.copytree(target, backup)
    else:
        shutil.copy2(target, backup)
    return backup


def prompt_confirm(message: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    answer = input(message + " Type YES to continue: ").strip()
    if answer != "YES":
        raise BmmError("Cancelled.")


def command_init(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    game_dir = str(Path(args.game_dir).expanduser()) if args.game_dir else ""
    index = args.index or str(Path(__file__).with_name(DEFAULT_INDEX_NAME))
    config.update(
        {
            "game": "ostranauts",
            "game_dir": game_dir,
            "indexes": [index],
        }
    )
    rt.cache_dir.mkdir(parents=True, exist_ok=True)
    rt.backup_dir.mkdir(parents=True, exist_ok=True)
    write_json_with_backup(rt.config_path, config)
    print(f"Initialized {APP_NAME}")
    print(f"Config: {rt.config_path}")
    print(f"Game dir: {game_dir or 'not set'}")
    print(f"Index: {index}")
    return 0


def command_config(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    print(json.dumps(config, indent=2, sort_keys=True))
    print(f"Data dir: {rt.data_dir}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    game_dir = game_dir_from_config(config, args.game_dir)
    print(f"Data dir: {rt.data_dir}")
    print(f"Config: {rt.config_path} {'OK' if rt.config_path.exists() else 'missing'}")
    print(f"Game dir: {game_dir} {'OK' if game_dir.exists() else 'missing'}")
    bepinex = game_dir / "BepInEx"
    print(f"BepInEx: {bepinex} {'OK' if bepinex.exists() else 'missing'}")
    default_load_order, legacy_load_order = loading_order_paths(game_dir)
    active_load_order = loading_order_path(game_dir)
    configured = configured_loading_order_path()
    print(f"In-game mod setting: {configured or 'not found'}")
    print(f"BMM data load order: {active_load_order} {'OK' if active_load_order.exists() else 'missing'}")
    print(f"Default data load order: {default_load_order} {'OK' if default_load_order.exists() else 'missing'}")
    print(f"Legacy data load order: {legacy_load_order} {'present' if legacy_load_order.exists() else 'missing'}")
    for warning in loading_order_warnings(game_dir):
        print(f"Warning: {warning}")
    ok = bepinex.exists()
    for root_name in INSTALL_ROOTS:
        root = install_root_path(game_dir, root_name)
        print(f"{root_name}: {root} {'OK' if root.exists() else 'missing'}")
        if root_name == "bepinex_plugins" and not root.exists():
            ok = False
    return 0 if ok else 1


def command_validate_index(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    index = load_index(args.index, config)
    errors = validate_index(index)
    if errors:
        print("Index validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Index validation OK")
    print(f"Mods: {len(get_mods(index))}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    index = load_index(args.index, config)
    errors = validate_index(index)
    if errors:
        raise BmmError("Index is invalid. Run validate-index for details.")

    installed = state.get("installed", {})
    for mod in sorted(get_mods(index), key=lambda m: str(m.get("name", "")).lower()):
        declared = latest_declared_version(mod)
        latest = declared.get("version") if declared else "github"
        record = installed.get(mod["id"])
        status = "not installed"
        if record:
            status = f"installed {record.get('version', '?')}"
            if not record.get("enabled", True):
                status += " disabled"
        print(f"{mod['id']:<28} {latest:<12} {status:<24} {mod['name']}")
    return 0


def command_scan(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    game_dir = game_dir_from_config(config, args.game_dir)
    ensure_game_dir(game_dir)
    for root_name in INSTALL_ROOTS:
        root = install_root_path(game_dir, root_name)
        print(f"[{root_name}] {root}")
        if not root.exists():
            print("  missing")
            continue
        for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if path.name.lower() == "backups":
                continue
            if path.is_dir():
                print(f"  dir  {path.name}/")
            else:
                print(f"  file {path.name} {path.stat().st_size} bytes")
    return 0


def command_check(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    index = load_index(args.index, config)
    errors = validate_index(index)
    if errors:
        raise BmmError("Index is invalid. Run validate-index for details.")

    installed = state.get("installed", {})
    mods = get_mods(index)
    if args.mod_id:
        mods = [find_mod(index, args.mod_id)]

    had_error = False
    for mod in mods:
        release_spec = mod.get("release") or {}
        declared = latest_declared_version(mod)
        local_latest = declared.get("version") if declared else None
        installed_version = (installed.get(mod["id"]) or {}).get("version")
        github_version = None
        github_url = None
        note = ""
        if release_spec.get("provider") == "github":
            try:
                release = github_latest_release(
                    str(release_spec["repo"]),
                    bool(release_spec.get("include_prereleases", False)),
                )
                github_version = str(release.get("tag_name") or "").lstrip("v")
                github_url = release.get("html_url")
            except BmmError as exc:
                had_error = True
                note = f"GitHub check failed: {exc}"
        latest = github_version or local_latest or "unknown"
        status = "not installed"
        if installed_version:
            cmp_latest = latest if latest != "unknown" else local_latest
            if not cmp_latest:
                status = f"installed {installed_version} (latest unknown)"
            elif version_key(str(installed_version)) < version_key(str(cmp_latest)):
                status = f"update available {installed_version} -> {cmp_latest}"
            elif version_key(str(installed_version)) > version_key(str(cmp_latest)):
                status = f"installed {installed_version} (remote behind local {cmp_latest})"
            else:
                status = f"up to date {installed_version}"
        print(f"{mod['id']}: latest={latest} status={status}")
        if local_latest:
            print(f"  index: {local_latest}")
        if github_version:
            print(f"  github: {github_version}")
            if local_latest and version_key(str(local_latest)) > version_key(str(github_version)):
                print("  note: index version is newer than the latest GitHub release")
        if github_url:
            print(f"  release: {github_url}")
        if note:
            print(f"  {note}")
    return 1 if had_error else 0


def command_install(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    index = load_index(args.index, config)
    errors = validate_index(index)
    if errors:
        raise BmmError("Index is invalid. Run validate-index for details.")
    mod = find_mod(index, args.mod_id)
    version = resolve_version(mod, args.version)
    ensure_relationships_ok(mod, version, index, state)
    archive, source_label = resolve_archive(mod, version, rt)
    game_dir = game_dir_from_config(config, args.game_dir)
    entries = install_entries_for_archive(mod, version, archive)
    plan = expand_install_plan(archive, entries)
    roots = {str(item["root"]) for item in plan}
    ensure_game_dir(game_dir)
    if roots - {DATA_MOD_ROOT}:
        ensure_bepinex_dir(game_dir)
    if DATA_MOD_ROOT in roots:
        (game_dir / "Ostranauts_Data").mkdir(parents=True, exist_ok=True)
        data_mods_dir(game_dir).mkdir(parents=True, exist_ok=True)
    version_label = str((version or {}).get("version") or "github-latest")
    backup_root = rt.backup_dir / mod["id"] / ("install-" + stamp())
    data_folder = plan_data_mod_folder(plan)
    verify_archive_nest_for_mod(archive, mod)
    if DATA_MOD_ROOT in roots and not data_folder:
        raise BmmError("Data mod installs must target exactly one folder under Ostranauts_Data/Mods.")
    if data_folder:
        validate_data_mod_archive_json(archive, data_folder)
    has_bepinex_files = bool(roots - {DATA_MOD_ROOT})
    if data_folder and has_bepinex_files:
        mod_type = "hybrid"
    elif data_folder:
        mod_type = "data"
    else:
        mod_type = "bepinex"

    print(f"Install {mod['name']} {version_label}")
    print(f"Archive: {archive}")
    for root_name in sorted({str(item["root"]) for item in plan}):
        print(f"Target {root_name}: {install_root_path(game_dir, root_name)}")
    for item in plan:
        root = install_root_path(game_dir, str(item["root"]))
        target = safe_target(root, item["target"], str(item["root"]))
        action = "overwrite" if target.exists() else "create"
        print(f"  {action}: [{item['root']}] {item['target']} ({item['size']} bytes)")
    prompt_confirm("Install will copy files into whitelisted BepInEx install roots.", args.yes)

    installed_files = []
    backups = []
    with zipfile.ZipFile(archive) as zf:
        for item in plan:
            root_name = str(item["root"])
            root = install_root_path(game_dir, root_name)
            root.mkdir(parents=True, exist_ok=True)
            target = safe_target(root, item["target"], root_name)
            backup = backup_existing(target, root, backup_root / root_name)
            if backup:
                backups.append(str(backup))
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item["zip"]) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            installed_files.append(
                {
                    "root": root_name,
                    "path": item["target"].replace("\\", "/"),
                    "sha256": sha256_file(target),
                    "bytes": target.stat().st_size,
                }
            )

    load_order_path_value = ""
    if data_folder:
        load_order, changed = set_data_mod_load_order(game_dir, data_folder, True)
        load_order_path_value = str(load_order)
        print(f"Data mod load order: {'added' if changed else 'already enabled'} {data_folder}")
        print(f"Load order: {load_order}")

    state.setdefault("installed", {})[mod["id"]] = {
        "id": mod["id"],
        "name": mod["name"],
        "type": mod_type,
        "version": version_label,
        "enabled": True,
        "installed_at": stamp(),
        "source": source_label,
        "plugin": mod.get("plugin", {}),
        "data_mod_folder": data_folder or "",
        "load_order_path": load_order_path_value,
        "provides": [item for item in merged_relationships(mod, version)["provides"] if isinstance(item, str)],
        "files": installed_files,
        "backups": backups,
    }
    save_state(rt, state)
    print(f"Installed {mod['id']} {version_label}")
    if backups:
        print(f"Backups: {backup_root}")
    return 0


def command_uninstall(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    installed = state.get("installed", {})
    record = installed.get(args.mod_id)
    if not record:
        raise BmmError(f"{args.mod_id} is not installed by BMM.")
    game_dir = game_dir_from_config(config, args.game_dir)
    ensure_game_dir(game_dir)
    roots = {
        str(file_record.get("root") or "bepinex_plugins")
        for file_record in record.get("files", [])
        if isinstance(file_record, dict)
    }
    if roots - {DATA_MOD_ROOT}:
        ensure_bepinex_dir(game_dir)
    backup_root = rt.backup_dir / args.mod_id / ("uninstall-" + stamp())

    print(f"Uninstall {record.get('name', args.mod_id)} {record.get('version', '?')}")
    for file_record in record.get("files", []):
        print(f"  remove with backup: [{file_record.get('root', 'bepinex_plugins')}] {file_record.get('path')}")
    prompt_confirm("Uninstall will move BMM-managed files out of whitelisted install roots.", args.yes)

    moved = []
    for file_record in record.get("files", []):
        root_name = str(file_record.get("root") or "bepinex_plugins")
        root = install_root_path(game_dir, root_name)
        rel = file_record.get("disabled_path") or file_record.get("path")
        if not rel:
            continue
        target = safe_target(root, rel, root_name)
        if not target.exists():
            continue
        backup = backup_root / root_name / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            backup = backup.with_name(backup.name + "-" + stamp())
        shutil.move(str(target), str(backup))
        moved.append(str(backup))
        if root_name == DATA_MOD_ROOT:
            parent = target.parent
            while parent != root and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    data_folder = str(record.get("data_mod_folder") or "").strip()
    if data_folder:
        load_order, changed = set_data_mod_load_order(game_dir, data_folder, False)
        print(f"Data mod load order: {'removed' if changed else 'already disabled'} {data_folder}")
        print(f"Load order: {load_order}")
    del installed[args.mod_id]
    save_state(rt, state)
    print(f"Uninstalled {args.mod_id}")
    if moved:
        print(f"Moved files to: {backup_root}")
    return 0


def command_disable(args: argparse.Namespace) -> int:
    return set_enabled(args, False)


def command_enable(args: argparse.Namespace) -> int:
    return set_enabled(args, True)


def change_mod_enabled(
    config: dict[str, Any],
    state: dict[str, Any],
    mod_id: str,
    enable: bool,
    game_dir_override: str | None = None,
) -> list[str]:
    record = state.get("installed", {}).get(mod_id)
    if not record:
        raise BmmError(f"{mod_id} is not installed by BMM.")
    game_dir = game_dir_from_config(config, game_dir_override)
    ensure_game_dir(game_dir)
    changed = []

    data_folder = str(record.get("data_mod_folder") or "").strip()
    if not data_folder:
        data_files = [
            file_record
            for file_record in record.get("files", [])
            if isinstance(file_record, dict) and file_record.get("root") == DATA_MOD_ROOT
        ]
        if data_files:
            rel = str(data_files[0].get("path") or "").replace("\\", "/").strip("/")
            data_folder = rel.split("/", 1)[0] if rel else ""
    if data_folder:
        load_order, did_change = set_data_mod_load_order(game_dir, data_folder, enable)
        record["enabled"] = bool(enable)
        record["load_order_path"] = str(load_order)
        if did_change:
            changed.append(f"[{DATA_MOD_ROOT}] {data_folder} {'enabled' if enable else 'disabled'} in {load_order}")

    plugin_files = [
        file_record
        for file_record in record.get("files", [])
        if isinstance(file_record, dict)
        and str(file_record.get("root") or "bepinex_plugins") != DATA_MOD_ROOT
        and str(file_record.get("path") or "").lower().endswith(".dll")
    ]
    if not plugin_files:
        return changed

    ensure_bepinex_dir(game_dir)

    if enable:
        for file_record in plugin_files:
            disabled_rel = file_record.get("disabled_path")
            original_rel = file_record.get("path")
            if not disabled_rel or not original_rel:
                continue
            root_name = str(file_record.get("root") or "bepinex_plugins")
            root = install_root_path(game_dir, root_name)
            disabled = safe_target(root, disabled_rel, root_name)
            original = safe_target(root, original_rel, root_name)
            if disabled.exists():
                if original.exists():
                    raise BmmError(f"Cannot enable because target already exists: {original}")
                disabled.rename(original)
                changed.append(f"[{root_name}] {original_rel}")
            file_record.pop("disabled_path", None)
        record["enabled"] = True
    else:
        for file_record in plugin_files:
            original_rel = str(file_record.get("path", ""))
            if not original_rel.lower().endswith(".dll"):
                continue
            root_name = str(file_record.get("root") or "bepinex_plugins")
            root = install_root_path(game_dir, root_name)
            original = safe_target(root, original_rel, root_name)
            disabled_rel = original_rel + ".disabled"
            disabled = safe_target(root, disabled_rel, root_name)
            if original.exists():
                if disabled.exists():
                    disabled = safe_target(root, original_rel + ".disabled-" + stamp(), root_name)
                    disabled_rel = str(disabled.relative_to(root)).replace("\\", "/")
                original.rename(disabled)
                file_record["disabled_path"] = disabled_rel
                changed.append(f"[{root_name}] {disabled_rel}")
        record["enabled"] = False
    return changed


def set_enabled(args: argparse.Namespace, enable: bool) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    changed = change_mod_enabled(config, state, args.mod_id, enable, args.game_dir)
    action = "Enabled" if enable else "Disabled"

    save_state(rt, state)
    print(f"{action} {args.mod_id}")
    for rel in changed:
        print(f"  {rel}")
    return 0


def inspect_archive_contents(archive: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as zf:
        files = []
        total_size = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = normalized_zip_name(info.filename)
            files.append({"name": name, "size": info.file_size})
            total_size += info.file_size
    dlls = [item for item in files if item["name"].lower().endswith(".dll")]
    metadata = [
        item
        for item in files
        if item["name"].lower().endswith(("manifest.json", "mod_info.json", "autoload.meta.toml"))
    ]
    top_dirs = sorted({item["name"].split("/", 1)[0] for item in files if "/" in item["name"]})
    data_mod = detect_data_mod_archive(archive)
    return {
        "file_count": len(files),
        "total_size": total_size,
        "dlls": dlls,
        "metadata": metadata,
        "top_dirs": top_dirs,
        "data_mod": data_mod,
    }


def command_inspect(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    target = Path(args.target).expanduser()
    mod = None
    version = None
    source_label = str(target)
    if target.exists():
        archive = target
    else:
        index = load_index(args.index, config)
        errors = validate_index(index)
        if errors:
            raise BmmError("Index is invalid. Run validate-index for details.")
        mod = find_mod(index, args.target)
        version = resolve_version(mod, args.version)
        archive, source_label = resolve_archive(mod, version, rt)

    info = inspect_archive_contents(archive)
    print(f"Archive: {archive}")
    print(f"Source: {source_label}")
    print(f"Files: {info['file_count']} ({info['total_size']} bytes unpacked)")
    if info["top_dirs"]:
        print("Top folders: " + ", ".join(info["top_dirs"]))
    if info["metadata"]:
        print("Metadata files:")
        for item in info["metadata"]:
            print(f"  {item['name']} ({item['size']} bytes)")
    if info.get("data_mod"):
        data_mod = info["data_mod"]
        metadata = data_mod.get("metadata", {}) if isinstance(data_mod, dict) else {}
        print(f"Data mod folder: {data_mod.get('folder')}")
        if metadata:
            print(f"  name: {metadata.get('strName', '')}")
            print(f"  author: {metadata.get('strAuthor', '')}")
            print(f"  game: {metadata.get('strGameVersion', '')}")
            print(f"  version: {metadata.get('strModVersion', '')}")
    if info["dlls"]:
        print("DLLs:")
        for item in info["dlls"]:
            print(f"  {item['name']} ({item['size']} bytes)")
    else:
        print("DLLs: none")

    if mod:
        try:
            entries = install_entries_for_archive(mod, version, archive)
            plan = expand_install_plan(archive, entries)
            print("Install plan:")
            for item in plan:
                print(f"  [{item['root']}] {item['target']} <- {item['zip']} ({item['size']} bytes)")
        except BmmError as exc:
            print(f"Install plan: {exc}")
    return 0


def command_profiles(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    state = load_state(rt)
    profiles = state.get("profiles", {})
    if not profiles:
        print("No saved profiles.")
        return 0
    for name, profile in sorted(profiles.items()):
        mods = profile.get("mods", []) if isinstance(profile, dict) else []
        updated = profile.get("updated_at", "?") if isinstance(profile, dict) else "?"
        print(f"{name:<24} {len(mods):>3} mods  updated {updated}")
    return 0


def command_profile_save(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    state = load_state(rt)
    profiles = state.setdefault("profiles", {})
    existing = profiles.get(args.name, {}) if isinstance(profiles.get(args.name), dict) else {}
    mods = []
    for mod_id, record in sorted(state.get("installed", {}).items()):
        mods.append(
            {
                "id": mod_id,
                "version": record.get("version", "?") if isinstance(record, dict) else "?",
                "enabled": bool(record.get("enabled", True)) if isinstance(record, dict) else True,
            }
        )
    profiles[args.name] = {
        "name": args.name,
        "created_at": existing.get("created_at") or stamp(),
        "updated_at": stamp(),
        "mods": mods,
    }
    save_state(rt, state)
    print(f"Saved profile {args.name} with {len(mods)} installed mods.")
    return 0


def command_profile_show(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    state = load_state(rt)
    profile = state.get("profiles", {}).get(args.name)
    if not isinstance(profile, dict):
        raise BmmError(f"Profile not found: {args.name}")
    print(f"Profile: {args.name}")
    print(f"Updated: {profile.get('updated_at', '?')}")
    for item in profile.get("mods", []):
        if not isinstance(item, dict):
            continue
        status = "enabled" if item.get("enabled", True) else "disabled"
        print(f"  {item.get('id')} {item.get('version', '?')} {status}")
    return 0


def command_profile_apply(args: argparse.Namespace) -> int:
    rt = make_runtime(args.data_dir)
    config = load_config(rt)
    state = load_state(rt)
    profile = state.get("profiles", {}).get(args.name)
    if not isinstance(profile, dict):
        raise BmmError(f"Profile not found: {args.name}")
    wanted = {
        str(item.get("id")): bool(item.get("enabled", True))
        for item in profile.get("mods", [])
        if isinstance(item, dict) and item.get("id")
    }
    installed = state.get("installed", {})
    missing = [mod_id for mod_id in wanted if mod_id not in installed]
    changes: list[tuple[str, bool]] = []
    for mod_id, should_enable in wanted.items():
        record = installed.get(mod_id)
        if not isinstance(record, dict):
            continue
        if bool(record.get("enabled", True)) != should_enable:
            changes.append((mod_id, should_enable))
    if args.disable_extra:
        for mod_id, record in installed.items():
            if mod_id not in wanted and isinstance(record, dict) and record.get("enabled", True):
                changes.append((mod_id, False))

    print(f"Apply profile {args.name}")
    for mod_id in missing:
        print(f"  missing installed mod: {mod_id}")
    for mod_id, should_enable in changes:
        print(f"  {'enable' if should_enable else 'disable'}: {mod_id}")
    if not changes:
        print("  no enable/disable changes needed")
        return 0 if not missing else 1
    prompt_confirm("Profile apply will change enabled state for BMM-managed plugin and data mods.", args.yes)

    changed_paths = []
    for mod_id, should_enable in changes:
        changed_paths.extend(change_mod_enabled(config, state, mod_id, should_enable, args.game_dir))
    save_state(rt, state)
    print(f"Applied profile {args.name}")
    for rel in changed_paths:
        print(f"  {rel}")
    return 0 if not missing else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BMM prototype for Ostranauts BepInEx mods")
    parser.add_argument("--data-dir", help="BMM data directory. Defaults to the local Mod_index folder.")
    parser.add_argument("--index", help="Index JSON path or URL.")
    parser.add_argument("--game-dir", help="Ostranauts game directory override.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create BMM config")
    p.add_argument("--index", help="Index path or URL to store in config.")
    p.add_argument("--game-dir", help="Ostranauts game directory.")
    p.set_defaults(func=command_init)

    p = sub.add_parser("config", help="Show BMM config")
    p.set_defaults(func=command_config)

    p = sub.add_parser("doctor", help="Check configured paths")
    p.set_defaults(func=command_doctor)

    p = sub.add_parser("validate-index", help="Validate a BMM index")
    p.set_defaults(func=command_validate_index)

    p = sub.add_parser("list", help="List indexed mods and install state")
    p.set_defaults(func=command_list)

    p = sub.add_parser("scan", help="List current BepInEx and data mod folder contents")
    p.set_defaults(func=command_scan)

    p = sub.add_parser("inspect", help="Inspect a mod archive or indexed mod install plan")
    p.add_argument("target", help="Zip path or indexed mod id.")
    p.add_argument("--version")
    p.set_defaults(func=command_inspect)

    p = sub.add_parser("check", help="Check GitHub release status")
    p.add_argument("mod_id", nargs="?")
    p.set_defaults(func=command_check)

    p = sub.add_parser("install", help="Install a mod from the index")
    p.add_argument("mod_id")
    p.add_argument("--version")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p.set_defaults(func=command_install)

    p = sub.add_parser("uninstall", help="Uninstall a BMM-managed mod with backup")
    p.add_argument("mod_id")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p.set_defaults(func=command_uninstall)

    p = sub.add_parser("disable", help="Disable a BMM-managed mod by renaming DLLs")
    p.add_argument("mod_id")
    p.set_defaults(func=command_disable)

    p = sub.add_parser("enable", help="Enable a previously disabled BMM-managed mod")
    p.add_argument("mod_id")
    p.set_defaults(func=command_enable)

    p = sub.add_parser("profiles", help="List saved profiles")
    p.set_defaults(func=command_profiles)

    p = sub.add_parser("profile-save", help="Save the current installed BMM mod state as a profile")
    p.add_argument("name")
    p.set_defaults(func=command_profile_save)

    p = sub.add_parser("profile-show", help="Show a saved profile")
    p.add_argument("name")
    p.set_defaults(func=command_profile_show)

    p = sub.add_parser("profile-apply", help="Apply enabled/disabled state from a saved profile")
    p.add_argument("name")
    p.add_argument("--disable-extra", action="store_true", help="Disable installed BMM mods that are not in the profile.")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    p.set_defaults(func=command_profile_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BmmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
