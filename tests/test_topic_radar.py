from __future__ import annotations

import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

import backend.app.main as main_module
from backend.app.db import Database, json_dumps, json_loads
from backend.app.monitoring import TopicRadarService


class FakePublicSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, workspace_id: str, query: str, limit: int = 8):
        self.calls.append((workspace_id, query, limit))
        return [
            {
                "title": f"{query}：公开趋势样例",
                "url": f"https://example.test/{hashlib.sha256(query.encode()).hexdigest()[:12]}",
                "excerpt": "来自公开搜索的测试线索。",
                "published_at": "2026-09-14",
                "credibility": 0.65,
                "search_mode": "fake_public_search",
            }
        ]


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, platform: str, query: str, limit: int = 12):
        self.calls.append((platform, query, limit))
        source_id = hashlib.sha256(f"{platform}:{query}".encode()).hexdigest()[:12]
        return [{
            "source_key": source_id,
            "title": f"{query}：平台可见热门内容",
            "url": f"https://www.{platform}.com/{'explore' if platform == 'xiaohongshu' else 'video'}/{source_id}",
            "excerpt": "这是浏览器当前搜索页实际显示的文案内容。",
            "author": "平台作者",
            "published_at": None,
            "metrics": {"likes": 321},
            "raw": {"rank": 1, "collection": "visible_search_card"},
        }]


class MixedHeatBrowser:
    def search(self, platform: str, query: str, limit: int = 12):
        prefix = "explore" if platform == "xiaohongshu" else "video"
        host = "xiaohongshu" if platform == "xiaohongshu" else "douyin"
        rows = [
            ("unrelated", "相亲相爱电竞夺冠", "赛事集锦", 999999),
            ("warm", "长沙相亲真实体验", "线下活动复盘", 120),
            ("hot", "长沙线下相亲活动避坑", "相亲参与者最在意什么", 8800),
        ]
        return [{
            "source_key": f"{platform}-{key}",
            "title": title,
            "url": f"https://www.{host}.com/{prefix}/{platform}-{key}",
            "excerpt": excerpt,
            "author": "作者",
            "published_at": None,
            "metrics": {"likes": likes},
            "raw": {"rank": index, "collection": "visible_search_card"},
        } for index, (key, title, excerpt, likes) in enumerate(rows, start=1)]


def test_ip_daily_radar_derives_only_broad_terms_and_never_saves_profile_text(tmp_path: Path):
    db = Database(tmp_path / "daily-radar.sqlite3")
    db.initialize()
    public_search = FakePublicSearch()
    service = TopicRadarService(
        db=db, public_search=public_search, browser_collector=FakeBrowser(),
    )
    workspace = db.create_workspace("IP 每日热点")
    private_profile_phrase = "我的姓名和联系方式绝不应该进入搜索或结果"
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": f"我在长沙做相亲婚恋内容，关注线下交友和脱单。{private_profile_phrase}"}), workspace["id"]),
    )

    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    assert monitor["monitor_kind"] == "ip_daily"
    assert monitor["platforms_json"] == '["web"]'
    assert service.ensure_ip_daily_monitor(workspace["id"])["id"] == monitor["id"]

    run = service.run_monitor(monitor["id"], workspace["id"])
    summary = json_loads(run["summary_json"], {})
    assert run["status"] == "DONE"
    assert summary["monitor_kind"] == "ip_daily"
    assert summary["profile_based"] is True
    assert [item["platform"] for item in summary["platforms"]] == ["xiaohongshu", "douyin"]
    recommendations = summary["recommendations"]
    assert len(recommendations) >= 6
    assert {item["persona"] for item in recommendations}.issuperset({
        "柚子皮 · 同城线下相亲", "小豆子 · 女性脱单红娘",
    })
    assert public_search.calls == []
    for recommendation in recommendations:
        links = recommendation["platform_search_links"]
        assert [item["platform"] for item in links] == ["douyin", "xiaohongshu"]
        assert [urlsplit(item["url"]).hostname for item in links] == ["www.douyin.com", "www.xiaohongshu.com"]
        assert all(item["url"].startswith("https://") for item in links)
        assert all("example.test" not in url for url in recommendation["source_urls"])
    serialized = json_dumps({"summary": summary, "items": run["items"]})
    assert private_profile_phrase not in serialized

    repeat = service.run_monitor(monitor["id"], workspace["id"])
    assert repeat["items"][0]["is_new"] == 0


