from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.app.main as main_module
from backend.app.db import Database, json_dumps, new_id, utc_now
from backend.app.life_cases import LifeCaseService


def make_png(color: str = "orange") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(output, format="PNG")
    return output.getvalue()


class WorkerSpy:
    def __init__(self):
        self.wake_count = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def wake(self) -> None:
        self.wake_count += 1


class ModelStatus:
    def __init__(self, usable: bool = False):
        self.usable = usable

    def profile_is_usable(self, workspace_id: str, role_key: str = "writer") -> bool:
        return self.usable


@pytest.fixture()
def api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("WORKBENCH_DISABLE_WORKERS", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = Database(tmp_path / "api.sqlite3")
    db.initialize()
    cases = LifeCaseService(db=db, root=tmp_path / "life-cases")
    worker = WorkerSpy()
    model = ModelStatus(False)
    monkeypatch.setattr(main_module, "database", db)
    monkeypatch.setattr(main_module, "life_case_service", cases)
    monkeypatch.setattr(main_module, "task_worker", worker)
    monkeypatch.setattr(main_module, "model_gateway", model)
    with TestClient(main_module.app) as client:
        yield client, db, cases, worker, model


def create_case(client: TestClient, workspace_id: str, *, note: str = "下班路上看见一束花。") -> dict:
    response = client.post(
        "/api/life-cases",
        data={
            "workspace_id": workspace_id,
            "note_text": note,
            "occurred_at": "2026-09-13",
            "sync_ip": "true",
        },
        files=[("images", ("street.jpg", make_png(), "image/jpeg"))],
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_list_detail_and_original_media_api(api):
    client, db, _, _, _ = api
    workspace = db.create_workspace("生活案例 API")

    case = create_case(client, workspace["id"])
    listed = client.get("/api/life-cases", params={"workspace_id": workspace["id"]})
    detail = client.get(f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]})
    media = client.get(case["media"][0]["media_url"])

    assert listed.status_code == 200 and [item["id"] for item in listed.json()] == [case["id"]]
    assert detail.status_code == 200 and detail.json()["note_text"] == "下班路上看见一束花。"
    assert media.status_code == 200
    assert media.content == make_png()
    assert media.headers["content-type"].startswith("image/png")
    assert media.headers["x-content-type-options"] == "nosniff"
    assert "local_name" not in detail.text and "base64" not in detail.text


def test_create_api_rejects_missing_parts_spoofed_files_and_too_many_images(api):
    client, db, _, _, _ = api
    workspace = db.create_workspace("错误输入")
    fields = {"workspace_id": workspace["id"], "note_text": "有文字", "sync_ip": "true"}

    missing_image = client.post("/api/life-cases", data=fields)
    missing_text = client.post(
        "/api/life-cases",
        data={"workspace_id": workspace["id"]},
        files=[("images", ("a.png", make_png(), "image/png"))],
    )
    spoofed = client.post(
        "/api/life-cases",
        data=fields,
        files=[("images", ("fake.jpg", b"not-an-image", "image/jpeg"))],
    )
    too_many = client.post(
        "/api/life-cases",
        data=fields,
        files=[("images", (f"{index}.png", make_png(), "image/png")) for index in range(10)],
    )

    assert missing_image.status_code == 422
    assert missing_text.status_code == 422
    assert spoofed.status_code == 422 and "损坏或格式" in spoofed.text
    assert too_many.status_code == 422 and "1—9" in too_many.text
    assert db.one("SELECT COUNT(*) AS n FROM life_cases")["n"] == 0


def test_generate_without_model_keeps_case_and_creates_resumable_task(api):
    client, db, cases, worker, _ = api
    workspace = db.create_workspace("等待模型")
    case = create_case(client, workspace["id"])

    response = client.post(
        f"/api/life-cases/{case['id']}/generate", params={"workspace_id": workspace["id"]},
    )

    assert response.status_code == 202
    detail = response.json()
    task = detail["task"]
    assert task["status"] == "NEEDS_MODEL"
    assert task["stage"] == "RESEARCHER"
    assert task["source_case_id"] == case["id"]
    assert task["web_research"] is False
    assert task["constraints"]["product_mode"] == "moments_life_case"
    assert task["constraints"]["output_angles"] == ["daily", "reflection", "soft_business"]
    assert detail["source_case"]["id"] == case["id"]
    assert worker.wake_count == 0
    path, _ = cases.original_path(case["id"], case["media"][0]["id"])
    assert path.read_bytes() == make_png()


