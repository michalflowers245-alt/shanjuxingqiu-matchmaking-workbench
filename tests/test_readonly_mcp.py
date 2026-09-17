from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mcp" / "matchmaking_readonly_mcp.py"
SPEC = importlib.util.spec_from_file_location("matchmaking_readonly_mcp", MODULE_PATH)
assert SPEC and SPEC.loader
mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp)


def make_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE workspaces (id TEXT PRIMARY KEY,name TEXT,description TEXT,color TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE brand_profiles (workspace_id TEXT,profile_json TEXT,completed_fields_json TEXT,updated_at TEXT);
            CREATE TABLE libraries (id TEXT,workspace_id TEXT);
            CREATE TABLE documents (id TEXT,workspace_id TEXT);
            CREATE TABLE voice_samples (id TEXT,workspace_id TEXT);
            CREATE TABLE copy_tasks (id TEXT,workspace_id TEXT,content_type TEXT,platform TEXT,topic TEXT,goal TEXT,offer TEXT,status TEXT,stage TEXT,revision_round INTEGER,retry_count INTEGER,last_error TEXT,source_case_id TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE topic_monitor_items (id TEXT,workspace_id TEXT);
            CREATE TABLE xhs_post_extractions (id TEXT,workspace_id TEXT);
            CREATE TABLE life_cases (id TEXT,workspace_id TEXT);
            CREATE TABLE performance_records (id TEXT,workspace_id TEXT);
            CREATE TABLE draft_versions (id TEXT,task_id TEXT,workspace_id TEXT,version INTEGER,origin TEXT,body_text TEXT,is_final INTEGER,created_at TEXT);
            CREATE TABLE editor_reviews (id TEXT,task_id TEXT,round INTEGER,total_score REAL,decision TEXT,change_summary TEXT,created_at TEXT);
            CREATE TABLE research_sources (id TEXT,task_id TEXT,source_key TEXT,kind TEXT,title TEXT,url TEXT,published_at TEXT,excerpt TEXT,credibility REAL,created_at TEXT);
            """
        )
        db.execute("INSERT INTO workspaces VALUES ('ws1','相亲工作区','测试','pink','now','now')")
        db.execute("INSERT INTO brand_profiles VALUES ('ws1','{\"name\":\"柚子皮\",\"api_key\":\"secret\"}','[]','now')")
        db.execute("INSERT INTO copy_tasks VALUES ('task1','ws1','xiaohongshu','小红书','长沙相亲','获客','','FINAL_READY','FINAL',0,0,'',NULL,'now','now')")
        db.commit()


def test_store_is_read_only_and_redacts_secrets(tmp_path: Path):
    path = tmp_path / "copy.sqlite3"
    make_db(path)
    store = mcp.ReadOnlyStore(path, tmp_path / "audit.log")
    context = store.call("get_workspace_context", {"workspace_id": "ws1"})
    assert context["brand_profile"] == {"name": "柚子皮"}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM copy_tasks").fetchone()[0] == 1
    # The read-only connection rejects writes at the SQLite level.
    conn = store._connect()
    try:
        try:
            conn.execute("CREATE TABLE should_not_exist (id TEXT)")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        else:  # pragma: no cover - a regression in the guard
            raise AssertionError("read-only connection accepted a write")
    finally:
        conn.close()
    audit = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "api_key" not in audit
    assert '"tool":"get_workspace_context"' in audit


def test_stdio_protocol_lists_tools(tmp_path: Path):
    path = tmp_path / "copy.sqlite3"
    make_db(path)
    env = {"WORKBENCH_MCP_DB_PATH": str(path), "WORKBENCH_MCP_AUDIT_PATH": str(tmp_path / "audit.log")}
    request = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        ]
    ) + "\n"
    result = subprocess.run(["python3", str(MODULE_PATH)], input=request, text=True, capture_output=True, env={**__import__("os").environ, **env}, check=False)
    assert result.returncode == 0
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[0]["result"]["serverInfo"]["name"] == "matchmaking-workbench-readonly"
    names = {tool["name"] for tool in messages[1]["result"]["tools"]}
    assert "search_knowledge" in names
    assert "get_task_result" in names
