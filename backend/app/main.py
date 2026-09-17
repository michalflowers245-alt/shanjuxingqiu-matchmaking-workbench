from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from .config import ASSET_DIR, EXPORT_DIR, FRONTEND_DIST, RADAR_CAPTURE_DIR, UPLOAD_DIR, settings
from .build_info import BUILD_ID, PROCESS_ID
from .authenticity import build_voice_dna, scan_expression
from .db import database, json_dumps, json_loads, new_id, utc_now
from .knowledge import SUPPORTED_SUFFIXES, knowledge_service
from .life_cases import (
    LifeCaseBusyError,
    LifeCaseCleanupError,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    MAX_TOTAL_BYTES,
    life_case_service,
)
from .monitoring import RadarBusyError, topic_radar_service, topic_radar_worker
from .platform_browser import PlatformBrowserError, platform_browser_collector
from .plugins import plugin_manager
from .prompts import CONTENT_TYPES, REQUIRED_BRAND_FIELDS, adaptive_questions
from .providers import (
    DEEPSEEK_API_BASE_URL,
    DEEPSEEK_PROVIDER,
    DEEPSEEK_PROTOCOL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_LOCAL_PROXY_MODEL,
    DEFAULT_LOCAL_PROXY_URL,
    LOCAL_PROXY_PROTOCOL,
    LOCAL_PROXY_PROVIDER,
    ProviderError,
    model_profile_is_usable,
    model_gateway,
    normalize_local_proxy_url,
    provider_label,
)
from .publishing import PUBLISH_GATE, PublishError, publish_worker, publisher, publishing_enabled
from .schemas import (
    BrandProfileUpdate,
    ConnectorCreate,
    CopyTaskCreate,
    DraftEdit,
    DeepSeekSettingsUpdate,
    ImageProfileCreate,
    InterviewAnswer,
    KnowledgeFolderCreate,
    KnowledgeTextCreate,
    KnowledgeUrlCreate,
    LifeCaseUpdate,
    LocalProxySettingsUpdate,
    DailyIpRadarEnable,
    PlatformBrowserOpen,
    PerformanceCreate,
    PluginInvoke,
    ProfileCreate,
    PublishApprove,
    RetrievalRequest,
    RoleUpdate,
    SearchProfileCreate,
    TopicMonitorUpdate,
    TopicDiscoveryCreate,
    TopicDiscoveryRequest,
    XiaohongshuExtractionRequest,
    WorkflowUpdate,
    WorkspaceCreate,
    WorkspaceUpdate,
    VoiceSampleCreate,
)
from .security import vault
from .skills import skill_registry
from .workflow import WEIGHTS, performance_insights, task_worker, weighted_score, workflow_engine
from .xhs_extractions import xhs_extraction_service


if os.getenv("WORKBENCH_DEBUG_STACKS") == "1":
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1)


topic_radar_service.on_results_ready = xhs_extraction_service.enqueue_results


JSON_FIELDS = {
    "profile_json",
    "completed_fields_json",
    "metadata_json",
    "constraints_json",
    "source_urls_json",
    "packet_json",
    "package_json",
    "scores_json",
    "blocking_issues_json",
    "instructions_json",
    "conversion_structure_json",
    "risks_json",
    "brief_json",
    "config_json",
    "steps_json",
    "frozen_payload_json",
    "result_json",
    "payload_json",
    "findings_json",
    "platforms_json",
    "summary_json",
    "metrics_json",
    "raw_json",
}


def decode_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    for key in list(value):
        if key in JSON_FIELDS:
            value[key.removesuffix("_json")] = json_loads(value.pop(key), {} if key.endswith("_json") else None)
    for key in ("is_gold", "is_final", "web_research", "enabled", "is_default", "is_new"):
        if key in value:
            value[key] = bool(value[key])
    return value


def require_workspace(workspace_id: str) -> dict[str, Any]:
    row = database.one("SELECT * FROM workspaces WHERE id=?", (workspace_id,))
    if not row:
        raise HTTPException(404, "品牌工作区不存在")
    return row


def redact_profile(row: dict[str, Any]) -> dict[str, Any]:
    clean = decode_row(row) or {}
    has_secret = bool(clean.get("secret_ref"))
    clean.pop("secret_ref", None)
    clean["has_secret"] = has_secret
    clean["is_local_proxy"] = (
        clean.get("provider") == LOCAL_PROXY_PROVIDER and clean.get("protocol") == LOCAL_PROXY_PROTOCOL
    )
    clean["is_deepseek"] = (
        clean.get("provider") == DEEPSEEK_PROVIDER and clean.get("protocol") == DEEPSEEK_PROTOCOL
    )
    clean["provider_label"] = provider_label(clean.get("provider"))
    return clean


def decode_radar_run(row: dict[str, Any] | None) -> dict[str, Any]:
    clean = decode_row(row) or {}
    clean["items"] = [decode_row(item) or {} for item in (row or {}).get("items", [])]
    return clean


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database.initialize()
    database.recover_jobs()
    topic_radar_service.recover_interrupted_runs()
    skill_registry.reload()
    plugin_manager.reload()
    if os.getenv("WORKBENCH_DISABLE_WORKERS") != "1":
        task_worker.start()
        publish_worker.start()
        xhs_extraction_service.start()
        topic_radar_worker.start()
    yield
    task_worker.stop()
    publish_worker.stop()
    topic_radar_worker.stop()
    xhs_extraction_service.stop()