def test_retry_rejects_needs_model_task_after_source_case_is_deleted_and_keeps_draft(api):
    client, db, _, worker, _ = api
    workspace = db.create_workspace("删除后不能重试")
    case = create_case(client, workspace["id"])
    generated = client.post(
        f"/api/life-cases/{case['id']}/generate",
        params={"workspace_id": workspace["id"]},
    )
    assert generated.status_code == 202
    task_id = generated.json()["task"]["id"]
    draft_id, now = new_id("draft"), utc_now()
    db.execute(
        """INSERT INTO draft_versions
        (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            draft_id,
            task_id,
            workspace["id"],
            1,
            "USER_EDIT",
            json_dumps({"body": "删除素材后仍应保留的文案"}),
            "删除素材后仍应保留的文案",
            1,
            now,
        ),
    )

    deleted = client.delete(
        f"/api/life-cases/{case['id']}",
        params={"workspace_id": workspace["id"]},
    )
    retried = client.post(f"/api/copy-tasks/{task_id}/retry")

    assert deleted.status_code == 204
    assert retried.status_code == 409
    assert "永久删除" in retried.text
    task = db.one("SELECT status,source_case_id,constraints_json FROM copy_tasks WHERE id=?", (task_id,))
    assert task["status"] == "NEEDS_MODEL"
    assert task["source_case_id"] is None
    assert json.loads(task["constraints_json"])["source_case_deleted"] is True
    assert db.one("SELECT body_text FROM draft_versions WHERE id=?", (draft_id,))["body_text"] == "删除素材后仍应保留的文案"
    assert worker.wake_count == 0


def test_generate_and_regenerate_preserve_case_and_all_settings(api):
    client, db, _, worker, model = api
    workspace = db.create_workspace("重新生成")
    case = create_case(client, workspace["id"])
    model.usable = True
    first = client.post(
        f"/api/life-cases/{case['id']}/generate", params={"workspace_id": workspace["id"]},
    )
    assert first.status_code == 202
    first_task = first.json()["task"]

    second = client.post(f"/api/copy-tasks/{first_task['id']}/regenerate")

    assert second.status_code == 202
    second_task = second.json()["task"]
    assert second_task["id"] != first_task["id"]
    assert second_task["source_case_id"] == first_task["source_case_id"] == case["id"]
    assert second_task["topic"] == first_task["topic"]
    assert second_task["goal"] == first_task["goal"]
    assert second_task["constraints"] == first_task["constraints"]
    assert second_task["web_research"] is False
    assert worker.wake_count == 2


def test_archive_restore_and_permanent_delete_via_api_keep_finished_work(api):
    client, db, cases, _, _ = api
    workspace = db.create_workspace("归档删除")
    case = create_case(client, workspace["id"])

    archived = client.patch(
        f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]}, json={"archived": True},
    )
    default_list = client.get("/api/life-cases", params={"workspace_id": workspace["id"]})
    archive_list = client.get(
        "/api/life-cases", params={"workspace_id": workspace["id"], "include_archived": "true"},
    )
    restored = client.patch(
        f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]}, json={"archived": False},
    )
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert default_list.json() == []
    assert archive_list.json()[0]["id"] == case["id"]
    assert restored.status_code == 200 and restored.json()["archived"] is False

    task_id, draft_id, now = new_id("task"), new_id("draft"), utc_now()
    db.execute(
        """INSERT INTO copy_tasks
        (id,workspace_id,content_type,platform,topic,goal,offer,constraints_json,source_urls_json,
         web_research,status,stage,source_case_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'[]',0,'FINAL_READY','FINAL',?,?,?)""",
        (
            task_id, workspace["id"], "moments", "微信朋友圈", case["title"], "生活记录", "",
            json_dumps({"product_mode": "moments_life_case", "target_length": [80, 500]}),
            case["id"], now, now,
        ),
    )
    db.execute(
        """INSERT INTO draft_versions
        (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (draft_id, task_id, workspace["id"], 1, "EDITOR", json_dumps({"body": "留下来的成稿"}), "留下来的成稿", 1, now),
    )
    folder = cases.root / case["id"]
    deleted = client.delete(f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]})

    assert deleted.status_code == 204
    assert not folder.exists()
    assert client.get(
        f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]},
    ).status_code == 404
    assert db.one("SELECT source_case_id FROM copy_tasks WHERE id=?", (task_id,))["source_case_id"] is None
    assert db.one("SELECT body_text FROM draft_versions WHERE id=?", (draft_id,))["body_text"] == "留下来的成稿"
    assert client.post(f"/api/copy-tasks/{task_id}/regenerate").status_code == 409