def test_one_off_topic_discovery_returns_sources_and_copy_ready_angles(tmp_path: Path):
    db = Database(tmp_path / "discovery.sqlite3")
    db.initialize()
    public_search = FakePublicSearch()
    service = TopicRadarService(
        db=db, public_search=public_search, browser_collector=FakeBrowser(),
    )
    workspace = db.create_workspace("主题搜索")
    result = service.discover_topic(workspace["id"], "同城相亲活动")

    assert result["search_mode"] == "visible_browser"
    assert result["source_count"] == 2
    assert result["visible_source_count"] == 2
    assert result["angles"][0]["topic"] == "同城相亲活动"
    assert [item["platform"] for item in result["platform_search_links"]] == ["douyin", "xiaohongshu"]
    assert result["angles"][0]["source_urls"][0].startswith("https://www.")


def test_topic_discovery_filters_accidental_matches_and_sorts_visible_heat(tmp_path: Path):
    db = Database(tmp_path / "hot-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(
        db=db, public_search=FakePublicSearch(), browser_collector=MixedHeatBrowser(),
    )
    workspace = db.create_workspace("热门筛选")
    result = service.discover_topic(workspace["id"], "长沙 相亲 线下活动")

    assert result["source_count"] == 4
    assert all("电竞" not in item["title"] for item in result["sources"])
    for platform in ("xiaohongshu", "douyin"):
        rows = [item for item in result["sources"] if item["platform"] == platform]
        assert [item["metrics"]["likes"] for item in rows] == [8800, 120]
        assert rows[0]["raw"]["relevance_score"] >= 2