app = FastAPI(title="AI 文案工作台", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                headers["Cache-Control"] = "no-store" if scope.get("path", "").startswith("/api/") else "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(ProviderError)
async def provider_error_handler(_: Request, exc: ProviderError):
    status = 400 if exc.category == "model" else 503
    return JSONResponse({"detail": str(exc), "code": exc.code, "category": exc.category}, status_code=status)


@app.exception_handler(PublishError)
async def publish_error_handler(_: Request, exc: PublishError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "database": str(database.path.name),
        "publishing_enabled": publishing_enabled(),
        "build_id": BUILD_ID,
        "process_id": PROCESS_ID,
    }


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    workspaces = [decode_row(row) for row in database.all("SELECT * FROM workspaces ORDER BY updated_at DESC")]
    raw_models = database.all("SELECT * FROM model_profiles ORDER BY is_default DESC,name")
    models = [redact_profile(row) for row in raw_models]
    active_model = next((row for row in models if row.get("is_default")), None)
    active_private_model = next((row for row in raw_models if row.get("is_default")), None)
    return {
        "workspaces": workspaces,
        "content_types": [{"key": key, **value} for key, value in CONTENT_TYPES.items()],
        "model_status": {
            "profiles": models,
            "usable": model_profile_is_usable(active_private_model),
            "status": (active_model or {}).get("verification_status", "unverified"),
            "search_status": (active_model or {}).get("search_status", "unverified"),
            "search_mode": (active_model or {}).get("search_mode", "unavailable"),
            "message": (active_model or {}).get("last_error_message"),
            "active_provider": (active_model or {}).get("provider"),
            "active_model": (active_model or {}).get("model"),
            "active_label": provider_label((active_model or {}).get("provider")),
        },
        "skills": skill_registry.list(),
        "publishing_enabled": publishing_enabled(),
    }


def _default_model_profile() -> dict[str, Any] | None:
    return database.one(
        "SELECT * FROM model_profiles WHERE workspace_id IS NULL AND is_default=1 ORDER BY updated_at DESC LIMIT 1"
    )


def _local_proxy_profile() -> dict[str, Any] | None:
    return database.one(
        """SELECT * FROM model_profiles WHERE workspace_id IS NULL AND provider=? AND protocol=?
        ORDER BY CASE WHEN id='model_local_proxy_default' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1""",
        (LOCAL_PROXY_PROVIDER, LOCAL_PROXY_PROTOCOL),
    )


def _deepseek_profile() -> dict[str, Any] | None:
    return database.one(
        """SELECT * FROM model_profiles WHERE workspace_id IS NULL AND provider=? AND protocol=?
        ORDER BY CASE WHEN id='model_deepseek_default' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1""",
        (DEEPSEEK_PROVIDER, DEEPSEEK_PROTOCOL),
    )


def _save_proxy_check(profile_id: str, check: dict[str, Any]) -> None:
    database.execute(
        """UPDATE model_profiles SET verification_status=?,last_checked_at=?,last_error_code=NULL,last_error_message=NULL,
        search_status=?,search_mode=?,updated_at=? WHERE id=?""",
        (
            check["model"]["status"], check["checked_at"], check["search"]["status"],
            check["search"]["mode"], utc_now(), profile_id,
        ),
    )


def _local_proxy_settings_result() -> dict[str, Any]:
    row = _local_proxy_profile()
    if not row:
        return {
            "model": {"status": "unverified", "model": DEFAULT_LOCAL_PROXY_MODEL, "message": "尚未配置"},
            "base_url": DEFAULT_LOCAL_PROXY_URL,
            "search": {"status": "unverified", "mode": "unavailable", "message": "尚未检测"},
            "checked_at": None,
        }
    profile = redact_profile(row)
    return {
        "model": {
            "status": profile.get("verification_status") or "unverified",
            "model": profile.get("model") or DEFAULT_LOCAL_PROXY_MODEL,
            "message": profile.get("last_error_message") or ("本机代理模型生成正常" if profile.get("verification_status") == "verified" else "请测试并保存"),
        },
        "base_url": profile.get("base_url") or DEFAULT_LOCAL_PROXY_URL,
        "search": {
            "status": profile.get("search_status") or "unverified",
            "mode": profile.get("search_mode") or "unavailable",
            "message": "RSS 与资料库来源正常" if profile.get("search_status") == "fallback" else "尚未检测",
        },
        "checked_at": profile.get("last_checked_at"),
    }


@app.put("/api/settings/local-proxy")
def save_local_proxy_settings(payload: LocalProxySettingsUpdate) -> dict[str, Any]:
    current = _local_proxy_profile()
    try:
        base_url = normalize_local_proxy_url(payload.base_url)
        check = model_gateway.test_proxy(base_url, payload.model)
    except ProviderError as exc:
        if current:
            now = utc_now()
            database.execute(
                """UPDATE model_profiles SET verification_status='error',last_checked_at=?,last_error_code=?,last_error_message=?,
                search_status='unverified',search_mode='unavailable',updated_at=? WHERE id=?""",
                (now, exc.code, str(exc)[:300], now, current["id"]),
            )
        raise
    now = utc_now()
    # Switching providers only changes the selected global default.  It does
    # not erase a separately configured DeepSeek profile or employee choice.
    database.execute("UPDATE model_profiles SET is_default=0 WHERE workspace_id IS NULL")
    if current:
        database.execute(
            """UPDATE model_profiles SET name='本机 OpenAI 代理',provider=?,protocol=?,base_url=?,model=?,
            secret_ref=NULL,temperature=0.7,max_tokens=6000,is_default=1,updated_at=? WHERE id=?""",
            (LOCAL_PROXY_PROVIDER, LOCAL_PROXY_PROTOCOL, base_url, payload.model.strip(), now, current["id"]),
        )
        profile_id = current["id"]
    else:
        profile_id = "model_local_proxy_default"
        database.execute(
            """INSERT INTO model_profiles
            (id,workspace_id,name,provider,protocol,base_url,model,secret_ref,temperature,max_tokens,is_default,
             verification_status,last_checked_at,last_error_code,last_error_message,search_status,search_mode,created_at,updated_at)
            VALUES(?,NULL,'本机 OpenAI 代理',?,?,?,?,NULL,0.7,6000,1,'unverified',NULL,NULL,NULL,'unverified','unavailable',?,?)""",
            (profile_id, LOCAL_PROXY_PROVIDER, LOCAL_PROXY_PROTOCOL, base_url, payload.model.strip(), now, now),
        )
    _save_proxy_check(profile_id, check)
    return check


@app.post("/api/settings/local-proxy/recheck")
def recheck_local_proxy_settings() -> dict[str, Any]:
    current = _local_proxy_profile()
    if not current:
        raise ProviderError("还没有保存本机 OpenAI 代理配置", category="model", code="not_configured")
    try:
        check = model_gateway.test_proxy(str(current.get("base_url") or ""), str(current.get("model") or DEFAULT_LOCAL_PROXY_MODEL))
    except ProviderError as exc:
        database.execute(
            """UPDATE model_profiles SET verification_status='error',last_checked_at=?,last_error_code=?,last_error_message=?,
            search_status='unverified',search_mode='unavailable',updated_at=? WHERE id=?""",
            (utc_now(), exc.code, str(exc)[:300], utc_now(), current["id"]),
        )
        raise
    _save_proxy_check(current["id"], check)
    return check


@app.get("/api/settings/local-proxy")
def get_local_proxy_settings() -> dict[str, Any]:
    return _local_proxy_settings_result()


def _deepseek_settings_result(check: dict[str, Any] | None = None) -> dict[str, Any]:
    row = _deepseek_profile()
    if not row:
        return {
            "model": {"status": "unverified", "model": DEFAULT_DEEPSEEK_MODEL, "message": "尚未连接"},
            "base_url": DEEPSEEK_API_BASE_URL,
            "has_api_key": False,
            "configured": False,
            "active": False,
            "checked_at": None,
        }
    profile = redact_profile(row)
    result: dict[str, Any] = {
        "model": {
            "status": profile.get("verification_status") or "unverified",
            "model": profile.get("model") or DEFAULT_DEEPSEEK_MODEL,
            "message": profile.get("last_error_message") or (
                "DeepSeek 模型生成正常" if profile.get("verification_status") == "verified" else "请填写 Key 后测试"
            ),
        },
        "base_url": DEEPSEEK_API_BASE_URL,
        "has_api_key": bool(profile.get("has_secret")),
        "configured": bool(profile.get("has_secret")),
        "active": bool(profile.get("is_default")),
        "checked_at": profile.get("last_checked_at"),
    }
    if check:
        if check.get("model", {}).get("latency_ms") is not None:
            result["model"]["latency_ms"] = check["model"]["latency_ms"]
        result["search"] = check.get("search")
    return result


def _record_model_check_error(profile_id: str, exc: ProviderError) -> None:
    now = utc_now()
    database.execute(
        """UPDATE model_profiles SET verification_status='error',last_checked_at=?,last_error_code=?,last_error_message=?,
        search_status='unverified',search_mode='unavailable',updated_at=? WHERE id=?""",
        (now, exc.code, str(exc)[:300], now, profile_id),
    )


@app.get("/api/settings/deepseek")
def get_deepseek_settings() -> dict[str, Any]:
    return _deepseek_settings_result()


@app.put("/api/settings/deepseek")
def save_deepseek_settings(payload: DeepSeekSettingsUpdate) -> dict[str, Any]:
    """Test first, then save the Keychain reference and activate DeepSeek."""
    current = _deepseek_profile()
    submitted_key = payload.api_key
    api_key = submitted_key or vault.get((current or {}).get("secret_ref"))
    if not api_key:
        raise ProviderError(
            "请填写 DeepSeek API Key；已保存过的 Key 可以留空重新检测",
            category="model", code="missing_deepseek_key",
        )
    try:
        check = model_gateway.test_deepseek(api_key, payload.model)
    except ProviderError as exc:
        if current:
            _record_model_check_error(current["id"], exc)
        raise

    if submitted_key:
        try:
            secret_ref = vault.put(api_key, "model:deepseek:default")
        except (RuntimeError, ValueError) as exc:
            raise ProviderError(str(exc), category="model", code="secret_store_failed") from exc
    else:
        secret_ref = str((current or {}).get("secret_ref") or "")
    if not secret_ref:
        raise ProviderError("DeepSeek API Key 未能保存到本机钥匙串", category="model", code="secret_store_failed")

    now = utc_now()
    profile_id = current["id"] if current else "model_deepseek_default"
    with database.transaction() as connection:
        connection.execute("UPDATE model_profiles SET is_default=0 WHERE workspace_id IS NULL")
        if current:
            connection.execute(
                """UPDATE model_profiles SET name='DeepSeek API',provider=?,protocol=?,base_url=?,model=?,secret_ref=?,
                temperature=0.7,max_tokens=6000,is_default=1,updated_at=? WHERE id=?""",
                (DEEPSEEK_PROVIDER, DEEPSEEK_PROTOCOL, DEEPSEEK_API_BASE_URL, payload.model, secret_ref, now, profile_id),
            )
        else:
            connection.execute(
                """INSERT INTO model_profiles
                (id,workspace_id,name,provider,protocol,base_url,model,secret_ref,temperature,max_tokens,is_default,
                 verification_status,last_checked_at,last_error_code,last_error_message,search_status,search_mode,created_at,updated_at)
                VALUES(?,NULL,'DeepSeek API',?,?,?,?,?,0.7,6000,1,'unverified',NULL,NULL,NULL,'unverified','unavailable',?,?)""",
                (profile_id, DEEPSEEK_PROVIDER, DEEPSEEK_PROTOCOL, DEEPSEEK_API_BASE_URL, payload.model, secret_ref, now, now),
            )
    _save_proxy_check(profile_id, check)
    return _deepseek_settings_result(check)


@app.post("/api/settings/deepseek/recheck")
def recheck_deepseek_settings() -> dict[str, Any]:
    current = _deepseek_profile()
    if not current:
        raise ProviderError("还没有保存 DeepSeek 配置", category="model", code="not_configured")
    api_key = vault.get(current.get("secret_ref"))
    if not api_key:
        error = ProviderError(
            "DeepSeek API Key 未在本机钥匙串中找到，请重新填写并测试",
            category="model", code="missing_deepseek_key",
        )
        _record_model_check_error(current["id"], error)
        raise error
    try:
        check = model_gateway.test_deepseek(api_key, str(current.get("model") or DEFAULT_DEEPSEEK_MODEL))
    except ProviderError as exc:
        _record_model_check_error(current["id"], exc)
        raise
    _save_proxy_check(current["id"], check)
    return _deepseek_settings_result(check)


@app.get("/api/skills")
def list_skills() -> list[dict[str, Any]]:
    return skill_registry.list()


@app.get("/api/workspaces")
def list_workspaces() -> list[dict[str, Any]]:
    return [decode_row(row) or {} for row in database.all("SELECT * FROM workspaces ORDER BY updated_at DESC")]


@app.post("/api/workspaces", status_code=201)
def create_workspace(payload: WorkspaceCreate) -> dict[str, Any]:
    return decode_row(database.create_workspace(payload.name, payload.description)) or {}


@app.patch("/api/workspaces/{workspace_id}")
def update_workspace(workspace_id: str, payload: WorkspaceUpdate) -> dict[str, Any]:
    require_workspace(workspace_id)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return decode_row(database.one("SELECT * FROM workspaces WHERE id=?", (workspace_id,))) or {}
    fields["updated_at"] = utc_now()
    clause = ",".join(f"{key}=?" for key in fields)
    database.execute(f"UPDATE workspaces SET {clause} WHERE id=?", (*fields.values(), workspace_id))
    return decode_row(database.one("SELECT * FROM workspaces WHERE id=?", (workspace_id,))) or {}


@app.delete("/api/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str):
    require_workspace(workspace_id)
    uploaded = database.all("SELECT source_uri FROM documents WHERE workspace_id=? AND source_type='file'", (workspace_id,))
    local_assets = database.all("SELECT local_path FROM assets WHERE workspace_id=? AND local_path IS NOT NULL", (workspace_id,))
    refs = database.all(
        """SELECT secret_ref FROM model_profiles WHERE workspace_id=?
        UNION ALL SELECT secret_ref FROM search_profiles WHERE workspace_id=?
        UNION ALL SELECT secret_ref FROM image_profiles WHERE workspace_id=?
        UNION ALL SELECT secret_ref FROM connector_profiles WHERE workspace_id=?
        UNION ALL SELECT douyin_secret_ref FROM radar_platform_settings WHERE workspace_id=?""",
        (workspace_id, workspace_id, workspace_id, workspace_id, workspace_id),
    )
    for row in refs:
        vault.delete(row.get("secret_ref"))
    life_case_service.delete_workspace_files(workspace_id)
    database.execute("DELETE FROM chunks_fts WHERE workspace_id=?", (workspace_id,))
    database.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
    for row in uploaded:
        path = (UPLOAD_DIR / str(row.get("source_uri") or "")).resolve()
        if path.parent == UPLOAD_DIR.resolve() and path.is_file():
            path.unlink()
    for row in local_assets:
        path = Path(str(row.get("local_path") or "")).resolve()
        if ASSET_DIR.resolve() in path.parents and path.is_file():
            path.unlink()
    return None


@app.get("/api/workspaces/{workspace_id}/profile")
def get_profile(workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    row = database.one("SELECT * FROM brand_profiles WHERE workspace_id=?", (workspace_id,))
    return decode_row(row) or {}


@app.patch("/api/workspaces/{workspace_id}/profile")
def update_profile(workspace_id: str, payload: BrandProfileUpdate) -> dict[str, Any]:
    require_workspace(workspace_id)
    row = database.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,)) or {}
    profile = json_loads(row.get("profile_json"), {}) or {}
    for key, value in payload.fields.items():
        if value in (None, "", [], {}):
            profile.pop(key, None)
        else:
            profile[key] = value
    completed = [item["key"] for item in REQUIRED_BRAND_FIELDS if profile.get(item["key"])]
    database.execute(
        "UPDATE brand_profiles SET profile_json=?,completed_fields_json=?,updated_at=? WHERE workspace_id=?",
        (json_dumps(profile), json_dumps(completed), utc_now(), workspace_id),
    )
    database.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (utc_now(), workspace_id))
    if "voice" in payload.fields:
        _refresh_voice_dna(workspace_id)
    return get_profile(workspace_id)


