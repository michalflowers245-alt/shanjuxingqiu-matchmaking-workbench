from __future__ import annotations

import io
import sqlite3
import time
from pathlib import Path

import pytest
from PIL import Image

from backend.app.db import Database, utc_now
from backend.app.platform_browser import PlatformBrowserError
from backend.app.xhs_archive import XiaohongshuArchiveRepository, XiaohongshuArchiveService
from backend.app.xhs_extractions import XiaohongshuExtractionService
from scripts.migrate_xhs_archive_v2 import migrate


POST_ID = "68c6f07d000000001d01abcd"
POST_URL = f"https://www.xiaohongshu.com/explore/{POST_ID}"


def image_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 32), color).save(output, format="PNG")
    return output.getvalue()


def setup_db(tmp_path: Path) -> tuple[Database, dict, str]:
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    workspace = db.create_workspace("归档测试")
    extraction_id = "xhs_test_extraction"
    now = utc_now()
    db.execute(
        """INSERT INTO xhs_post_extractions
        (id,workspace_id,url,post_key,title,status,extracted_at,updated_at)
        VALUES(?,?,?,?,?,'partial',?,?)""",
        (extraction_id, workspace["id"], POST_URL, POST_ID, "同标题不等于同帖子", now, now),
    )
    return db, workspace, extraction_id


def capture(image_count: int = 6, *, missing: set[int] | None = None) -> dict:
    missing = missing or set()
    raw = "第一行  保留两个空格\n\n第二行🙂\n#原话题"
    assets = []
    for ordinal in range(1, image_count + 1):
        assets.append({
            "role": "carousel",
            "ordinal": ordinal,
            "source_url": f"https://ci.xiaohongshu.test/images/{ordinal}.png?token=secret",
            "source_kind": "embedded_state+network_response",
            "ordered_by": "page_state",
            "mime_declared": "image/png",
            "http_status": 200 if ordinal not in missing else 503,
            "bytes": None if ordinal in missing else image_bytes((ordinal * 20, 10, 80)),
            "error_code": "HTTP_503" if ordinal in missing else "",
        })
    assets.append({
        "role": "cover",
        "ordinal": 1,
        "source_url": "https://ci.xiaohongshu.test/cover.png?token=secret",
        "source_kind": "og_image+network_response",
        "ordered_by": "cover_metadata",
        "mime_declared": "image/png",
        "http_status": 200,
        "bytes": image_bytes((255, 80, 120)),
    })
    return {
        "target_post_id": POST_ID,
        "observed_post_id": POST_ID,
        "canonical_url": POST_URL,
        "title": "测试图文",
        "author": "作者",
        "body_raw": raw,
        "body_source": "embedded_state",
        "body_verified": True,
        "body_evidence": {"same_post_id": True},
        "expected_image_count": image_count,
        "video_count": 0,
        "assets": assets,
        "diagnostics": {},
    }