def test_topic_radar_http_api_returns_saved_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = Database(tmp_path / "radar-api.sqlite3")
    db.initialize()
    service = TopicRadarService(
        db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser(),
    )

    class Worker:
        def start(self):
            pass

        def stop(self):
            pass

        def wake(self):
            pass

    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "topic_radar_service", service)
    monkeypatch.setattr(main_module, "topic_radar_worker", Worker())
    workspace = db.create_workspace("API 雷达")

    with TestClient(main_module.app) as client:
        db.execute(
            "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
            (json_dumps({"common_info": "专注同城相亲与婚恋关系内容"}), workspace["id"]),
        )
        daily = client.post("/api/topic-radar/ip-daily/enable", json={"workspace_id": workspace["id"]})
        assert daily.status_code == 201, daily.text
        assert daily.json()["monitor"]["monitor_kind"] == "ip_daily"
        assert daily.json()["latest_run"]["summary"]["monitor_kind"] == "ip_daily"
        daily_refresh = client.post("/api/topic-radar/ip-daily/run", json={"workspace_id": workspace["id"]})
        assert daily_refresh.status_code == 202, daily_refresh.text
        assert daily_refresh.json()["monitor"]["monitor_kind"] == "ip_daily"
        refresh_id = daily_refresh.json()["latest_run"]["id"]
        deadline = time.monotonic() + 2
        refreshed = daily_refresh.json()["latest_run"]
        while refreshed["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
            runs = client.get(
                f"/api/topic-radar/monitors/{daily_refresh.json()['monitor']['id']}/runs",
                params={"workspace_id": workspace["id"], "limit": 5},
            ).json()
            refreshed = next(item for item in runs if item["id"] == refresh_id)
        assert refreshed["status"] == "DONE"
        assert refreshed["summary"]["monitor_kind"] == "ip_daily"
        listed_after_daily = client.get("/api/topic-radar/monitors", params={"workspace_id": workspace["id"]})
        assert {row["monitor_kind"] for row in listed_after_daily.json()} == {"ip_daily"}

        discovered = client.post("/api/topic-radar/topic-search", json={
            "workspace_id": workspace["id"], "topic": "第一次相亲怎么聊天",
        })
        assert discovered.status_code == 200, discovered.text
        source_urls = [item["url"] for item in discovered.json()["platform_search_links"]]
        copy_task = client.post("/api/topic-radar/topic-search/create", json={
            "workspace_id": workspace["id"],
            "topic": "第一次相亲怎么聊天",
            "product_mode": "douyin_emotion",
            "source_urls": source_urls,
            "constraints": {"radar_platform_searches": discovered.json()["platform_search_links"]},
        })
        assert copy_task.status_code == 202, copy_task.text
        task = copy_task.json()["task"]
        assert task["content_type"] == "short_video"
        assert task["platform"] == "抖音"
        assert task["constraints"]["product_mode"] == "douyin_emotion"
        assert task["constraints"]["radar_origin"] == "topic_search"
        assert task["constraints"]["radar_platform_searches"][0]["platform"] == "douyin"

class VerificationBrowser:
    def search(self, platform: str, query: str, limit: int = 12):
        from backend.app.platform_browser import PlatformBrowserError
        raise PlatformBrowserError("平台要求完成安全验证，请完成后重试", code="verification_required")


def test_daily_radar_uses_last_saved_xhs_and_douyin_cards_during_verification(tmp_path: Path):
    db = Database(tmp_path / "cached-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("双平台缓存")
    db.execute(
        "UPDATE brand_profiles SET profile_json=? WHERE workspace_id=?",
        (json_dumps({"common_info": "我在长沙做相亲、脱单与线下交友内容"}), workspace["id"]),
    )
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    first = service.run_monitor(monitor["id"], workspace["id"])
    assert {item["platform"] for item in first["items"]} == {"xiaohongshu", "douyin"}

    service.browser = VerificationBrowser()
    second = service.run_monitor(monitor["id"], workspace["id"])
    summary = json_loads(second["summary_json"], {})
    by_platform = {item["platform"]: item for item in summary["platforms"]}

    assert by_platform["xiaohongshu"]["count"] > 0
    assert by_platform["douyin"]["count"] > 0
    assert by_platform["xiaohongshu"]["source_mode"] == "cached_visible_browser"
    assert {item["source_mode"] for item in second["items"]} == {"cached_visible_browser"}
    evidence_platforms = {item.get("evidence_platform") for item in summary["recommendations"] if item.get("evidence_platform")}
    assert evidence_platforms == {"xiaohongshu", "douyin"}
    assert any("上次成功采集时可见点赞" in item.get("reason", "") for item in summary["recommendations"])


def test_radar_recovers_running_rows_left_by_a_stopped_server(tmp_path: Path):
    db = Database(tmp_path / "interrupted-radar.sqlite3")
    db.initialize()
    service = TopicRadarService(db=db, public_search=FakePublicSearch(), browser_collector=FakeBrowser())
    workspace = db.create_workspace("中断恢复")
    monitor = service.ensure_ip_daily_monitor(workspace["id"])
    db.execute(
        "INSERT INTO topic_monitor_runs(id,monitor_id,workspace_id,status,started_at) VALUES(?,?,?,'RUNNING',?)",
        ("monitor_run_interrupted", monitor["id"], workspace["id"], "2026-09-15T00:00:00+00:00"),
    )
    db.execute("UPDATE topic_monitors SET last_status='RUNNING' WHERE id=?", (monitor["id"],))

    assert service.recover_interrupted_runs() == 1
    recovered = service.get_run("monitor_run_interrupted", workspace["id"])
    assert recovered["status"] == "FAILED"
    assert recovered["completed_at"]
    assert "工作台重启" in recovered["error_message"]