def _refresh_voice_dna(workspace_id: str) -> dict[str, Any]:
    rows = database.all("SELECT content FROM voice_samples WHERE workspace_id=? ORDER BY created_at", (workspace_id,))
    profile_row = database.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,)) or {}
    profile = json_loads(profile_row.get("profile_json"), {}) or {}
    samples = [str(row["content"]) for row in rows]
    dna = build_voice_dna(samples, str(profile.get("voice") or ""))
    profile["voice_dna"] = dna
    profile["voice_samples"] = f"已保存 {len(samples)} 份本人表达样本" if samples else ""
    completed = [item["key"] for item in REQUIRED_BRAND_FIELDS if profile.get(item["key"])]
    database.execute(
        "UPDATE brand_profiles SET profile_json=?,completed_fields_json=?,updated_at=? WHERE workspace_id=?",
        (json_dumps(profile), json_dumps(completed), utc_now(), workspace_id),
    )
    return dna


@app.get("/api/workspaces/{workspace_id}/interview")
def next_interview_question(
    workspace_id: str,
    topic: str = "",
    goal: str = "",
    offer: str = "",
    content_type: str = "",
) -> dict[str, Any]:
    profile = get_profile(workspace_id).get("profile") or {}
    missing = adaptive_questions(profile, topic=topic, goal=goal, offer=offer, content_type=content_type)
    core = [item for item in missing if item.get("priority") == "core"]
    known = sum(bool(profile.get(item["key"])) for item in REQUIRED_BRAND_FIELDS)
    quality_score = round(known / len(REQUIRED_BRAND_FIELDS) * 100)
    return {
        "complete": not missing,
        "ready_to_write": not core,
        "next": missing[0] if missing else None,
        "missing": missing,
        "progress": quality_score,
        "readiness": {
            "known": known,
            "total": len(REQUIRED_BRAND_FIELDS),
            "core_missing": len(core),
            "message": "关键信息已够，可以开写" if not core else f"还缺 {len(core)} 条会直接影响本篇质量的关键信息",
        },
    }


@app.post("/api/workspaces/{workspace_id}/interview")
def answer_interview(workspace_id: str, payload: InterviewAnswer) -> dict[str, Any]:
    allowed = {item["key"] for item in REQUIRED_BRAND_FIELDS}
    if payload.field not in allowed:
        raise HTTPException(400, "未知访谈字段")
    row = database.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,)) or {}
    profile = json_loads(row.get("profile_json"), {}) or {}
    skipped = set(profile.get("_skipped_fields") or [])
    if payload.skip:
        skipped.add(payload.field)
        update_profile(workspace_id, BrandProfileUpdate(fields={"_skipped_fields": sorted(skipped)}))
    elif payload.field == "voice_samples":
        content = str(payload.answer or "").strip()
        if len(content) < 20:
            raise HTTPException(400, "表达样本至少需要20个字")
        database.execute(
            "INSERT INTO voice_samples(id,workspace_id,label,content,source_type,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("voice"), workspace_id, "访谈表达样本", content, "interview", utc_now()),
        )
        skipped.discard(payload.field)
        update_profile(workspace_id, BrandProfileUpdate(fields={"_skipped_fields": sorted(skipped)}))
        _refresh_voice_dna(workspace_id)
    else:
        skipped.discard(payload.field)
        update_profile(
            workspace_id,
            BrandProfileUpdate(fields={payload.field: payload.answer, "_skipped_fields": sorted(skipped)}),
        )
    return next_interview_question(workspace_id)


