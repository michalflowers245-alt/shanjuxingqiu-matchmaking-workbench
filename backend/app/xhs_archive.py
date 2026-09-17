from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .config import XHS_ARCHIVE_DIR
from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, platform_browser_collector


COLLECTOR_VERSION = "xhs-archive-v2.1"
BROWSER_PROFILE_ID = "social_browser"
ACTIVE_JOB_STATUSES = ("QUEUED", "RUNNING")


def _redact_error(value: str) -> str:
    text = re.sub(r"(?i)(authorization|cookie|token|xsec_token|signature|sign)=([^\s&]+)", r"\1=[REDACTED]", value)
    return re.sub(r"https?://[^\s]+", lambda item: _safe_source_ref(item.group(0))[0] or "[REDACTED_URL]", text)[:500]


def normalize_body(value: str) -> str:
    """Create searchable text without mutating the archived raw body."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "")[:160]
    if not cleaned:
        raise ValueError("归档目录标识为空")
    return cleaned


def _safe_source_ref(value: str) -> tuple[str, str]:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "", ""
    if parsed.scheme not in {"http", "https"}:
        return "", ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), parsed.hostname or ""


def _image_metadata(payload: bytes, declared: str | None) -> tuple[str, str, int | None, int | None]:
    detected = ""
    extension = ""
    width: int | None = None
    height: int | None = None
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format or "", "")
            extension = f".{(image.format or '').lower()}" if image.format else ""
    except Exception:
        detected = ""
    mime = detected or str(declared or "").split(";", 1)[0].strip().lower()
    if not extension and mime:
        extension = mimetypes.guess_extension(mime) or ""
    if extension == ".jpe":
        extension = ".jpg"
    return mime, extension, width, height


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass
class ArchiveResult:
    archive_id: str
    archive_status: str
    verification_status: str
    archive_path: str
    expected_asset_count: int | None
    saved_asset_count: int
    missing_ordinals: list[int]


class XiaohongshuArchiveRepository:
    def __init__(self, db: Database = database, root: Path = XHS_ARCHIVE_DIR) -> None:
        self.db = db
        self.root = root

    def persist(self, workspace_id: str, extraction_id: str, capture: dict[str, Any]) -> ArchiveResult:
        target_post_id = str(capture.get("target_post_id") or "")
        observed_post_id = str(capture.get("observed_post_id") or "")
        if not target_post_id or target_post_id != observed_post_id:
            raise PlatformBrowserError(
                "详情页帖子 ID 与目标帖子不一致，已拒绝写入归档",
                code="identity_mismatch",
                diagnostics={"target_post_id": target_post_id, "observed_post_id": observed_post_id},
            )

        body_raw = capture.get("body_raw")
        if body_raw is not None and not isinstance(body_raw, str):
            body_raw = str(body_raw)
        body_normalized = normalize_body(body_raw or "") if body_raw is not None else None
        body_verified = bool(capture.get("body_verified") and body_raw is not None)
        expected = capture.get("expected_image_count")
        expected_count = int(expected) if isinstance(expected, int) and expected >= 0 else None
        archive_id = new_id("xhsa")
        captured_at = utc_now()
        workspace_part = _safe_component(workspace_id)
        post_part = _safe_component(target_post_id)
        archive_part = _safe_component(archive_id)
        final_dir = self.root / workspace_part / post_part / archive_part
        staging_dir = final_dir.with_name(f".{final_dir.name}.part")
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        (staging_dir / "images").mkdir(parents=True, exist_ok=True)

        asset_rows: list[dict[str, Any]] = []
        manifest_assets: list[dict[str, Any]] = []
        saved_carousel: set[int] = set()
        cover_discovered = False
        cover_saved = False
        for candidate in capture.get("assets") or []:
            if not isinstance(candidate, dict):
                continue
            role = str(candidate.get("role") or "carousel")
            if role not in {"carousel", "cover"}:
                continue
            ordinal = max(1, int(candidate.get("ordinal") or 1))
            if role == "cover":
                cover_discovered = True
            asset_id = new_id("xhsi")
            raw_url = str(candidate.get("source_url") or "")
            source_ref, source_host = _safe_source_ref(raw_url)
            payload = candidate.get("bytes")
            if not isinstance(payload, (bytes, bytearray)):
                payload = None
            declared = str(candidate.get("mime_declared") or "") or None
            status = "MISSING"
            error_code = str(candidate.get("error_code") or "RESPONSE_BODY_UNAVAILABLE")
            relative_path: str | None = None
            sha256: str | None = None
            byte_size: int | None = None
            mime_detected = ""
            extension = ""
            width = candidate.get("width") if isinstance(candidate.get("width"), int) else None
            height = candidate.get("height") if isinstance(candidate.get("height"), int) else None
            if payload:
                mime_detected, extension, detected_width, detected_height = _image_metadata(bytes(payload), declared)
                width = detected_width or width
                height = detected_height or height
                if mime_detected.startswith("image/") and extension:
                    filename = f"{role}-{ordinal:03d}{extension}"
                    target = staging_dir / "images" / filename
                    target.write_bytes(bytes(payload))
                    relative_path = str(target.relative_to(staging_dir))
                    sha256 = hashlib.sha256(payload).hexdigest()
                    byte_size = len(payload)
                    status = "SAVED"
                    error_code = ""
                    if role == "carousel":
                        saved_carousel.add(ordinal)
                    elif role == "cover":
                        cover_saved = True
                else:
                    error_code = "INVALID_IMAGE_BYTES"
            row = {
                "id": asset_id,
                "workspace_id": workspace_id,
                "archive_id": archive_id,
                "role": role,
                "ordinal": ordinal,
                "source_ref": source_ref,
                "source_kind": str(candidate.get("source_kind") or "browser_response")[:80],
                "source_host": source_host,
                "rendition": str(candidate.get("rendition") or "browser_returned")[:80],
                "mime_declared": declared,
                "mime_detected": mime_detected or None,
                "extension": extension or None,
                "width": width,
                "height": height,
                "duration_ms": None,
                "byte_size": byte_size,
                "bytes_expected": candidate.get("bytes_expected") if isinstance(candidate.get("bytes_expected"), int) else None,
                "sha256": sha256,
                "relative_path": relative_path,
                "status": status,
                "http_status": candidate.get("http_status") if isinstance(candidate.get("http_status"), int) else None,
                "error_code": error_code or None,
                "retry_after": None,
                "evidence_json": json_dumps({
                    "source": str(candidate.get("source_kind") or "browser_response"),
                    "ordered_by": str(candidate.get("ordered_by") or "page_state"),
                }),
                "created_at": captured_at,
            }
            asset_rows.append(row)
            manifest_assets.append({key: value for key, value in row.items() if key not in {"workspace_id", "archive_id", "evidence_json"}})

        saved_count = len(saved_carousel)
        missing = [] if expected_count is None else [value for value in range(1, expected_count + 1) if value not in saved_carousel]
        has_video = bool(capture.get("video_count"))
        identity_ok = target_post_id == observed_post_id
        images_complete = expected_count is not None and expected_count > 0 and saved_count == expected_count and not missing
        complete = identity_ok and body_verified and images_complete and (not cover_discovered or cover_saved) and not has_video
        archive_status = "COMPLETE" if complete else "PARTIAL"
        verification_status = "VERIFIED" if identity_ok and body_verified else "UNVERIFIED"
        content_sha256 = hashlib.sha256((body_raw or "").encode("utf-8")).hexdigest() if body_raw is not None else None

        metadata = {
            "archive_id": archive_id,
            "workspace_id": workspace_id,
            "extraction_id": extraction_id,
            "post_id": target_post_id,
            "observed_post_id": observed_post_id,
            "canonical_url": str(capture.get("canonical_url") or ""),
            "title": str(capture.get("title") or ""),
            "author": str(capture.get("author") or ""),
            "published_at": capture.get("published_at"),
            "body_source": str(capture.get("body_source") or "unavailable"),
            "body_verified": body_verified,
            "expected_image_count": expected_count,
            "saved_image_count": saved_count,
            "missing_ordinals": missing,
            "video_count": int(capture.get("video_count") or 0),
            "media_status": "MEDIA_UNAVAILABLE" if has_video else "NOT_APPLICABLE",
            "archive_status": archive_status,
            "verification_status": verification_status,
            "captured_at": captured_at,
            "collector_version": COLLECTOR_VERSION,
        }
        diagnostics = {
            "failure_reasons": [
                *([] if body_verified else ["BODY_UNVERIFIED"]),
                *([] if expected_count is not None else ["EXPECTED_IMAGE_COUNT_UNKNOWN"]),
                *([] if not missing else ["MISSING_IMAGE_ORDINALS"]),
                *([] if not cover_discovered or cover_saved else ["COVER_MISSING"]),
                *([] if not has_video else ["MEDIA_UNAVAILABLE"]),
            ],
            "missing_ordinals": missing,
            "collector": capture.get("diagnostics") or {},
        }
        (staging_dir / "body_raw.txt").write_bytes((body_raw or "").encode("utf-8"))
        (staging_dir / "body_normalized.txt").write_bytes((body_normalized or "").encode("utf-8"))
        (staging_dir / "metadata.json").write_bytes(_json_bytes(metadata))
        (staging_dir / "diagnostics.json").write_bytes(_json_bytes(diagnostics))
        manifest = {"version": 2, "assets": manifest_assets}
        manifest_bytes = _json_bytes(manifest)
        (staging_dir / "manifest.json").write_bytes(manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        checksum_lines = []
        for path in sorted(item for item in staging_dir.rglob("*") if item.is_file()):
            checksum_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(staging_dir)}")
        (staging_dir / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dir, final_dir)

        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT archive_status,latest_archive_id FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
                (workspace_id, extraction_id),
            ).fetchone()
            if not current:
                raise ValueError("小红书记录不存在或不属于当前工作区")
            connection.execute(
                """INSERT INTO xhs_archive_versions
                (id,workspace_id,extraction_id,post_id,observed_post_id,archive_path,body_raw,body_normalized,
                 body_source,body_evidence_json,archive_status,verification_status,content_sha256,manifest_sha256,
                 captured_at,collector_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    archive_id, workspace_id, extraction_id, target_post_id, observed_post_id,
                    str(final_dir.relative_to(self.root)), body_raw, body_normalized,
                    str(capture.get("body_source") or "unavailable"),
                    json_dumps(capture.get("body_evidence") or {}), archive_status,
                    verification_status, content_sha256, manifest_sha256, captured_at, COLLECTOR_VERSION,
                ),
            )
            for row in asset_rows:
                columns = list(row)
                connection.execute(
                    f"INSERT INTO xhs_post_assets({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
            keep_existing_complete = current[0] == "COMPLETE" and archive_status != "COMPLETE"
            if not keep_existing_complete:
                connection.execute(
                    """UPDATE xhs_post_extractions SET
                    post_key=?,
                    title=CASE WHEN ?<>'' THEN ? ELSE title END,
                    author=CASE WHEN ?<>'' THEN ? ELSE author END,
                    published_at=COALESCE(?,published_at),
                    body_raw=CASE WHEN ? THEN ? ELSE body_raw END,
                    body_normalized=CASE WHEN ? THEN ? ELSE body_normalized END,
                    body_source=CASE WHEN ? THEN ? ELSE body_source END,
                    body_evidence_json=CASE WHEN ? THEN ? ELSE body_evidence_json END,
                    status=?,archive_status=?,verification_status=?,content_sha256=?,latest_archive_id=?,
                    expected_asset_count=?,saved_asset_count=?,updated_at=? WHERE workspace_id=? AND id=?""",
                    (
                        target_post_id,
                        str(capture.get("title") or ""), str(capture.get("title") or "")[:500],
                        str(capture.get("author") or ""), str(capture.get("author") or "")[:300],
                        capture.get("published_at") or None,
                        int(body_verified), body_raw, int(body_verified), body_normalized,
                        int(body_verified), str(capture.get("body_source") or ""),
                        int(body_verified), json_dumps(capture.get("body_evidence") or {}),
                        "success" if archive_status == "COMPLETE" else "partial",
                        archive_status, verification_status, content_sha256, archive_id,
                        expected_count, saved_count, captured_at, workspace_id, extraction_id,
                    ),
                )
        return ArchiveResult(
            archive_id=archive_id,
            archive_status=archive_status,
            verification_status=verification_status,
            archive_path=str(final_dir),
            expected_asset_count=expected_count,
            saved_asset_count=saved_count,
            missing_ordinals=missing,
        )


class XiaohongshuArchiveService:
    def __init__(
        self,
        db: Database = database,
        collector: PlatformBrowserCollector = platform_browser_collector,
        root: Path = XHS_ARCHIVE_DIR,
        collector_timeout_seconds: float | None = None,
    ) -> None:
        self.db = db
        self.collector = collector
        self.repository = XiaohongshuArchiveRepository(db, root)
        self.collector_timeout_seconds = max(
            0.01,
            float(collector_timeout_seconds if collector_timeout_seconds is not None else os.getenv("XHS_ARCHIVE_TIMEOUT_SECONDS", "90")),
        )
        self._jobs: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        now = utc_now()
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='QUEUED',stage='RECOVERED_AFTER_RESTART',
            lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=?,updated_at=? WHERE status='RUNNING'""",
            (now, now),
        )
        for row in self.db.all("SELECT id FROM xhs_archive_jobs WHERE status='QUEUED' ORDER BY created_at"):
            self._queue(str(row["id"]))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="xhs-archive-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _queue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
            self._jobs.put(job_id)

    def enqueue(
        self,
        workspace_id: str,
        extraction_id: str,
        *,
        resume_after_user_action: bool = False,
    ) -> dict[str, Any]:
        extraction = self.db.one(
            "SELECT id FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (workspace_id, extraction_id),
        )
        if not extraction:
            raise ValueError("小红书记录不存在或不属于当前工作区")
        active = self.db.one(
            """SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?
            AND operation='archive' AND status IN ('QUEUED','RUNNING') ORDER BY created_at DESC LIMIT 1""",
            (workspace_id, extraction_id),
        )
        if active:
            return self.decode_job(active)
        if resume_after_user_action:
            self.db.execute("DELETE FROM xhs_browser_pause WHERE profile_id=?", (BROWSER_PROFILE_ID,))
        pause = self.db.one("SELECT * FROM xhs_browser_pause WHERE profile_id=?", (BROWSER_PROFILE_ID,))
        revision_row = self.db.one(
            "SELECT COALESCE(MAX(revision),0) AS revision FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=?",
            (workspace_id, extraction_id),
        ) or {"revision": 0}
        revision = int(revision_row["revision"] or 0) + 1
        job_id = new_id("xhsj")
        now = utc_now()
        status = "PAUSED" if pause else "QUEUED"
        stage = "USER_ACTION_REQUIRED" if pause else "QUEUED"
        self.db.execute(
            """INSERT INTO xhs_archive_jobs
            (id,workspace_id,extraction_id,operation,idempotency_key,browser_profile_id,status,stage,
             progress_json,attempt,revision,created_at,updated_at,error_code)
            VALUES(?,?,?,'archive',?,?,?,?,?,0,?,?,?,?)""",
            (
                job_id, workspace_id, extraction_id, f"archive:{extraction_id}:{revision}",
                BROWSER_PROFILE_ID, status, stage, json_dumps({"message": pause["reason"] if pause else "已进入归档队列"}),
                revision, now, now, "BROWSER_PAUSED" if pause else None,
            ),
        )
        if status == "QUEUED":
            self._queue(job_id)
        return self.get_job(workspace_id, job_id) or {}

    @staticmethod
    def decode_job(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["progress"] = json_loads(result.pop("progress_json", None), {})
        return result

    def get_job(self, workspace_id: str, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND id=?", (workspace_id, job_id))
        return self.decode_job(row) if row else None

    def list_jobs(self, workspace_id: str, extraction_id: str | None = None) -> list[dict[str, Any]]:
        if extraction_id:
            rows = self.db.all(
                "SELECT * FROM xhs_archive_jobs WHERE workspace_id=? AND extraction_id=? ORDER BY created_at DESC LIMIT 20",
                (workspace_id, extraction_id),
            )
        else:
            rows = self.db.all(
                "SELECT * FROM xhs_archive_jobs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100",
                (workspace_id,),
            )
        return [self.decode_job(row) for row in rows]

    def _heartbeat(self, job_id: str, stage: str, message: str) -> None:
        now = utc_now()
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='RUNNING',stage=?,progress_json=?,heartbeat_at=?,updated_at=?
            WHERE id=?""",
            (stage, json_dumps({"message": message}), now, now, job_id),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job_id)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._jobs.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.db.one("SELECT * FROM xhs_archive_jobs WHERE id=?", (job_id,))
        if not job or job["status"] != "QUEUED":
            return
        extraction = self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND id=?",
            (job["workspace_id"], job["extraction_id"]),
        )
        if not extraction:
            self._finish_error(job, "EXTRACTION_NOT_FOUND", "归档来源记录不存在")
            return
        self._heartbeat(job_id, "READING_AUTHORIZED_PAGE", "正在读取登录浏览器中的同一篇帖子")
        self.db.execute("UPDATE xhs_archive_jobs SET attempt=attempt+1 WHERE id=?", (job_id,))
        try:
            capture = self._collect_with_deadline(
                str(extraction["url"]),
                search_keyword=str(extraction.get("search_keyword") or ""),
                title_hint=str(extraction.get("title") or ""),
            )
            self._heartbeat(job_id, "VERIFYING_AND_WRITING", "正在校验正文和图片字节并写入版本归档")
            result = self.repository.persist(str(job["workspace_id"]), str(job["extraction_id"]), capture)
            now = utc_now()
            status = "COMPLETE" if result.archive_status == "COMPLETE" else "PARTIAL"
            self.db.execute(
                """UPDATE xhs_archive_jobs SET status=?,stage='FINISHED',progress_json=?,heartbeat_at=?,
                error_code=NULL,updated_at=? WHERE id=?""",
                (
                    status,
                    json_dumps({
                        "message": "图文已完整归档" if status == "COMPLETE" else "已保存可取得内容，可继续补齐缺失项",
                        "archive_id": result.archive_id,
                        "expected_asset_count": result.expected_asset_count,
                        "saved_asset_count": result.saved_asset_count,
                        "missing_ordinals": result.missing_ordinals,
                    }),
                    now, now, job_id,
                ),
            )
        except PlatformBrowserError as exc:
            if exc.code in {"login_required", "verification_required", "platform_limited"}:
                now = utc_now()
                self.db.execute(
                    """INSERT INTO xhs_browser_pause(profile_id,reason,retry_after,requires_user_action,updated_at)
                    VALUES(?,?,NULL,1,?) ON CONFLICT(profile_id) DO UPDATE SET reason=excluded.reason,
                    retry_after=excluded.retry_after,requires_user_action=1,updated_at=excluded.updated_at""",
                    (BROWSER_PROFILE_ID, str(exc), now),
                )
                self.db.execute(
                    """UPDATE xhs_archive_jobs SET status='PAUSED',stage='USER_ACTION_REQUIRED',
                    progress_json=?,error_code=?,heartbeat_at=?,updated_at=? WHERE id=?""",
                    (json_dumps({"message": str(exc)}), exc.code.upper(), now, now, job_id),
                )
            else:
                self._finish_error(job, exc.code.upper(), _redact_error(str(exc)))
        except Exception as exc:
            self._finish_error(job, "ARCHIVE_FAILED", f"归档失败：{_redact_error(str(exc))}")

    def _collect_with_deadline(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Bound a potentially stalled CDP call without blocking the job forever."""
        result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def collect() -> None:
            try:
                result.put(("ok", self.collector.archive_xiaohongshu_detail(url, **kwargs)))
            except BaseException as exc:  # pass the original typed error back to the worker
                result.put(("error", exc))

        thread = threading.Thread(target=collect, daemon=True, name="xhs-archive-cdp-call")
        thread.start()
        thread.join(timeout=self.collector_timeout_seconds)
        if thread.is_alive():
            raise PlatformBrowserError(
                "读取小红书页面超时，任务已停止；请重启测试版后重试",
                code="archive_timeout",
            )
        state, value = result.get_nowait()
        if state == "error":
            raise value
        return value

    def _finish_error(self, job: dict[str, Any], code: str, message: str) -> None:
        now = utc_now()
        self.db.execute(
            """UPDATE xhs_archive_jobs SET status='FAILED',stage='FAILED',progress_json=?,error_code=?,
            heartbeat_at=?,updated_at=? WHERE id=?""",
            (json_dumps({"message": message}), code[:100], now, now, job["id"]),
        )

    def latest_archive(self, workspace_id: str, extraction_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            """SELECT * FROM xhs_archive_versions WHERE workspace_id=? AND extraction_id=?
            ORDER BY captured_at DESC LIMIT 1""",
            (workspace_id, extraction_id),
        )
        if not row:
            return None
        result = dict(row)
        result["body_evidence"] = json_loads(result.pop("body_evidence_json", None), {})
        assets = self.db.all(
            "SELECT * FROM xhs_post_assets WHERE workspace_id=? AND archive_id=? ORDER BY role,ordinal",
            (workspace_id, row["id"]),
        )
        for asset in assets:
            asset["evidence"] = json_loads(asset.pop("evidence_json", None), {})
            asset["download_url"] = (
                f"/api/topic-radar/xhs/archives/{row['id']}/assets/{asset['id']}?workspace_id={urllib.parse.quote(workspace_id)}"
                if asset.get("status") == "SAVED" else ""
            )
        result["assets"] = assets
        return result

    def resolve_asset(self, workspace_id: str, archive_id: str, asset_id: str) -> tuple[Path, str]:
        row = self.db.one(
            """SELECT a.relative_path,a.mime_detected,v.archive_path FROM xhs_post_assets a
            JOIN xhs_archive_versions v ON v.workspace_id=a.workspace_id AND v.id=a.archive_id
            WHERE a.workspace_id=? AND a.archive_id=? AND a.id=? AND a.status='SAVED'""",
            (workspace_id, archive_id, asset_id),
        )
        if not row or not row.get("relative_path"):
            raise FileNotFoundError("归档图片不存在")
        base = (self.repository.root / row["archive_path"]).resolve()
        target = (base / row["relative_path"]).resolve()
        if self.repository.root.resolve() not in target.parents or not target.is_file():
            raise FileNotFoundError("归档图片不存在")
        return target, str(row.get("mime_detected") or "application/octet-stream")


xhs_archive_service = XiaohongshuArchiveService()
