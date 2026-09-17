from __future__ import annotations

from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

import backend.app.main as main_module
from backend.app.db import Database
from backend.app.platform_browser import PlatformBrowserCollector, PlatformBrowserError, _best_xhs_search_result, _safe_url
from backend.app.xhs_extractions import XiaohongshuExtractionService


POST_URL = "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"


class FakeDetailCollector:
    def __init__(self, *, failure: PlatformBrowserError | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []
        self.contexts: list[tuple[str, str]] = []

    def extract_xiaohongshu_detail(self, url: str, *, search_keyword: str = "", title_hint: str = ""):
        self.calls.append(url)
        self.contexts.append((search_keyword, title_hint))
        if self.failure:
            raise self.failure
        return {
            "url": url,
            "post_key": "68c6f07d000000001d01abcd",
            "title": "第一次参加线下相亲",
            "author": "小豆子",
            "published_at": "2026-09-15",
            "body": "这是详情页正文。",
            "image_text": "这是图片里的文字。",
            "video_subtitle": "这是页面可见字幕。",
            "video_speech": "",
            "merged_copy": "这是详情页正文。\n\n这是图片里的文字。\n\n这是页面可见字幕。",
            "tags": ["#相亲", "#长沙脱单"],
            "metrics": {"likes": 1200},
            "screenshot_name": "68c6f07d000000001d01abcd/page.png",
            "status": "success",
            "completeness": 100,
            "field_sources": {"body": "网页 DOM", "image_text": "macOS Vision OCR"},
            "ocr_status": "success",
            "ocr_message": "已识别图片文字。",
            "asr_status": "subtitle_available",
            "asr_message": "已读取页面可见字幕。",
            "diagnostics": {"image_count": 1, "video_count": 1},
        }


def test_xhs_extraction_persists_structured_full_copy(tmp_path: Path):
    db = Database(tmp_path / "xhs.sqlite3")
    db.initialize()
    workspace = db.create_workspace("小红书提取")
    collector = FakeDetailCollector()
    service = XiaohongshuExtractionService(db=db, collector=collector)

    result = service.extract(workspace["id"], POST_URL, search_keyword="长沙相亲")

    assert result["status"] == "success"
    assert result["body"] == "这是详情页正文。"
    assert result["image_text"] == "这是图片里的文字。"
    assert result["merged_copy"].endswith("这是页面可见字幕。")
    assert result["tags"] == ["#相亲", "#长沙脱单"]
    assert result["metrics"] == {"likes": 1200}
    assert result["screenshot_url"].endswith("/68c6f07d000000001d01abcd/page.png")
    assert service.list(workspace["id"])[0]["url"] == POST_URL
    assert collector.contexts[0] == ("长沙相亲", "")

    repeated = service.extract(workspace["id"], POST_URL)
    assert repeated["id"] == result["id"]
    assert repeated["retry_count"] == 0
    assert len(collector.calls) == 1

    forced = service.extract(workspace["id"], POST_URL, force=True)
    assert forced["retry_count"] == 1
    assert len(collector.calls) == 2


def test_xhs_extraction_saves_actionable_failure(tmp_path: Path):
    db = Database(tmp_path / "xhs-failure.sqlite3")
    db.initialize()
    workspace = db.create_workspace("小红书失败状态")
    service = XiaohongshuExtractionService(
        db=db,
        collector=FakeDetailCollector(failure=PlatformBrowserError("请扫码登录后重试", code="login_required")),
    )

    result = service.extract(workspace["id"], POST_URL, title_hint="原帖标题")

    assert result["status"] == "needs_login"
    assert result["title"] == "原帖标题"
    assert result["failure_stage"] == "login_required"
    assert "扫码登录" in result["error_message"]
    assert result["diagnostics"] == {"error_code": "login_required"}


def test_radar_results_queue_and_extract_xhs_copy_automatically(tmp_path: Path):
    db = Database(tmp_path / "xhs-auto.sqlite3")
    db.initialize()
    workspace = db.create_workspace("自动提取")
    collector = FakeDetailCollector()
    service = XiaohongshuExtractionService(db=db, collector=collector)
    service.start()
    try:
        added = service.enqueue_results(workspace["id"], {
            "topic": "线下相亲",
            "sources": [{
                "platform": "xiaohongshu",
                "url": POST_URL + "?xsec_token=temporary",
                "title": "第一次参加线下相亲",
                "metadata": {"platform": "xiaohongshu"},
            }],
        })
        assert added == 1
        deadline = time.monotonic() + 3
        result = None
        while time.monotonic() < deadline:
            result = service.get(workspace["id"], POST_URL)
            if result and result["status"] == "success":
                break
            time.sleep(0.02)
        assert result and result["status"] == "success"
        assert result["merged_copy"]
        assert result["url"] == POST_URL
        assert collector.contexts == [("线下相亲", "第一次参加线下相亲")]
    finally:
        service.stop()


def test_xhs_extraction_http_api_uses_original_workbench_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "xhs-api.sqlite3")
    db.initialize()
    workspace = db.create_workspace("原工作台")
    service = XiaohongshuExtractionService(db=db, collector=FakeDetailCollector())

    class Worker:
        def start(self): pass
        def stop(self): pass
        def wake(self): pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "xhs_extraction_service", service)
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())

    with TestClient(main_module.app) as client:
        response = client.post("/api/topic-radar/xhs/extract", json={
            "workspace_id": workspace["id"],
            "url": POST_URL,
            "search_keyword": "线下相亲",
            "title_hint": "搜索页标题",
        })
        assert response.status_code == 200, response.text
        assert response.json()["extraction"]["merged_copy"]

        listed = client.get("/api/topic-radar/xhs/extractions", params={"workspace_id": workspace["id"]})
        assert listed.status_code == 200
    assert listed.json()[0]["url"] == POST_URL