@app.get("/api/workspaces/{workspace_id}/voice-samples")
def list_voice_samples(workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    rows = database.all(
        "SELECT id,label,source_type,length(content) AS characters,created_at FROM voice_samples WHERE workspace_id=? ORDER BY created_at DESC",
        (workspace_id,),
    )
    profile = get_profile(workspace_id).get("profile") or {}
    return {"samples": rows, "voice_dna": profile.get("voice_dna") or build_voice_dna([], str(profile.get("voice") or ""))}


@app.post("/api/workspaces/{workspace_id}/voice-samples")
def add_voice_sample(workspace_id: str, payload: VoiceSampleCreate) -> dict[str, Any]:
    require_workspace(workspace_id)
    sample_id = new_id("voice")
    database.execute(
        "INSERT INTO voice_samples(id,workspace_id,label,content,source_type,created_at) VALUES(?,?,?,?,?,?)",
        (sample_id, workspace_id, payload.label, payload.content, payload.source_type, utc_now()),
    )
    dna = _refresh_voice_dna(workspace_id)
    return {"id": sample_id, "voice_dna": dna}


@app.delete("/api/workspaces/{workspace_id}/voice-samples/{sample_id}", status_code=204)
def delete_voice_sample(workspace_id: str, sample_id: str):
    require_workspace(workspace_id)
    database.execute("DELETE FROM voice_samples WHERE id=? AND workspace_id=?", (sample_id, workspace_id))
    _refresh_voice_dna(workspace_id)
    return None


@app.post("/api/expression-check")
def check_expression(payload: dict[str, Any]) -> dict[str, Any]:
    return scan_expression(str(payload.get("text") or ""), str(payload.get("content_type") or "wechat_article"))


@app.get("/api/knowledge/libraries")
def list_libraries(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return database.all("SELECT * FROM libraries WHERE workspace_id=? ORDER BY created_at", (workspace_id,))


@app.get("/api/knowledge/documents")
def list_documents(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    rows = database.all(
        """SELECT d.*,COUNT(c.id) AS chunk_count FROM documents d
        LEFT JOIN chunks c ON c.document_id=d.id WHERE d.workspace_id=?
        GROUP BY d.id ORDER BY d.updated_at DESC""",
        (workspace_id,),
    )
    return [decode_row(row) or {} for row in rows]


@app.post("/api/knowledge/text", status_code=201)
def add_knowledge_text(payload: KnowledgeTextCreate) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    return decode_row(
        knowledge_service.import_text(
            payload.workspace_id, payload.title, payload.content, payload.source_type, "", payload.library_id, "text/plain", payload.is_gold
        )
    ) or {}


@app.post("/api/knowledge/url", status_code=201)
def add_knowledge_url(payload: KnowledgeUrlCreate) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    return decode_row(knowledge_service.import_url(payload.workspace_id, str(payload.url), payload.library_id, payload.is_gold)) or {}


@app.post("/api/knowledge/folder", status_code=201)
def add_knowledge_folder(payload: KnowledgeFolderCreate) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    rows = knowledge_service.import_folder(payload.workspace_id, payload.path, payload.library_id)
    return {"imported": len(rows), "documents": [decode_row(row) for row in rows]}


@app.post("/api/knowledge/upload", status_code=201)
async def upload_knowledge(
    workspace_id: str = Form(...),
    library_id: str | None = Form(None),
    is_gold: bool = Form(False),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    require_workspace(workspace_id)
    imported: list[dict[str, Any]] = []
    for item in files:
        data = await item.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"文件 {item.filename} 超过 30MB")
        imported.extend(knowledge_service.import_bytes(workspace_id, item.filename or "document.txt", data, library_id, is_gold))
    return {"imported": len(imported), "documents": [decode_row(row) for row in imported]}


@app.post("/api/knowledge/retrieve")
def retrieve_knowledge(payload: RetrievalRequest) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    return {"results": knowledge_service.retrieve(payload.workspace_id, payload.query, payload.limit, payload.gold_only)}


@app.post("/api/knowledge/embeddings/rebuild")
def rebuild_embeddings(workspace_id: str) -> dict[str, int]:
    require_workspace(workspace_id)
    return knowledge_service.build_embeddings(workspace_id)


@app.patch("/api/knowledge/documents/{document_id}/gold")
def mark_gold(document_id: str, enabled: bool) -> dict[str, Any]:
    database.execute("UPDATE documents SET is_gold=?,updated_at=? WHERE id=?", (int(enabled), utc_now(), document_id))
    row = database.one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not row:
        raise HTTPException(404, "资料不存在")
    return decode_row(row) or {}


@app.delete("/api/knowledge/documents/{document_id}", status_code=204)
def delete_document(document_id: str):
    row = database.one("SELECT id FROM documents WHERE id=?", (document_id,))
    if not row:
        raise HTTPException(404, "资料不存在")
    chunk_rows = database.all("SELECT id FROM chunks WHERE document_id=?", (document_id,))
    for chunk in chunk_rows:
        database.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk["id"],))
    database.execute("DELETE FROM documents WHERE id=?", (document_id,))
    return None


@app.post("/api/life-cases", status_code=201)
async def create_life_case(
    workspace_id: str = Form(...),
    note_text: str = Form(...),
    occurred_at: str | None = Form(None),
    sync_ip: bool = Form(True),
    images: list[UploadFile] = File(...),
) -> dict[str, Any]:
    require_workspace(workspace_id)
    if not 1 <= len(images) <= MAX_IMAGE_COUNT:
        raise HTTPException(422, "每个生活案例需要上传 1—9 张图片")
    uploads: list[tuple[str | None, bytes]] = []
    total = 0
    try:
        for image in images:
            data = await image.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(422, "单张图片不能超过 12MB")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(422, "全部图片合计不能超过 30MB")
            uploads.append((image.filename, data))
    finally:
        for image in images:
            await image.close()
    try:
        return life_case_service.create(
            workspace_id=workspace_id,
            note_text=note_text,
            occurred_at=occurred_at,
            sync_ip=sync_ip,
            images=uploads,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/life-cases")
def list_life_cases(workspace_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return life_case_service.list(workspace_id, include_archived=include_archived)


@app.get("/api/life-cases/{case_id}")
def get_life_case(case_id: str, workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    try:
        return life_case_service.get(case_id, workspace_id)
    except KeyError as exc:
        raise HTTPException(404, "生活案例不存在") from exc


@app.patch("/api/life-cases/{case_id}")
def update_life_case(case_id: str, payload: LifeCaseUpdate, workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    try:
        return life_case_service.set_archived(case_id, payload.archived, workspace_id)
    except KeyError as exc:
        raise HTTPException(404, "生活案例不存在") from exc


@app.delete("/api/life-cases/{case_id}", status_code=204)
def delete_life_case(case_id: str, workspace_id: str):
    require_workspace(workspace_id)
    try:
        life_case_service.delete(case_id, workspace_id)
    except KeyError as exc:
        raise HTTPException(404, "生活案例不存在") from exc
    except LifeCaseBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LifeCaseCleanupError as exc:
        raise HTTPException(500, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, "本地原图正在被占用，案例尚未删除，请稍后重试") from exc
    return None


@app.get("/api/life-cases/{case_id}/media/{media_id}")
def get_life_case_media(case_id: str, media_id: str, workspace_id: str):
    require_workspace(workspace_id)
    try:
        path, media = life_case_service.original_path(case_id, media_id, workspace_id)
    except KeyError as exc:
        raise HTTPException(404, "生活图片不存在") from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, "本地原图已丢失") from exc
    return FileResponse(
        path,
        media_type=str(media["mime_type"]),
        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/life-cases/{case_id}/generate", status_code=202)
def generate_life_case(case_id: str, workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    try:
        life_case = life_case_service.get(case_id, workspace_id)
    except KeyError as exc:
        raise HTTPException(404, "生活案例不存在") from exc
    payload = CopyTaskCreate(
        workspace_id=life_case["workspace_id"],
        content_type="moments",
        platform="微信朋友圈",
        topic=life_case["title"],
        goal="记录真实生活，生成可直接发布的朋友圈文案",
        offer="",
        constraints={
            "product_mode": "moments_life_case",
            "sync_ip": life_case["sync_ip"],
            "target_length": [80, 500],
            "output_angles": ["daily", "reflection", "soft_business"],
        },
        source_urls=[],
        web_research=False,
    )
    wait_for_model = not model_gateway.profile_is_usable(life_case["workspace_id"], "researcher")
    return _create_task_record(payload, source_case_id=case_id, wait_for_model=wait_for_model)


def _create_task_record(
    payload: CopyTaskCreate,
    *,
    source_case_id: str | None = None,
    wait_for_model: bool = False,
) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    target_length = payload.constraints.get("target_length")
    if target_length is not None:
        valid_length = (
            isinstance(target_length, list)
            and len(target_length) == 2
            and all(isinstance(item, (int, float)) and 30 <= int(item) <= 20000 for item in target_length)
            and int(target_length[0]) <= int(target_length[1])
        )
        if not valid_length:
            raise HTTPException(422, "目标字数必须是 30—20000 之间的有效范围")
    task_id, now = new_id("task"), utc_now()
    status = "NEEDS_MODEL" if wait_for_model else "QUEUED"
    last_error = "生活案例和原图已保存；连接本机代理后可从研究员继续" if wait_for_model else None
    database.execute(
        """INSERT INTO copy_tasks
        (id,workspace_id,content_type,platform,topic,goal,offer,constraints_json,source_urls_json,web_research,
         status,stage,source_case_id,last_error,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,? ,?,'RESEARCHER',?,?,?,?)""",
        (
            task_id, payload.workspace_id, payload.content_type, payload.platform, payload.topic, payload.goal,
            payload.offer, json_dumps(payload.constraints), json_dumps([str(url) for url in payload.source_urls]),
            int(payload.web_research), status, source_case_id, last_error, now, now,
        ),
    )
    message = "生活案例已保存，等待连接模型" if wait_for_model else "任务已进入三员工文案流程"
    event_type = "NEEDS_MODEL" if wait_for_model else "TASK_CREATED"
    database.emit(task_id, payload.workspace_id, "RESEARCHER", event_type, message)
    if not wait_for_model:
        task_worker.wake()
    return get_task(task_id)


@app.post("/api/copy-tasks", status_code=202)
def create_copy_task(payload: CopyTaskCreate) -> dict[str, Any]:
    return _create_task_record(payload)


@app.get("/api/copy-tasks")
def list_copy_tasks(workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    rows = database.all(
        """SELECT t.*,
        (SELECT total_score FROM editor_reviews r WHERE r.task_id=t.id ORDER BY r.created_at DESC LIMIT 1) AS latest_score
        FROM copy_tasks t WHERE t.workspace_id=? ORDER BY t.created_at DESC LIMIT ?""",
        (workspace_id, min(limit, 200)),
    )
    return [decode_row(row) or {} for row in rows]


@app.get("/api/copy-tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "文案任务不存在")
    packet = database.one("SELECT * FROM research_packets WHERE task_id=?", (task_id,))
    sources = database.all("SELECT * FROM research_sources WHERE task_id=? ORDER BY source_key", (task_id,))
    drafts = database.all("SELECT * FROM draft_versions WHERE task_id=? ORDER BY version DESC", (task_id,))
    reviews = database.all("SELECT * FROM editor_reviews WHERE task_id=? ORDER BY created_at DESC", (task_id,))
    events = database.all("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,))
    assets = database.all("SELECT * FROM assets WHERE task_id=? ORDER BY created_at DESC", (task_id,))
    artifacts = database.all("SELECT * FROM task_artifacts WHERE task_id=? ORDER BY created_at", (task_id,))
    expression_checks = database.all("SELECT * FROM expression_checks WHERE task_id=? ORDER BY created_at", (task_id,))
    source_case = None
    if task.get("source_case_id"):
        try:
            source_case = life_case_service.get(str(task["source_case_id"]), str(task["workspace_id"]))
        except KeyError:
            source_case = None
    return {
        "task": decode_row(task),
        "source_case": source_case,
        "research": decode_row(packet),
        "sources": [decode_row(row) for row in sources],
        "drafts": [decode_row(row) for row in drafts],
        "reviews": [decode_row(row) for row in reviews],
        "events": [decode_row(row) for row in events],
        "assets": [decode_row(row) for row in assets],
        "artifacts": [decode_row(row) for row in artifacts],
        "expression_checks": [decode_row(row) for row in expression_checks],
    }


@app.post("/api/copy-tasks/{task_id}/regenerate", status_code=202)
def regenerate_task(task_id: str) -> dict[str, Any]:
    source = database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
    if not source:
        raise HTTPException(404, "文案任务不存在")
    constraints = json_loads(source.get("constraints_json"), {}) or {}
    if bool(constraints.get("source_case_deleted")):
        raise HTTPException(409, "原生活案例已经永久删除，无法携带原图重新生成")
    life_mode = str(constraints.get("product_mode") or "") == "moments_life_case"
    source_case_id = str(source.get("source_case_id") or "") or None
    if life_mode and not source_case_id:
        raise HTTPException(409, "原生活案例已经永久删除，无法携带原图重新生成")
    if source_case_id:
        try:
            life_case = life_case_service.get(source_case_id, str(source["workspace_id"]))
        except KeyError as exc:
            raise HTTPException(409, "关联的生活案例已经不存在") from exc
    try:
        payload = CopyTaskCreate(
            workspace_id=source["workspace_id"],
            content_type=source["content_type"],
            platform=source["platform"],
            topic=source["topic"],
            goal=source["goal"],
            offer=source["offer"],
            constraints=constraints,
            source_urls=json_loads(source.get("source_urls_json"), []) or [],
            web_research=bool(source["web_research"]),
        )
    except ValueError as exc:
        raise HTTPException(409, "旧任务配置已失效，无法重新生成") from exc
    wait_for_model = life_mode and not model_gateway.profile_is_usable(source["workspace_id"], "researcher")
    return _create_task_record(payload, source_case_id=source_case_id, wait_for_model=wait_for_model)


@app.post("/api/copy-tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    task = database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] in {"FINAL_READY", "FAILED", "CANCELLED"}:
        return decode_row(task) or {}
    database.execute("UPDATE copy_tasks SET status='CANCELLED',stage='CANCELLED',lease_until=NULL,updated_at=? WHERE id=?", (utc_now(), task_id))
    database.emit(task_id, task["workspace_id"], "CANCELLED", "CANCELLED", "任务已取消")
    return decode_row(database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))) or {}


@app.post("/api/copy-tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict[str, Any]:
    task = database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "任务不存在")
    constraints = json_loads(task.get("constraints_json"), {}) or {}
    if bool(constraints.get("source_case_deleted")):
        raise HTTPException(409, "原生活案例已经永久删除，任务无法继续；已生成文案仍会保留")
    database.execute("UPDATE copy_tasks SET status='QUEUED',retry_count=0,lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?", (utc_now(), task_id))
    database.emit(task_id, task["workspace_id"], task["stage"], "MANUAL_RETRY", "用户已重新启动任务")
    task_worker.wake()
    return decode_row(database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))) or {}


@app.post("/api/copy-tasks/{task_id}/continue-without-search")
def continue_without_search(task_id: str) -> dict[str, Any]:
    task = database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "任务不存在")
    database.execute(
        "UPDATE copy_tasks SET web_research=0,status='QUEUED',stage='RESEARCHER',lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?",
        (utc_now(), task_id),
    )
    database.emit(task_id, task["workspace_id"], "RESEARCHER", "SEARCH_SKIPPED", "已改为仅使用现有资料继续")
    task_worker.wake()
    return decode_row(database.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))) or {}


@app.get("/api/research/{task_id}")
def get_research(task_id: str) -> dict[str, Any]:
    detail = get_task(task_id)
    return {"research": detail["research"], "sources": detail["sources"]}


@app.get("/api/drafts")
def list_drafts(workspace_id: str, content_type: str | None = None, final_only: bool = False) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    clauses, params = ["d.workspace_id=?"], [workspace_id]
    if content_type:
        clauses.append("t.content_type=?")
        params.append(content_type)
    if final_only:
        clauses.append("d.is_final=1")
    rows = database.all(
        f"""SELECT d.*,t.topic,t.platform,t.content_type,t.status
        FROM draft_versions d JOIN copy_tasks t ON t.id=d.task_id
        WHERE {' AND '.join(clauses)} ORDER BY d.created_at DESC""",
        params,
    )
    return [decode_row(row) or {} for row in rows]


@app.post("/api/drafts/{draft_id}/edit", status_code=201)
def edit_draft(draft_id: str, payload: DraftEdit) -> dict[str, Any]:
    parent = database.one("SELECT * FROM draft_versions WHERE id=?", (draft_id,))
    if not parent:
        raise HTTPException(404, "稿件版本不存在")
    max_row = database.one("SELECT COALESCE(MAX(version),0) AS n FROM draft_versions WHERE task_id=?", (parent["task_id"],)) or {"n": 0}
    new_draft_id = new_id("draft")
    package = payload.model_dump()
    task_row = database.one("SELECT constraints_json FROM copy_tasks WHERE id=?", (parent["task_id"],)) or {}
    task_constraints = json_loads(task_row.get("constraints_json"), {}) or {}
    if str(task_constraints.get("product_mode") or "") == "moments_life_case":
        package["cta"] = ""
        deliverables = package.get("deliverables") if isinstance(package.get("deliverables"), dict) else {}
        variants = deliverables.get("life_case_variants") if isinstance(deliverables.get("life_case_variants"), list) else []
        selected_index = next(
            (
                index for index, item in enumerate(variants)
                if isinstance(item, dict) and str(item.get("body") or "").strip() == payload.body.strip()
            ),
            None,
        )
        if selected_index is None:
            selected_index = next(
                (index for index, item in enumerate(variants) if isinstance(item, dict) and bool(item.get("recommended"))),
                next((index for index, item in enumerate(variants) if isinstance(item, dict)), None),
            )
            if selected_index is not None and isinstance(variants[selected_index], dict):
                variants[selected_index]["body"] = payload.body.strip()
        if selected_index is not None:
            for index, item in enumerate(variants):
                if isinstance(item, dict):
                    item["recommended"] = index == selected_index
            deliverables["life_case_variants"] = variants
            package["deliverables"] = deliverables
    now = utc_now()
    with database.transaction() as connection:
        connection.execute("UPDATE draft_versions SET is_final=0 WHERE task_id=?", (parent["task_id"],))
        connection.execute(
            """INSERT INTO draft_versions
            (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,parent_version_id,created_at)
            VALUES(?,?,?,?, 'USER_EDIT',?,?,1,?,?)""",
            (new_draft_id, parent["task_id"], parent["workspace_id"], int(max_row["n"]) + 1, json_dumps(package), payload.body, draft_id, now),
        )
        connection.execute(
            "UPDATE copy_tasks SET status='FINAL_READY',stage='FINAL',review_target_id=NULL,last_error=NULL,updated_at=? WHERE id=?",
            (now, parent["task_id"]),
        )
    database.emit(parent["task_id"], parent["workspace_id"], "FINAL", "USER_EDIT", "用户修改已保存为新的最终版本", {"draft_id": new_draft_id})
    return decode_row(database.one("SELECT * FROM draft_versions WHERE id=?", (new_draft_id,))) or {}


@app.post("/api/drafts/{draft_id}/submit-review", status_code=202)
def submit_draft_review(draft_id: str) -> dict[str, Any]:
    draft = database.one("SELECT * FROM draft_versions WHERE id=?", (draft_id,))
    if not draft:
        raise HTTPException(404, "稿件版本不存在")
    database.execute(
        "UPDATE copy_tasks SET status='QUEUED',stage='EDITOR',review_target_id=?,lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?",
        (draft_id, utc_now(), draft["task_id"]),
    )
    database.emit(draft["task_id"], draft["workspace_id"], "EDITOR", "REVIEW_REQUESTED", "用户版本已提交主编独立复核", {"draft_id": draft_id})
    task_worker.wake()
    return {"queued": True, "draft_id": draft_id}


@app.post("/api/drafts/{draft_id}/rollback", status_code=201)
def rollback_draft(draft_id: str) -> dict[str, Any]:
    source = database.one("SELECT * FROM draft_versions WHERE id=?", (draft_id,))
    if not source:
        raise HTTPException(404, "稿件版本不存在")
    package = json_loads(source["package_json"], {}) or {}
    return edit_draft(
        draft_id,
        DraftEdit(
            title=package.get("title") or "未命名稿件",
            body=package.get("body") or source["body_text"],
            cta=package.get("cta") or "",
            alternative_titles=package.get("alternative_titles") or [],
            platform_variants=package.get("platform_variants") or {},
            tags=package.get("tags") or [],
            deliverables=package.get("deliverables") or {},
            claims=package.get("claims") or [],
            image_briefs=package.get("image_briefs") or [],
            conversion_structure=package.get("conversion_structure") or {},
        ),
    )


@app.get("/api/drafts/compare")
def compare_drafts(left_id: str, right_id: str) -> dict[str, Any]:
    left = database.one("SELECT * FROM draft_versions WHERE id=?", (left_id,))
    right = database.one("SELECT * FROM draft_versions WHERE id=?", (right_id,))
    if not left or not right or left["task_id"] != right["task_id"]:
        raise HTTPException(400, "两个版本必须来自同一任务")
    diff = "\n".join(
        difflib.unified_diff(left["body_text"].splitlines(), right["body_text"].splitlines(), fromfile=f"v{left['version']}", tofile=f"v{right['version']}", lineterm="")
    )
    ratio = difflib.SequenceMatcher(None, left["body_text"], right["body_text"]).ratio()
    return {"left": decode_row(left), "right": decode_row(right), "similarity": round(ratio, 4), "diff": diff}


@app.get("/api/drafts/{draft_id}/export")
def export_draft(draft_id: str, format: str = "md"):
    draft = database.one("SELECT * FROM draft_versions WHERE id=?", (draft_id,))
    if not draft:
        raise HTTPException(404, "稿件版本不存在")
    package = json_loads(draft["package_json"], {}) or {}
    if format == "json":
        return JSONResponse(package, headers={"Content-Disposition": f'attachment; filename="{draft_id}.json"'})
    content = f"# {package.get('title','未命名稿件')}\n\n{package.get('body','')}\n\n---\n\nCTA：{package.get('cta','')}\n"
    return PlainTextResponse(content, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{draft_id}.md"'})


@app.get("/api/roles")
def list_roles(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return [decode_row(row) or {} for row in database.all("SELECT * FROM role_configs WHERE workspace_id=? ORDER BY sort_order", (workspace_id,))]


@app.patch("/api/roles/{role_id}")
def update_role(role_id: str, payload: RoleUpdate) -> dict[str, Any]:
    row = database.one("SELECT * FROM role_configs WHERE id=?", (role_id,))
    if not row:
        raise HTTPException(404, "员工配置不存在")
    fields = payload.model_dump(exclude_unset=True)
    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])
    fields["updated_at"] = utc_now()
    database.execute(f"UPDATE role_configs SET {','.join(f'{key}=?' for key in fields)} WHERE id=?", (*fields.values(), role_id))
    return decode_row(database.one("SELECT * FROM role_configs WHERE id=?", (role_id,))) or {}


@app.get("/api/workflows")
def get_workflows(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return [decode_row(row) or {} for row in database.all("SELECT * FROM workflows WHERE workspace_id=?", (workspace_id,))]


@app.patch("/api/workflows/{workflow_id}")
def update_workflow(workflow_id: str, payload: WorkflowUpdate) -> dict[str, Any]:
    row = database.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
    if not row:
        raise HTTPException(404, "工作流不存在")
    fields = payload.model_dump(exclude_none=True)
    if "steps" in fields:
        steps = fields.pop("steps")
        if steps != ["researcher", "writer", "editor"]:
            raise HTTPException(400, "首版生产线必须保持研究员 → 文案员工 → 主编的质量闸门顺序")
        fields["steps_json"] = json_dumps(steps)
    fields["updated_at"] = utc_now()
    database.execute(f"UPDATE workflows SET {','.join(f'{key}=?' for key in fields)} WHERE id=?", (*fields.values(), workflow_id))
    return decode_row(database.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))) or {}


@app.get("/api/models")
def list_models(workspace_id: str | None = None) -> list[dict[str, Any]]:
    if workspace_id:
        rows = database.all("SELECT * FROM model_profiles WHERE workspace_id=? OR workspace_id IS NULL ORDER BY workspace_id IS NULL,is_default DESC", (workspace_id,))
    else:
        rows = database.all("SELECT * FROM model_profiles ORDER BY workspace_id IS NULL,is_default DESC")
    return [redact_profile(row) for row in rows]


@app.post("/api/models", status_code=201)
def create_model(payload: ProfileCreate) -> dict[str, Any]:
    if payload.workspace_id:
        require_workspace(payload.workspace_id)
    if payload.api_key:
        raise HTTPException(400, "自定义模型入口只使用本机代理；DeepSeek 请在设置页的 DeepSeek API 卡片中连接")
    if payload.provider != LOCAL_PROXY_PROVIDER or payload.protocol != LOCAL_PROXY_PROTOCOL:
        raise HTTPException(400, "自定义模型入口只支持本机 OpenAI-compatible 代理；DeepSeek 请在设置页连接")
    profile_id, now = new_id("model"), utc_now()
    base_url = normalize_local_proxy_url(payload.base_url)
    if payload.is_default:
        if payload.workspace_id:
            database.execute("UPDATE model_profiles SET is_default=0 WHERE workspace_id=?", (payload.workspace_id,))
        else:
            database.execute("UPDATE model_profiles SET is_default=0 WHERE workspace_id IS NULL")
    database.execute(
        """INSERT INTO model_profiles
        (id,workspace_id,name,provider,protocol,base_url,model,secret_ref,temperature,max_tokens,is_default,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (profile_id, payload.workspace_id, payload.name, payload.provider, payload.protocol, base_url, payload.model, None, payload.temperature, payload.max_tokens, int(payload.is_default), now, now),
    )
    return redact_profile(database.one("SELECT * FROM model_profiles WHERE id=?", (profile_id,)) or {})


@app.get("/api/search-profiles")
def list_search_profiles(workspace_id: str | None = None) -> list[dict[str, Any]]:
    rows = database.all("SELECT * FROM search_profiles WHERE workspace_id=? OR workspace_id IS NULL ORDER BY workspace_id IS NULL,is_default DESC", (workspace_id,)) if workspace_id else database.all("SELECT * FROM search_profiles ORDER BY is_default DESC")
    return [redact_profile(row) for row in rows]


@app.post("/api/search-profiles", status_code=201)
def create_search_profile(payload: SearchProfileCreate) -> dict[str, Any]:
    if payload.workspace_id:
        require_workspace(payload.workspace_id)
    profile_id, now = new_id("search"), utc_now()
    secret_ref = vault.put(payload.api_key) if payload.api_key else None
    if payload.is_default:
        if payload.workspace_id:
            database.execute("UPDATE search_profiles SET is_default=0 WHERE workspace_id=?", (payload.workspace_id,))
        else:
            database.execute("UPDATE search_profiles SET is_default=0 WHERE workspace_id IS NULL")
    database.execute(
        "INSERT INTO search_profiles(id,workspace_id,name,provider,endpoint,secret_ref,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (profile_id, payload.workspace_id, payload.name, payload.provider, payload.endpoint, secret_ref, int(payload.is_default), now, now),
    )
    return redact_profile(database.one("SELECT * FROM search_profiles WHERE id=?", (profile_id,)) or {})


@app.get("/api/image-profiles")
def list_image_profiles(workspace_id: str | None = None) -> list[dict[str, Any]]:
    rows = database.all("SELECT * FROM image_profiles WHERE workspace_id=? OR workspace_id IS NULL ORDER BY workspace_id IS NULL,is_default DESC", (workspace_id,)) if workspace_id else database.all("SELECT * FROM image_profiles ORDER BY is_default DESC")
    return [redact_profile(row) for row in rows]


@app.post("/api/image-profiles", status_code=201)
def create_image_profile(payload: ImageProfileCreate) -> dict[str, Any]:
    if payload.workspace_id:
        require_workspace(payload.workspace_id)
    profile_id, now = new_id("image"), utc_now()
    secret_ref = vault.put(payload.api_key) if payload.api_key else None
    if payload.is_default:
        if payload.workspace_id:
            database.execute("UPDATE image_profiles SET is_default=0 WHERE workspace_id=?", (payload.workspace_id,))
        else:
            database.execute("UPDATE image_profiles SET is_default=0 WHERE workspace_id IS NULL")
    database.execute(
        "INSERT INTO image_profiles(id,workspace_id,name,provider,base_url,model,secret_ref,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (profile_id, payload.workspace_id, payload.name, payload.provider, payload.base_url, payload.model, secret_ref, int(payload.is_default), now, now),
    )
    return redact_profile(database.one("SELECT * FROM image_profiles WHERE id=?", (profile_id,)) or {})


@app.post("/api/assets/{asset_id}/generate")
def generate_asset(asset_id: str) -> dict[str, Any]:
    asset = database.one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if not asset:
        raise HTTPException(404, "配图任务不存在")
    profile = database.one(
        """SELECT * FROM image_profiles WHERE (workspace_id=? OR workspace_id IS NULL) AND is_default=1
        ORDER BY CASE WHEN workspace_id=? THEN 0 ELSE 1 END LIMIT 1""",
        (asset["workspace_id"], asset["workspace_id"]),
    )
    if not profile or not vault.get(profile.get("secret_ref")):
        raise HTTPException(409, "请先配置可用的图片模型")
    brief = json_loads(asset["brief_json"], {}) or {}
    with httpx.Client(timeout=120, follow_redirects=False) as client:
        response = client.post(
            f"{str(profile['base_url']).rstrip('/')}/images/generations",
            headers={"Authorization": f"Bearer {vault.get(profile['secret_ref'])}"},
            json={"model": profile["model"], "prompt": brief.get("prompt") or brief.get("purpose") or "编辑感内容配图", "size": "1024x1024"},
        )
        if response.is_redirect:
            raise HTTPException(502, "图片接口重定向已被阻止")
        response.raise_for_status()
        data = (response.json().get("data") or [{}])[0]
    local_path, remote_url = None, data.get("url")
    if data.get("b64_json"):
        local = ASSET_DIR / f"{asset_id}.png"
        local.write_bytes(base64.b64decode(data["b64_json"]))
        local_path = str(local)
    if not local_path and not remote_url:
        raise HTTPException(502, "图片接口没有返回图像")
    database.execute("UPDATE assets SET local_path=?,remote_url=?,status='READY',updated_at=? WHERE id=?", (local_path, remote_url, utc_now(), asset_id))
    return decode_row(database.one("SELECT * FROM assets WHERE id=?", (asset_id,))) or {}


@app.post("/api/assets/{asset_id}/replace")
async def replace_asset(asset_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    asset = database.one("SELECT * FROM assets WHERE id=?", (asset_id,))
    if not asset:
        raise HTTPException(404, "配图任务不存在")
    data = await file.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "图片超过15MB")
    suffix = Path(file.filename or "image.png").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "只支持 PNG、JPG、WEBP")
    local = ASSET_DIR / f"{asset_id}{suffix}"
    local.write_bytes(data)
    database.execute("UPDATE assets SET local_path=?,remote_url=NULL,status='READY',updated_at=? WHERE id=?", (str(local), utc_now(), asset_id))
    return decode_row(database.one("SELECT * FROM assets WHERE id=?", (asset_id,))) or {}


@app.get("/api/assets/{asset_id}/file")
def get_asset_file(asset_id: str):
    asset = database.one("SELECT local_path FROM assets WHERE id=? AND status='READY'", (asset_id,))
    if not asset or not asset.get("local_path"):
        raise HTTPException(404, "本地图片不存在")
    path = Path(asset["local_path"]).resolve()
    if ASSET_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "图片路径无效")
    return FileResponse(path)


@app.get("/api/performance")
def list_performance(workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    rows = database.all("SELECT * FROM performance_records WHERE workspace_id=? ORDER BY recorded_at DESC", (workspace_id,))
    totals = database.one(
        """SELECT COALESCE(SUM(impressions),0) impressions,COALESCE(SUM(reads),0) reads,
        COALESCE(SUM(interactions),0) interactions,COALESCE(SUM(inquiries),0) inquiries,
        COALESCE(SUM(contacts),0) contacts,COALESCE(SUM(sales),0) sales,COALESCE(SUM(revenue),0) revenue
        FROM performance_records WHERE workspace_id=?""",
        (workspace_id,),
    ) or {}
    return {"records": rows, "totals": totals, "insights": performance_insights(database, workspace_id)}


@app.post("/api/performance", status_code=201)
def add_performance(payload: PerformanceCreate) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    record_id = new_id("perf")
    database.execute(
        """INSERT INTO performance_records
        (id,workspace_id,task_id,draft_version_id,platform,impressions,reads,completions,interactions,inquiries,contacts,sales,revenue,notes,recorded_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (record_id, payload.workspace_id, payload.task_id, payload.draft_version_id, payload.platform, payload.impressions, payload.reads, payload.completions, payload.interactions, payload.inquiries, payload.contacts, payload.sales, payload.revenue, payload.notes, utc_now()),
    )
    return database.one("SELECT * FROM performance_records WHERE id=?", (record_id,)) or {}


@app.get("/api/topic-radar/monitors")
def list_topic_monitors(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return [decode_row(row) or {} for row in topic_radar_service.list_monitors(workspace_id)]


@app.get("/api/topic-radar/browser/status")
def topic_radar_browser_status() -> dict[str, Any]:
    return platform_browser_collector.status()


@app.post("/api/topic-radar/browser/open")
def topic_radar_browser_open(payload: PlatformBrowserOpen) -> dict[str, Any]:
    try:
        return platform_browser_collector.open(payload.platform, payload.query)
    except PlatformBrowserError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/topic-radar/ip-daily")
def get_ip_daily_radar(workspace_id: str) -> dict[str, Any]:
    require_workspace(workspace_id)
    monitor = topic_radar_service.get_ip_daily_monitor(workspace_id)
    if not monitor:
        return {"monitor": None, "latest_run": None}
    runs = topic_radar_service.list_runs(monitor["id"], workspace_id, limit=1)
    return {
        "monitor": decode_row(monitor) or {},
        "latest_run": decode_radar_run(runs[0]) if runs else None,
    }


@app.post("/api/topic-radar/ip-daily/enable", status_code=201)
def enable_ip_daily_radar(payload: DailyIpRadarEnable) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    monitor = topic_radar_service.ensure_ip_daily_monitor(payload.workspace_id, payload.interval_hours)
    try:
        run = topic_radar_service.run_monitor(monitor["id"], payload.workspace_id)
    except RadarBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    topic_radar_worker.wake()
    return {
        "monitor": decode_row(topic_radar_service.get_monitor(monitor["id"], payload.workspace_id)) or {},
        "latest_run": decode_radar_run(run),
    }


@app.post("/api/topic-radar/ip-daily/run", status_code=202)
def run_ip_daily_radar(payload: DailyIpRadarEnable) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    monitor = topic_radar_service.get_ip_daily_monitor(payload.workspace_id)
    if not monitor:
        raise HTTPException(404, "每日 IP 雷达尚未开启")
    try:
        run = topic_radar_service.start_monitor(monitor["id"], payload.workspace_id)
    except RadarBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "monitor": decode_row(topic_radar_service.get_monitor(monitor["id"], payload.workspace_id)) or {},
        "latest_run": decode_radar_run(run),
    }


@app.post("/api/topic-radar/topic-search")
def discover_topic_for_copy(payload: TopicDiscoveryRequest) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    return topic_radar_service.discover_topic(payload.workspace_id, payload.topic, payload.limit)


@app.get("/api/topic-radar/xhs/extractions")
def list_xiaohongshu_extractions(workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return xhs_extraction_service.list(workspace_id, limit)


@app.post("/api/topic-radar/xhs/extract")
def extract_xiaohongshu_post(payload: XiaohongshuExtractionRequest) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    return {"extraction": xhs_extraction_service.extract(
        payload.workspace_id,
        str(payload.url),
        search_keyword=payload.search_keyword,
        title_hint=payload.title_hint,
        force=payload.force,
    )}


@app.get("/api/topic-radar/xhs/captures/{post_key}/{filename}")
def get_xiaohongshu_capture(post_key: str, filename: str) -> FileResponse:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", post_key) or not re.fullmatch(r"(?:page|image-[1-4])\.png", filename):
        raise HTTPException(404, "截图不存在")
    candidate = (RADAR_CAPTURE_DIR / post_key / filename).resolve()
    if not candidate.is_file() or RADAR_CAPTURE_DIR.resolve() not in candidate.parents:
        raise HTTPException(404, "截图不存在")
    return FileResponse(candidate, media_type="image/png")


@app.post("/api/topic-radar/topic-search/create", status_code=202)
def create_copy_task_from_topic_search(payload: TopicDiscoveryCreate) -> dict[str, Any]:
    """Create a normal three-employee copy task from selected public sources."""
    require_workspace(payload.workspace_id)
    constraints = dict(payload.constraints)
    content_type = payload.content_type
    platform = payload.platform.strip()
    if payload.product_mode == "xiaohongshu_ip":
        content_type, platform = "xiaohongshu", platform or "小红书"
        constraints["product_mode"] = "xiaohongshu_ip"
    elif payload.product_mode == "douyin_emotion":
        content_type, platform = "short_video", platform or "抖音"
        constraints["product_mode"] = "douyin_emotion"
    else:
        platform = platform or ("抖音" if content_type == "short_video" else "小红书")
    constraints.setdefault("radar_origin", "topic_search")
    task_payload = CopyTaskCreate(
        workspace_id=payload.workspace_id,
        content_type=content_type,
        platform=platform,
        topic=payload.topic,
        goal=payload.goal,
        offer=payload.offer,
        constraints=constraints,
        source_urls=list(payload.source_urls),
        web_research=True,
    )
    return _create_task_record(task_payload)


@app.patch("/api/topic-radar/monitors/{monitor_id}")
def update_topic_monitor(monitor_id: str, workspace_id: str, payload: TopicMonitorUpdate) -> dict[str, Any]:
    require_workspace(workspace_id)
    try:
        row = topic_radar_service.update_monitor(
            monitor_id, workspace_id, enabled=payload.enabled, interval_hours=payload.interval_hours
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    topic_radar_worker.wake()
    return decode_row(row) or {}


@app.get("/api/topic-radar/monitors/{monitor_id}/runs")
def list_topic_monitor_runs(monitor_id: str, workspace_id: str, limit: int = 12) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    try:
        return [decode_radar_run(row) for row in topic_radar_service.list_runs(monitor_id, workspace_id, limit)]
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc




@app.get("/api/connectors")
def list_connectors(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return [redact_profile(row) for row in database.all("SELECT * FROM connector_profiles WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))]


@app.post("/api/connectors", status_code=201)
def create_connector(payload: ConnectorCreate) -> dict[str, Any]:
    require_workspace(payload.workspace_id)
    connector_id, now = new_id("connector"), utc_now()
    secret_ref = vault.put(payload.secret) if payload.secret else None
    database.execute(
        "INSERT INTO connector_profiles(id,workspace_id,platform,name,config_json,secret_ref,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'CONFIGURED',?,?)",
        (connector_id, payload.workspace_id, payload.platform, payload.name, json_dumps(payload.config), secret_ref, now, now),
    )
    return redact_profile(database.one("SELECT * FROM connector_profiles WHERE id=?", (connector_id,)) or {})


@app.get("/api/publishing/status")
def publishing_status() -> dict[str, Any]:
    return {"enabled": publishing_enabled(), "gate": "阶段A真实文案验收"}


@app.post("/api/publishing/activate")
def activate_publishing(confirmed: bool = False) -> dict[str, Any]:
    if not confirmed:
        raise HTTPException(400, "需要明确确认阶段A已通过真实文案验收")
    PUBLISH_GATE.write_text(utc_now(), encoding="utf-8")
    publish_worker.wake()
    return {"enabled": True}


@app.post("/api/publishing/approve", status_code=202)
def approve_publish(payload: PublishApprove) -> dict[str, Any]:
    row = publisher.approve(payload.workspace_id, payload.task_id, payload.draft_version_id, payload.connector_id, payload.scheduled_at)
    publish_worker.wake()
    return decode_row(row) or {}


@app.get("/api/publishing/jobs")
def list_publish_jobs(workspace_id: str) -> list[dict[str, Any]]:
    require_workspace(workspace_id)
    return [decode_row(row) or {} for row in database.all("SELECT * FROM publish_jobs WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))]


@app.get("/api/plugins")
def list_plugins() -> list[dict[str, Any]]:
    return plugin_manager.list()


@app.post("/api/plugins/reload")
def reload_plugins() -> list[dict[str, Any]]:
    plugin_manager.reload()
    return plugin_manager.list()


@app.post("/api/plugins/{plugin_id}/invoke")
def invoke_plugin(plugin_id: str, payload: PluginInvoke) -> dict[str, Any]:
    return plugin_manager.invoke(plugin_id, payload.payload)


@app.get("/api/events")
async def stream_events(request: Request, workspace_id: str, after_id: int = 0):
    require_workspace(workspace_id)

    async def generate():
        cursor = after_id
        while not await request.is_disconnected():
            rows = database.all("SELECT * FROM task_events WHERE workspace_id=? AND id>? ORDER BY id LIMIT 100", (workspace_id, cursor))
            if rows:
                for row in rows:
                    cursor = int(row["id"])
                    yield f"id: {cursor}\nevent: task\ndata: {json_dumps(decode_row(row))}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "API 路径不存在")
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


def run() -> None:
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