def test_case_lists_do_not_mix_workspaces(api):
    client, db, _, _, _ = api
    first = db.create_workspace("品牌甲")
    second = db.create_workspace("品牌乙")
    first_case = create_case(client, first["id"], note="甲品牌的早餐")
    second_case = create_case(client, second["id"], note="乙品牌的散步")

    first_rows = client.get("/api/life-cases", params={"workspace_id": first["id"]}).json()
    second_rows = client.get("/api/life-cases", params={"workspace_id": second["id"]}).json()

    assert [item["id"] for item in first_rows] == [first_case["id"]]
    assert [item["id"] for item in second_rows] == [second_case["id"]]
    assert first_rows[0]["note_text"] != second_rows[0]["note_text"]


def test_case_id_endpoints_hide_other_workspaces(api):
    client, db, _, _, _ = api
    owner = db.create_workspace("素材所有者")
    other = db.create_workspace("另一个品牌")
    case = create_case(client, owner["id"], note="只属于素材所有者的记录")
    media_id = case["media"][0]["id"]

    assert client.get(
        f"/api/life-cases/{case['id']}", params={"workspace_id": other["id"]},
    ).status_code == 404
    assert client.get(
        f"/api/life-cases/{case['id']}/media/{media_id}", params={"workspace_id": other["id"]},
    ).status_code == 404
    assert client.post(
        f"/api/life-cases/{case['id']}/generate", params={"workspace_id": other["id"]},
    ).status_code == 404
    assert client.patch(
        f"/api/life-cases/{case['id']}", params={"workspace_id": other["id"]}, json={"archived": True},
    ).status_code == 404
    assert client.delete(
        f"/api/life-cases/{case['id']}", params={"workspace_id": other["id"]},
    ).status_code == 404
    owner_detail = client.get(
        f"/api/life-cases/{case['id']}", params={"workspace_id": owner["id"]},
    )
    assert owner_detail.status_code == 200
    assert owner_detail.json()["archived"] is False


def test_permanent_delete_waits_for_running_generation(api):
    client, db, cases, _, _ = api
    workspace = db.create_workspace("处理中的案例")
    case = create_case(client, workspace["id"])
    task_id, now = new_id("task"), utc_now()
    db.execute(
        """INSERT INTO copy_tasks
        (id,workspace_id,content_type,platform,topic,goal,offer,constraints_json,source_urls_json,
         web_research,status,stage,source_case_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'[]',0,'RUNNING','WRITER',?,?,?)""",
        (
            task_id, workspace["id"], "moments", "微信朋友圈", case["title"], "生活记录", "",
            json_dumps({"product_mode": "moments_life_case"}), case["id"], now, now,
        ),
    )

    blocked = client.delete(
        f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]},
    )

    assert blocked.status_code == 409 and "正在处理" in blocked.text
    assert cases.get(case["id"], workspace["id"])["id"] == case["id"]
    assert (cases.root / case["id"]).is_dir()
    db.execute("UPDATE copy_tasks SET status='CANCELLED',stage='CANCELLED' WHERE id=?", (task_id,))
    deleted = client.delete(
        f"/api/life-cases/{case['id']}", params={"workspace_id": workspace["id"]},
    )
    assert deleted.status_code == 204


