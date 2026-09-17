#!/usr/bin/env python3
"""Read-only MCP server for the local matchmaking/content workbench.

The server intentionally exposes a small, fixed set of read-only business
queries instead of arbitrary SQL. It speaks the MCP stdio JSON-RPC transport
without taking a dependency on the full MCP SDK, so it can run from the
workbench's bundled virtual environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "matchmaking-workbench-readonly"
SERVER_VERSION = "1.0.0"
MAX_LIMIT = 100
MAX_TEXT = 2000
RATE_WINDOW_SECONDS = 60.0
MAX_CALLS_PER_WINDOW = 60


def project_root() -> Path:
    # scripts/mcp/<file>.py -> project root is two parents up.
    return Path(__file__).resolve().parents[2]


def database_path() -> Path:
    configured = os.getenv("WORKBENCH_MCP_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = project_root()
    preferred = root / ".workbench" / "copy_workbench.sqlite3"
    legacy = root / ".workbench" / "workbench.sqlite3"
    return preferred if preferred.exists() else legacy


def audit_path() -> Path:
    configured = os.getenv("WORKBENCH_MCP_AUDIT_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else project_root() / ".workbench" / "mcp_audit.log"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp_limit(value: Any, default: int = 20) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(MAX_LIMIT, limit))


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def redact(value: Any) -> Any:
    """Recursively remove common credential/session fields from JSON data."""
    secret_tokens = ("secret", "token", "password", "cookie", "authorization", "api_key", "apikey", "private_key")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in secret_tokens):
                continue
            result[str(key)] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value[:100]]
    if isinstance(value, str):
        return clean_text(value)
    return value


def parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class ReadOnlyStore:
    def __init__(self, db_path: Path | None = None, audit_file: Path | None = None) -> None:
        self.db_path = (db_path or database_path()).resolve()
        self.audit_file = (audit_file or audit_path()).resolve()
        self._calls: list[float] = []
        if not self.db_path.is_file():
            raise FileNotFoundError(f"工作台数据库不存在: {self.db_path}")

    def _connect(self) -> sqlite3.Connection:
        # URI mode=ro prevents accidental writes even if a future handler is
        # changed. query_only is an additional SQLite-level guard.
        uri = f"file:{self.db_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1500")
        return connection

    def _check_rate(self) -> None:
        cutoff = time.monotonic() - RATE_WINDOW_SECONDS
        self._calls = [stamp for stamp in self._calls if stamp >= cutoff]
        if len(self._calls) >= MAX_CALLS_PER_WINDOW:
            raise RuntimeError("只读 MCP 查询频率已达到上限，请稍后再试")
        self._calls.append(time.monotonic())

    def _audit(self, tool: str, args: dict[str, Any], started: float, status: str, rows: int = 0, error: str = "") -> None:
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        # Store only metadata. Query text is hashed and arguments are never
        # written, so prompts, tokens and personal notes cannot leak to logs.
        query = str(args.get("query") or args.get("workspace_id") or "")
        event = {
            "at": now_iso(),
            "tool": tool,
            "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:12] if query else "",
            "duration_ms": round((time.monotonic() - started) * 1000),
            "rows": rows,
            "status": status,
        }
        if error:
            event["error"] = clean_text(error, 300)
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def call(self, tool: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = args if isinstance(args, dict) else {}
        started = time.monotonic()
        self._check_rate()
        try:
            handler = getattr(self, f"tool_{tool}", None)
            if handler is None:
                raise ValueError(f"未知只读工具: {tool}")
            result = handler(args)
            rows = len(result.get("items", [])) if isinstance(result, dict) else 0
            self._audit(tool, args, started, "ok", rows)
            return result
        except Exception as exc:
            self._audit(tool, args, started, "error", error=str(exc))
            raise

    @staticmethod
    def _require_workspace(args: dict[str, Any]) -> str:
        workspace_id = str(args.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id 不能为空")
        return workspace_id

    def tool_list_workspaces(self, args: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,name,description,color,created_at,updated_at FROM workspaces ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
        return {"items": [dict(row) for row in rows], "read_only": True}

    def tool_get_workspace_context(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        with self._connect() as db:
            workspace = db.execute(
                "SELECT id,name,description,color,created_at,updated_at FROM workspaces WHERE id=?",
                (workspace_id,),
            ).fetchone()
            if not workspace:
                raise ValueError("品牌工作区不存在")
            profile = db.execute("SELECT profile_json,completed_fields_json,updated_at FROM brand_profiles WHERE workspace_id=?", (workspace_id,)).fetchone()
            counts: dict[str, int] = {}
            for table, column in (
                ("libraries", "workspace_id"), ("documents", "workspace_id"), ("voice_samples", "workspace_id"),
                ("copy_tasks", "workspace_id"), ("topic_monitor_items", "workspace_id"),
                ("xhs_post_extractions", "workspace_id"), ("life_cases", "workspace_id"),
                ("performance_records", "workspace_id"),
            ):
                try:
                    counts[table] = int(db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (workspace_id,)).fetchone()[0])
                except sqlite3.OperationalError:
                    counts[table] = 0
        return {
            "workspace": dict(workspace),
            "brand_profile": redact(parse_json(profile["profile_json"], {})) if profile else {},
            "completed_fields": parse_json(profile["completed_fields_json"], []) if profile else [],
            "profile_updated_at": profile["updated_at"] if profile else None,
            "counts": counts,
            "read_only": True,
        }

    def tool_list_content_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 20)
        status = str(args.get("status") or "").strip()
        where = "workspace_id=?"
        params: list[Any] = [workspace_id]
        if status:
            where += " AND status=?"
            params.append(status)
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT id,content_type,platform,topic,goal,offer,status,stage,revision_round,
                           retry_count,last_error,source_case_id,created_at,updated_at
                    FROM copy_tasks WHERE {where} ORDER BY created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return {"items": [dict(row) for row in rows], "read_only": True}

    def tool_get_task_result(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id 不能为空")
        with self._connect() as db:
            task = db.execute(
                """SELECT id,workspace_id,content_type,platform,topic,goal,offer,status,stage,
                          revision_round,created_at,updated_at,last_error
                   FROM copy_tasks WHERE id=? AND workspace_id=?""",
                (task_id, workspace_id),
            ).fetchone()
            if not task:
                raise ValueError("任务不存在或不属于当前工作区")
            drafts = db.execute(
                """SELECT id,version,origin,body_text,is_final,created_at
                   FROM draft_versions WHERE task_id=? AND workspace_id=? ORDER BY version DESC LIMIT 5""",
                (task_id, workspace_id),
            ).fetchall()
            reviews = db.execute(
                """SELECT round,total_score,decision,change_summary,created_at
                   FROM editor_reviews WHERE task_id=? ORDER BY round DESC LIMIT 5""",
                (task_id,),
            ).fetchall()
            sources = db.execute(
                """SELECT source_key,kind,title,url,published_at,excerpt,credibility,created_at
                   FROM research_sources WHERE task_id=? ORDER BY created_at DESC LIMIT 30""",
                (task_id,),
            ).fetchall()
        return {
            "task": dict(task),
            "drafts": [dict(row) for row in drafts],
            "reviews": [dict(row) for row in reviews],
            "sources": [dict(row) for row in sources],
            "read_only": True,
        }

    def tool_search_knowledge(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        query = clean_text(args.get("query"), 200).strip()
        if not query:
            raise ValueError("query 不能为空")
        limit = clamp_limit(args.get("limit"), 10)
        with self._connect() as db:
            try:
                rows = db.execute(
                    """SELECT c.id AS chunk_id,c.document_id,c.ordinal,c.content,
                              d.title,d.source_type,d.source_uri,l.name AS library_name
                       FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id
                       JOIN documents d ON d.id=c.document_id
                       JOIN libraries l ON l.id=d.library_id
                       WHERE f.workspace_id=? AND chunks_fts MATCH ?
                       ORDER BY c.ordinal LIMIT ?""",
                    (workspace_id, query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = db.execute(
                    """SELECT c.id AS chunk_id,c.document_id,c.ordinal,c.content,
                              d.title,d.source_type,d.source_uri,l.name AS library_name
                       FROM chunks c JOIN documents d ON d.id=c.document_id
                       JOIN libraries l ON l.id=d.library_id
                       WHERE c.workspace_id=? AND c.content LIKE ?
                       ORDER BY c.ordinal LIMIT ?""",
                    (workspace_id, f"%{query}%", limit),
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["content"] = clean_text(item.get("content"), MAX_TEXT)
            items.append(item)
        return {"items": items, "query": query, "read_only": True}

    def tool_list_radar_items(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 20)
        platform = str(args.get("platform") or "").strip()
        where = "workspace_id=?"
        params: list[Any] = [workspace_id]
        if platform:
            where += " AND platform=?"
            params.append(platform)
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT id,monitor_id,run_id,platform,source_mode,source_key,title,url,
                           excerpt,author,published_at,metrics_json,is_new,discovered_at
                    FROM topic_monitor_items WHERE {where}
                    ORDER BY discovered_at DESC LIMIT ?""",
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["excerpt"] = clean_text(item.get("excerpt"))
            item["metrics"] = redact(parse_json(item.pop("metrics_json"), {}))
            items.append(item)
        return {"items": items, "read_only": True}

    def tool_list_social_extractions(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 20)
        include_body = bool(args.get("include_body", False))
        with self._connect() as db:
            rows = db.execute(
                """SELECT url,post_key,search_keyword,title,author,published_at,body,image_text,
                          video_subtitle,video_speech,merged_copy,tags_json,metrics_json,status,
                          completeness,ocr_status,asr_status,failure_stage,error_message,extracted_at,updated_at
                   FROM xhs_post_extractions WHERE workspace_id=? ORDER BY extracted_at DESC LIMIT ?""",
                (workspace_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for key in ("body", "image_text", "video_subtitle", "video_speech", "merged_copy"):
                value = item.pop(key, "")
                if include_body:
                    item[key] = clean_text(value)
            item["tags"] = parse_json(item.pop("tags_json"), [])
            item["metrics"] = redact(parse_json(item.pop("metrics_json"), {}))
            items.append(item)
        return {"items": items, "include_body": include_body, "read_only": True}

    def tool_list_verified_xhs_archives(self, args: dict[str, Any]) -> dict[str, Any]:
        """Expose verified archive evidence without files, credentials or local paths."""
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 20)
        include_body = bool(args.get("include_body", False))
        with self._connect() as db:
            try:
                rows = db.execute(
                    """SELECT v.id,v.extraction_id,v.post_id,v.observed_post_id,v.body_raw,
                              v.body_normalized,v.body_source,v.archive_status,v.verification_status,
                              v.content_sha256,v.manifest_sha256,v.captured_at,v.collector_version,
                              e.url,e.title,e.author,e.expected_asset_count,e.saved_asset_count
                       FROM xhs_archive_versions v
                       JOIN xhs_post_extractions e
                         ON e.workspace_id=v.workspace_id AND e.id=v.extraction_id
                       WHERE v.workspace_id=? ORDER BY v.captured_at DESC LIMIT ?""",
                    (workspace_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return {
                    "items": [],
                    "include_body": include_body,
                    "supported": False,
                    "message": "当前数据库尚未执行小红书归档 V2 迁移",
                    "read_only": True,
                }
            items = []
            for row in rows:
                item = dict(row)
                raw = item.pop("body_raw", "")
                normalized = item.pop("body_normalized", "")
                if include_body:
                    item["body_raw"] = clean_text(raw, MAX_TEXT)
                    item["body_normalized"] = clean_text(normalized, MAX_TEXT)
                assets = db.execute(
                    """SELECT role,ordinal,mime_detected,extension,width,height,duration_ms,
                              byte_size,sha256,status,error_code
                       FROM xhs_post_assets
                       WHERE workspace_id=? AND archive_id=? ORDER BY role,ordinal""",
                    (workspace_id, item["id"]),
                ).fetchall()
                item["assets"] = [dict(asset) for asset in assets]
                items.append(item)
        return {
            "items": items,
            "include_body": include_body,
            "supported": True,
            "read_only": True,
        }

    def tool_list_performance(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 30)
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,task_id,draft_version_id,platform,impressions,reads,completions,
                          interactions,inquiries,contacts,sales,revenue,notes,recorded_at
                   FROM performance_records WHERE workspace_id=? ORDER BY recorded_at DESC LIMIT ?""",
                (workspace_id, limit),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "read_only": True}

    def tool_list_life_cases(self, args: dict[str, Any]) -> dict[str, Any]:
        workspace_id = self._require_workspace(args)
        limit = clamp_limit(args.get("limit"), 20)
        include_content = bool(args.get("include_content", False))
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.id,c.title,c.occurred_at,c.archived,c.sync_ip,c.created_at,c.updated_at,
                          CASE WHEN COUNT(m.id)>0 THEN 1 ELSE 0 END AS has_media,
                          COUNT(m.id) AS media_count,c.note_text
                   FROM life_cases c LEFT JOIN life_case_media m ON m.case_id=c.id
                   WHERE c.workspace_id=? GROUP BY c.id ORDER BY c.created_at DESC LIMIT ?""",
                (workspace_id, limit),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            note = item.pop("note_text", "")
            if include_content:
                item["note_text"] = clean_text(note)
            items.append(item)
        return {"items": items, "include_content": include_content, "read_only": True}


TOOLS = [
    {
        "name": "list_workspaces",
        "description": "列出本机工作台中的品牌工作区。只读，不返回密钥。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_workspace_context",
        "description": "读取一个工作区的品牌资料摘要、资料库和内容任务数量。敏感字段自动脱敏。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}}, "required": ["workspace_id"]},
    },
    {
        "name": "list_content_tasks",
        "description": "读取工作区的文案任务列表，可按状态筛选。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
    {
        "name": "get_task_result",
        "description": "读取一个文案任务的草稿、审稿结果和来源链接。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "task_id": {"type": "string"}}, "required": ["workspace_id", "task_id"]},
    },
    {
        "name": "search_knowledge",
        "description": "在工作区资料库中搜索片段并返回来源。只读，最多返回 100 条。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id", "query"]},
    },
    {
        "name": "list_radar_items",
        "description": "读取内容雷达已经保存的热点和竞品条目，可按平台筛选。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "platform": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
    {
        "name": "list_social_extractions",
        "description": "读取已保存的小红书正文提取状态和互动数据；include_body=true 才返回正文。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "include_body": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
    {
        "name": "list_verified_xhs_archives",
        "description": "读取已经严格归档的小红书正文、图片顺序、哈希和完整性证据；include_body=true 才返回正文。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "include_body": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
    {
        "name": "list_performance",
        "description": "读取发布后的播放、阅读、互动、咨询、成交和收入指标。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
    {
        "name": "list_life_cases",
        "description": "读取生活案例标题、时间和图片数量；include_content=true 才返回案例正文。",
        "inputSchema": {"type": "object", "properties": {"workspace_id": {"type": "string"}, "include_content": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, "required": ["workspace_id"]},
    },
]


def rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": clean_text(message, 500)}}


def write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        store = ReadOnlyStore()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            write_message(rpc_error(None, -32700, "无效 JSON"))
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            params = request.get("params") or {}
            write_message(rpc_result(request_id, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "这是相亲工作台的本机只读数据接口；只能使用固定查询工具，不支持写入或任意 SQL。",
            }))
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            continue
        elif method == "ping":
            write_message(rpc_result(request_id, {}))
        elif method == "tools/list":
            write_message(rpc_result(request_id, {"tools": TOOLS}))
        elif method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name") or "")
            try:
                result = store.call(name, params.get("arguments") or {})
                write_message(rpc_result(request_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}))
            except Exception as exc:
                write_message(rpc_result(request_id, {"isError": True, "content": [{"type": "text", "text": clean_text(str(exc), 500)}]}))
        else:
            if request_id is not None:
                write_message(rpc_error(request_id, -32601, f"方法不支持: {method}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
