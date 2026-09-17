from __future__ import annotations

import queue
import re
import threading
import urllib.parse
from typing import Any

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, _safe_url, platform_browser_collector


JSON_COLUMNS = {
    "tags_json": ("tags", []),
    "metrics_json": ("metrics", {}),
    "field_sources_json": ("field_sources", {}),
    "diagnostics_json": ("diagnostics", {}),
}


class XiaohongshuExtractionService:
    """Persist the result of reading one visible Xiaohongshu post detail page."""

    def __init__(
        self,
        db: Database = database,
        collector: PlatformBrowserCollector = platform_browser_collector,
    ) -> None:
        self.db = db
        self.collector = collector
        self._jobs: queue.Queue[tuple[str, str, str, str]] = queue.Queue()
        self._queued: set[tuple[str, str]] = set()
        self._queue_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Resume jobs left behind if the workbench was closed during extraction.
        for row in self.db.all(
            "SELECT workspace_id,url,search_keyword,title FROM xhs_post_extractions WHERE status IN ('queued','extracting')"
        ):
            self._put_job(str(row["workspace_id"]), str(row["url"]), str(row.get("search_keyword") or ""), str(row.get("title") or ""))
        self._thread = threading.Thread(target=self._loop, daemon=True, name="xhs-extraction-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @staticmethod
    def _public_url(value: str) -> str:
        safe = _safe_url("xiaohongshu", value)
        if not safe:
            return ""
        parsed = urllib.parse.urlsplit(safe)
        if not re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", parsed.path):
            return ""
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _put_job(self, workspace_id: str, url: str, search_keyword: str, title_hint: str) -> bool:
        key = (workspace_id, url)
        with self._queue_lock:
            if key in self._queued:
                return False
            self._queued.add(key)
            self._jobs.put((workspace_id, url, search_keyword, title_hint))
        return True

    def enqueue(
        self, workspace_id: str, url: str, *, search_keyword: str = "", title_hint: str = "",
    ) -> bool:
        """Queue a post once and expose its progress to the UI immediately."""
        public_url = self._public_url(url)
        if not public_url:
            return False
        existing = self.get(workspace_id, public_url)
        if existing and existing.get("status") in {"success", "partial"} and existing.get("merged_copy"):
            return False
        if not self._put_job(workspace_id, public_url, search_keyword.strip()[:180], title_hint.strip()[:500]):
            return False
        now = utc_now()
        if existing:
            self.db.execute(
                """UPDATE xhs_post_extractions
                SET search_keyword=?,title=?,status='queued',failure_stage='',error_message='',updated_at=?
                WHERE workspace_id=? AND url=?""",
                (search_keyword.strip()[:180], title_hint.strip()[:500] or existing.get("title") or "", now, workspace_id, public_url),
            )
        else:
            self.db.execute(
                """INSERT INTO xhs_post_extractions
                (id,workspace_id,url,search_keyword,title,status,extracted_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?)""",
                (new_id("xhs"), workspace_id, public_url, search_keyword.strip()[:180], title_hint.strip()[:500], now, now),
            )
        return True

    def enqueue_results(self, workspace_id: str, payload: dict[str, Any], limit: int = 6) -> int:
        """Automatically queue Xiaohongshu originals from a radar run or topic search."""
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else json_loads(payload.get("summary_json"), {})
        candidates: list[tuple[str, str, str]] = []
        for recommendation in (summary or {}).get("recommendations", []):
            if not isinstance(recommendation, dict) or recommendation.get("evidence_platform") != "xiaohongshu":
                continue
            titles = recommendation.get("source_titles") or []
            candidates.append((
                str(recommendation.get("evidence_url") or ""),
                str(titles[0] if titles else recommendation.get("title") or ""),
                str(recommendation.get("search_query") or (summary or {}).get("query") or ""),
            ))
        for source in payload.get("sources") or []:
            if not isinstance(source, dict):
                continue
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            if metadata.get("platform") == "xiaohongshu" or source.get("platform") == "xiaohongshu":
                candidates.append((str(source.get("url") or ""), str(source.get("title") or ""), str(payload.get("topic") or "")))
        for item in payload.get("items") or []:
            if not isinstance(item, dict) or item.get("platform") != "xiaohongshu":
                continue
            raw = item.get("raw") if isinstance(item.get("raw"), dict) else json_loads(item.get("raw_json"), {})
            candidates.append((str(item.get("url") or ""), str(item.get("title") or ""), str((raw or {}).get("search_query") or (summary or {}).get("query") or "")))
        added, seen = 0, set()
        for url, title, query_text in candidates:
            public_url = self._public_url(url)
            if not public_url or public_url in seen:
                continue
            seen.add(public_url)
            if self.enqueue(workspace_id, public_url, search_keyword=query_text, title_hint=title):
                added += 1
            if len(seen) >= max(1, min(int(limit), 12)):
                break
        return added

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                workspace_id, url, search_keyword, title_hint = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            key = (workspace_id, url)
            try:
                self.db.execute(
                    "UPDATE xhs_post_extractions SET status='extracting',updated_at=? WHERE workspace_id=? AND url=?",
                    (utc_now(), workspace_id, url),
                )
                self.extract(
                    workspace_id, url, search_keyword=search_keyword, title_hint=title_hint, force=True,
                )
            except Exception as exc:
                self.db.execute(
                    """UPDATE xhs_post_extractions
                    SET status='failed',failure_stage='worker_error',error_message=?,updated_at=?
                    WHERE workspace_id=? AND url=?""",
                    (f"自动提取失败：{str(exc)[:500]}", utc_now(), workspace_id, url),
                )
            finally:
                with self._queue_lock:
                    self._queued.discard(key)
                self._jobs.task_done()

    @staticmethod
    def decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for column, (public_name, fallback) in JSON_COLUMNS.items():
            value[public_name] = json_loads(value.pop(column, None), fallback)
        screenshot = value.pop("screenshot_path", "")
        value["screenshot_url"] = f"/api/topic-radar/xhs/captures/{screenshot}" if screenshot else ""
        return value

    def list(self, workspace_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? ORDER BY extracted_at DESC LIMIT ?",
            (workspace_id, max(1, min(limit, 200))),
        )
        return [self.decode(row) or {} for row in rows]

    def get(self, workspace_id: str, url: str) -> dict[str, Any] | None:
        return self.decode(self.db.one(
            "SELECT * FROM xhs_post_extractions WHERE workspace_id=? AND url=?",
            (workspace_id, url),
        ))

    def extract(
        self,
        workspace_id: str,
        url: str,
        *,
        search_keyword: str = "",
        title_hint: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        existing = self.db.one(
            "SELECT id,retry_count FROM xhs_post_extractions WHERE workspace_id=? AND url=?",
            (workspace_id, url),
        )
        existing_full = self.get(workspace_id, url)
        if existing_full and existing_full.get("status") == "success" and existing_full.get("merged_copy") and not force:
            return existing_full
        extraction_id = existing["id"] if existing else new_id("xhs")
        retry_count = int((existing or {}).get("retry_count") or 0) + (1 if existing else 0)
        now = utc_now()
        try:
            result = self.collector.extract_xiaohongshu_detail(
                url, search_keyword=search_keyword, title_hint=title_hint,
            )
        except PlatformBrowserError as exc:
            status_by_code = {
                "browser_not_running": "needs_browser",
                "login_required": "needs_login",
                "verification_required": "needs_verification",
                "platform_limited": "platform_limited",
                "post_unavailable": "unavailable",
                "invalid_post_url": "invalid",
            }
            result = {
                "url": url,
                "post_key": "",
                "title": title_hint,
                "status": status_by_code.get(exc.code, "failed"),
                "failure_stage": exc.code,
                "error_message": str(exc),
                "diagnostics": {"error_code": exc.code, **getattr(exc, "diagnostics", {})},
            }
        values = {
            "id": extraction_id,
            "workspace_id": workspace_id,
            "url": str(result.get("url") or url),
            "post_key": str(result.get("post_key") or ""),
            "search_keyword": search_keyword.strip()[:180],
            "title": str(result.get("title") or title_hint)[:500],
            "author": str(result.get("author") or "")[:300],
            "published_at": result.get("published_at") or None,
            "body": str(result.get("body") or "")[:30000],
            "image_text": str(result.get("image_text") or "")[:30000],
            "video_subtitle": str(result.get("video_subtitle") or "")[:30000],
            "video_speech": str(result.get("video_speech") or "")[:30000],
            "merged_copy": str(result.get("merged_copy") or "")[:60000],
            "tags_json": json_dumps(result.get("tags") or []),
            "metrics_json": json_dumps(result.get("metrics") or {}),
            "screenshot_path": str(result.get("screenshot_name") or ""),
            "status": str(result.get("status") or "failed")[:60],
            "completeness": max(0, min(int(result.get("completeness") or 0), 100)),
            "field_sources_json": json_dumps(result.get("field_sources") or {}),
            "ocr_status": str(result.get("ocr_status") or "not_needed")[:60],
            "ocr_message": str(result.get("ocr_message") or "")[:1000],
            "asr_status": str(result.get("asr_status") or "not_needed")[:60],
            "asr_message": str(result.get("asr_message") or "")[:1000],
            "failure_stage": str(result.get("failure_stage") or "")[:100],
            "error_message": str(result.get("error_message") or "")[:2000],
            "diagnostics_json": json_dumps(result.get("diagnostics") or {}),
            "retry_count": retry_count,
            "extracted_at": now,
            "updated_at": now,
        }
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in {"id", "workspace_id"})
        self.db.execute(
            f"INSERT INTO xhs_post_extractions ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(workspace_id,url) DO UPDATE SET {updates}",
            tuple(values[column] for column in columns),
        )
        return self.get(workspace_id, values["url"]) or {}


xhs_extraction_service = XiaohongshuExtractionService()