def test_xhs_search_recovery_prefers_same_post_and_keeps_access_token():
    rows = [
        {
            "href": "https://www.xiaohongshu.com/explore/other-note?xsec_token=wrong&tracking=drop",
            "title": "相似但不是同一篇",
        },
        {
            "href": "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd?xsec_token=right-token&xsec_source=pc_search&tracking=drop",
            "title": "第一次参加线下相亲",
        },
    ]

    resolved = _best_xhs_search_result(rows, "68c6f07d000000001d01abcd", "第一次参加线下相亲")

    assert "xsec_token=right-token" in resolved
    assert "xsec_source=pc_search" in resolved
    assert "tracking" not in resolved


def test_xhs_search_recovery_never_replaces_post_with_similar_title():
    rows = [{
        "href": "https://www.xiaohongshu.com/explore/new-note?xsec_token=new-token",
        "title": "长沙相亲｜近期能面基的来",
    }]

    resolved = _best_xhs_search_result(rows, "old-note", "长沙相亲｜近期能面基的来")

    assert resolved == ""


def test_xhs_visible_search_card_becomes_tokenized_original_post_url():
    resolved = _safe_url(
        "xiaohongshu",
        "https://www.xiaohongshu.com/search_result/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=&tracking=drop",
    )

    assert resolved == (
        "https://www.xiaohongshu.com/explore/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=pc_search"
    )


def test_xhs_search_access_url_stays_in_memory_while_public_url_is_clean(tmp_path: Path):
    collector = PlatformBrowserCollector(profile_dir=tmp_path / "profile")
    public_url = collector._remember_xhs_access_url(
        "https://www.xiaohongshu.com/search_result/68c6f07d000000001d01abcd"
        "?xsec_token=access-token&xsec_source=pc_search&tracking=drop"
    )

    assert public_url == POST_URL
    assert collector._recent_xhs_access_url(public_url) == (
        POST_URL + "?xsec_token=access-token&xsec_source=pc_search"
    )
