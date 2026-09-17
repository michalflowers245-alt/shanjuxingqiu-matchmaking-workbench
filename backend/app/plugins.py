from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .db import Database, database, json_dumps, utc_now


PLUGIN_ROOT = PROJECT_ROOT / "plugins"


class PluginManager:
    def __init__(self, db: Database = database):
        self.db = db

    def reload(self) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        if not PLUGIN_ROOT.exists():
            return discovered
        for manifest_path in PLUGIN_ROOT.glob("*/plugin.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                plugin_id = str(manifest["id"])
                entrypoint = (manifest_path.parent / manifest["entrypoint"]).resolve()
                if entrypoint.parent != manifest_path.parent.resolve() or not entrypoint.is_file() or entrypoint.suffix != ".py":
                    continue
                row = {
                    "id": plugin_id,
                    "name": str(manifest.get("name") or plugin_id),
                    "version": str(manifest.get("version") or "0.1.0"),
                    "entrypoint": str(entrypoint),
                    "manifest_json": json_dumps(manifest),
                    "updated_at": utc_now(),
                }
                existing = self.db.one("SELECT id FROM plugin_registry WHERE id=?", (plugin_id,))
                if existing:
                    self.db.execute(
                        "UPDATE plugin_registry SET name=?,version=?,entrypoint=?,manifest_json=?,updated_at=? WHERE id=?",
                        (row["name"], row["version"], row["entrypoint"], row["manifest_json"], row["updated_at"], plugin_id),
                    )
                else:
                    self.db.execute(
                        "INSERT INTO plugin_registry(id,name,version,entrypoint,manifest_json,enabled,updated_at) VALUES(?,?,?,?,?,1,?)",
                        (plugin_id, row["name"], row["version"], row["entrypoint"], row["manifest_json"], row["updated_at"]),
                    )
                discovered.append({**row, "manifest": manifest})
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return discovered

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM plugin_registry ORDER BY name")
        for row in rows:
            row["manifest"] = json.loads(row.pop("manifest_json"))
        return rows

    def invoke(self, plugin_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM plugin_registry WHERE id=? AND enabled=1", (plugin_id,))
        if not row:
            raise ValueError("插件不存在或未启用")
        entrypoint = Path(row["entrypoint"]).resolve()
        if entrypoint.parent.parent != PLUGIN_ROOT.resolve() or not entrypoint.is_file():
            raise ValueError("插件入口不在允许目录内")
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "WORKBENCH_PLUGIN_ID": plugin_id,
        }
        process = subprocess.run(
            [sys.executable, str(entrypoint)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=safe_env,
            cwd=str(entrypoint.parent),
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError((process.stderr or "插件执行失败")[:500])
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("插件未返回合法 JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("插件必须返回 JSON 对象")
        return result


plugin_manager = PluginManager()