def test_adopting_a_life_variant_creates_a_new_final_version(api):
    client, db, _, _, _ = api
    workspace = db.create_workspace("采用文案版本")
    case = create_case(client, workspace["id"])
    task_id, original_id, now = new_id("task"), new_id("draft"), utc_now()
    variants = [
        {"angle": "daily", "label": "真实日常", "body": "真实日常正文", "rationale": "记录", "recommended": True},
        {"angle": "reflection", "label": "有感而发", "body": "有感而发正文", "rationale": "感受", "recommended": False},
        {"angle": "soft_business", "label": "轻度业务启发", "body": "轻度业务正文", "rationale": "启发", "recommended": False},
    ]
    original_package = {
        "title": case["title"], "alternative_titles": [], "body": variants[0]["body"],
        "platform_variants": {item["label"]: item["body"] for item in variants},
        "cta": "", "tags": [], "claims": [], "image_briefs": [],
        "deliverables": {
            "moments_posts": [],
            "xiaohongshu_publish": {"title": "", "body": "", "tags": []},
            "douyin_script": {"hook": "", "script": "", "emotion_beats": [], "ending": ""},
            "life_case_variants": variants,
        },
        "conversion_structure": {"hook": "", "trust": "", "proof": "", "objection": "", "action": ""},
    }
    db.execute(
        """INSERT INTO copy_tasks
        (id,workspace_id,content_type,platform,topic,goal,offer,constraints_json,source_urls_json,
         web_research,status,stage,source_case_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'[]',0,'FINAL_READY','FINAL',?,?,?)""",
        (
            task_id, workspace["id"], "moments", "微信朋友圈", case["title"], "生活记录", "",
            json_dumps({"product_mode": "moments_life_case"}), case["id"], now, now,
        ),
    )
    db.execute(
        """INSERT INTO draft_versions
        (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (original_id, task_id, workspace["id"], 1, "EDITOR_FINAL", json_dumps(original_package), variants[0]["body"], 1, now),
    )
    # Keep the old recommendation flags deliberately: the backend must infer the
    # selected variant from body and enforce one recommendation itself.
    adopted_variants = [{**item} for item in variants]
    adopted_package = {
        **original_package,
        "body": variants[1]["body"],
        "deliverables": {**original_package["deliverables"], "life_case_variants": adopted_variants},
    }

    response = client.post(f"/api/drafts/{original_id}/edit", json=adopted_package)

    assert response.status_code == 201, response.text
    created = response.json()
    rows = db.all("SELECT id,version,origin,is_final,package_json FROM draft_versions WHERE task_id=? ORDER BY version", (task_id,))
    assert created["parent_version_id"] == original_id
    assert [(row["version"], row["is_final"]) for row in rows] == [(1, 0), (2, 1)]
    assert rows[0]["origin"] == "EDITOR_FINAL" and rows[1]["origin"] == "USER_EDIT"
    assert json.loads(rows[0]["package_json"])["body"] == "真实日常正文"
    latest = json.loads(rows[1]["package_json"])
    assert latest["body"] == "有感而发正文"
    assert next(item for item in latest["deliverables"]["life_case_variants"] if item["recommended"])["angle"] == "reflection"
    assert sum(bool(item["recommended"]) for item in latest["deliverables"]["life_case_variants"]) == 1

    manual_body = "这是我在原稿基础上手动补充的新正文，系统应该把它同步进当前采用的有感而发版本。"
    manual_package = {
        **latest,
        "body": manual_body,
    }
    manual_response = client.post(f"/api/drafts/{created['id']}/edit", json=manual_package)
    assert manual_response.status_code == 201, manual_response.text
    manual = json.loads(
        db.one("SELECT package_json FROM draft_versions WHERE id=?", (manual_response.json()["id"],))["package_json"]
    )
    manual_variants = manual["deliverables"]["life_case_variants"]
    recommended = [item for item in manual_variants if item["recommended"]]
    assert len(recommended) == 1
    assert recommended[0]["angle"] == "reflection"
    assert manual["body"] == recommended[0]["body"] == manual_body