def test_archive_preserves_raw_body_and_all_six_images_in_order(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")

    result = repository.persist(workspace["id"], extraction_id, capture())

    assert result.archive_status == "COMPLETE"
    assert result.expected_asset_count == 6
    assert result.saved_asset_count == 6
    version = db.one("SELECT * FROM xhs_archive_versions WHERE id=?", (result.archive_id,))
    archive_dir = tmp_path / "archives" / version["archive_path"]
    assert (archive_dir / "body_raw.txt").read_text() == capture()["body_raw"]
    rows = db.all(
        "SELECT ordinal,status,relative_path,sha256,source_ref FROM xhs_post_assets WHERE archive_id=? AND role='carousel' ORDER BY ordinal",
        (result.archive_id,),
    )
    assert [row["ordinal"] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert all(row["status"] == "SAVED" for row in rows)
    assert all("token=" not in row["source_ref"] for row in rows)
    assert all((archive_dir / row["relative_path"]).read_bytes() for row in rows)
    assert (archive_dir / "manifest.json").is_file()
    assert (archive_dir / "checksums.sha256").is_file()


def test_identity_mismatch_is_rejected_without_writing_archive(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    value = capture()
    value["observed_post_id"] = "different_post"

    with pytest.raises(PlatformBrowserError) as error:
        repository.persist(workspace["id"], extraction_id, value)

    assert error.value.code == "identity_mismatch"
    assert db.one("SELECT id FROM xhs_archive_versions") is None


def test_partial_archive_can_be_completed_and_failed_attempt_does_not_replace_complete(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    partial = repository.persist(workspace["id"], extraction_id, capture(missing={4}))
    assert partial.archive_status == "PARTIAL"
    assert partial.missing_ordinals == [4]

    complete = repository.persist(workspace["id"], extraction_id, capture())
    assert complete.archive_status == "COMPLETE"
    current = db.one("SELECT archive_status,latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert current == {"archive_status": "COMPLETE", "latest_archive_id": complete.archive_id}

    later_partial = repository.persist(workspace["id"], extraction_id, capture(missing={2}))
    assert later_partial.archive_status == "PARTIAL"
    current = db.one("SELECT archive_status,latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))
    assert current == {"archive_status": "COMPLETE", "latest_archive_id": complete.archive_id}
    assert db.one("SELECT COUNT(*) AS count FROM xhs_archive_versions")["count"] == 3


def test_asset_download_is_scoped_to_workspace(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    repository = XiaohongshuArchiveRepository(db, tmp_path / "archives")
    service = XiaohongshuArchiveService(db, collector=object(), root=tmp_path / "archives")
    result = repository.persist(workspace["id"], extraction_id, capture())
    asset = db.one("SELECT id FROM xhs_post_assets WHERE archive_id=? AND role='carousel' ORDER BY ordinal LIMIT 1", (result.archive_id,))

    path, media_type = service.resolve_asset(workspace["id"], result.archive_id, asset["id"])
    assert path.is_file()
    assert media_type == "image/png"
    with pytest.raises(FileNotFoundError):
        service.resolve_asset("ws_other", result.archive_id, asset["id"])


class FakeArchiveCollector:
    def __init__(self, value: dict | Exception):
        self.value = value

    def archive_xiaohongshu_detail(self, *_args, **_kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class BlockingArchiveCollector:
    def archive_xiaohongshu_detail(self, *_args, **_kwargs):
        time.sleep(0.2)
        return capture()


def wait_job(service: XiaohongshuArchiveService, workspace_id: str, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    job = service.get_job(workspace_id, job_id)
    while job and job["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
        time.sleep(0.02)
        job = service.get_job(workspace_id, job_id)
    return job or {}


def test_persistent_job_pauses_for_verification_and_resumes(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    collector = FakeArchiveCollector(PlatformBrowserError("请完成验证", code="verification_required"))
    service = XiaohongshuArchiveService(db, collector=collector, root=tmp_path / "archives")
    service.start()
    try:
        first = service.enqueue(workspace["id"], extraction_id)
        paused = wait_job(service, workspace["id"], first["id"])
        assert paused["status"] == "PAUSED"
        assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'")

        collector.value = capture()
        resumed = service.enqueue(workspace["id"], extraction_id, resume_after_user_action=True)
        completed = wait_job(service, workspace["id"], resumed["id"])
        assert completed["status"] == "COMPLETE"
        assert db.one("SELECT reason FROM xhs_browser_pause WHERE profile_id='social_browser'") is None
    finally:
        service.stop()


def test_stalled_browser_call_reaches_explicit_timeout(tmp_path: Path):
    db, workspace, extraction_id = setup_db(tmp_path)
    service = XiaohongshuArchiveService(
        db,
        collector=BlockingArchiveCollector(),
        root=tmp_path / "archives",
        collector_timeout_seconds=0.02,
    )
    service.start()
    try:
        created = service.enqueue(workspace["id"], extraction_id)
        failed = wait_job(service, workspace["id"], created["id"])
        assert failed["status"] == "FAILED"
        assert failed["error_code"] == "ARCHIVE_TIMEOUT"
        assert "任务已停止" in failed["progress"]["message"]
        assert db.one("SELECT latest_archive_id FROM xhs_post_extractions WHERE id=?", (extraction_id,))["latest_archive_id"] is None
    finally:
        service.stop()


def test_migration_runner_is_idempotent(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version','7');
            CREATE TABLE xhs_post_extractions(
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              url TEXT NOT NULL,
              UNIQUE(workspace_id,url)
            );
            """
        )
    first = migrate(path)
    second = migrate(path)
    assert first["schema_version"] == "8"
    assert "body_raw" in first["added_columns"]
    assert second["added_columns"] == []
    assert second["integrity"] == "ok"


def test_direct_archive_record_does_not_run_legacy_ocr_extractor(tmp_path: Path):
    db = Database(tmp_path / "workbench.sqlite3")
    db.initialize()
    workspace = db.create_workspace("直接归档测试")

    class LegacyCollectorMustNotRun:
        def extract_xiaohongshu_detail(self, *_args, **_kwargs):
            raise AssertionError("strict archive setup must not invoke legacy extraction")

    service = XiaohongshuExtractionService(db, collector=LegacyCollectorMustNotRun())
    record = service.ensure_archive_record(
        workspace["id"], POST_URL, search_keyword="测试主题", title_hint="直接保存原图与正文",
    )

    assert record["status"] == "ready_to_archive"
    assert record["url"] == POST_URL
    assert record["search_keyword"] == "测试主题"
