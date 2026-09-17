from __future__ import annotations

import base64
import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import backend.app.life_cases as life_module
from backend.app.db import Database, json_dumps, new_id, utc_now
from backend.app.life_cases import LifeCaseService, validate_image


def image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (48, 32),
    color: tuple[int, int, int] = (42, 120, 210),
    exif: Image.Exif | None = None,
) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", size, color)
    kwargs = {"exif": exif} if exif is not None else {}
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


@pytest.fixture()
def life_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = Database(tmp_path / "life-cases.sqlite3")
    db.initialize()
    return db


@pytest.fixture()
def life_service(life_db: Database, tmp_path: Path) -> LifeCaseService:
    return LifeCaseService(db=life_db, root=tmp_path / "originals")


def test_schema_is_v7_and_life_case_original_is_preserved(
    life_db: Database,
    life_service: LifeCaseService,
):
    workspace = life_db.create_workspace("生活记录")
    original = image_bytes("PNG")

    case = life_service.create(
        workspace_id=workspace["id"],
        note_text=" 下午路过花店，老板把最后一束向日葵送给了小朋友。 ",
        occurred_at="2026-09-13",
        sync_ip=True,
        images=[("../../花店\n照片.png", original)],
    )

    assert life_db.one("SELECT value FROM schema_meta WHERE key='schema_version'")["value"] == "7"
    assert case["title"].startswith("2026-09-13 · 下午路过花店")
    assert case["note_text"].startswith("下午路过花店")
    assert case["media"][0]["display_name"] == "花店 照片.png"
    assert case["media"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert "local_name" not in case["media"][0]
    assert "local_path" not in json.dumps(case, ensure_ascii=False)

    path, _ = life_service.original_path(case["id"], case["media"][0]["id"])
    assert path.read_bytes() == original
    database_bytes = life_db.path.read_bytes()
    assert original not in database_bytes
    assert base64.b64encode(original) not in database_bytes


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not really a jpeg", "图片内容损坏或格式与文件不符"),
        (b"", "图片内容为空"),
    ],
)
def test_image_validation_uses_decoded_content_not_suffix(data: bytes, message: str):
    with pytest.raises(ValueError, match=message):
        validate_image(data, "伪装图片.jpg")


def test_animated_gif_is_rejected():
    first = Image.new("RGB", (24, 24), "red")
    second = Image.new("RGB", (24, 24), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=80, loop=0)

    with pytest.raises(ValueError, match="暂不支持动态 GIF"):
        validate_image(output.getvalue(), "会动.gif")


def test_heif_is_accepted_when_codec_is_installed():
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    data = image_bytes("HEIF")

    validated = validate_image(data, "手机原图.heic")

    assert validated.mime_type == "image/heic"
    assert validated.extension == ".heic"


def test_case_requires_text_and_one_to_nine_images(
    life_db: Database,
    life_service: LifeCaseService,
):
    workspace = life_db.create_workspace("输入校验")
    image = image_bytes()
    common = {
        "workspace_id": workspace["id"],
        "occurred_at": None,
        "sync_ip": True,
    }

    with pytest.raises(ValueError, match="请描述"):
        life_service.create(note_text="  ", images=[("a.png", image)], **common)
    with pytest.raises(ValueError, match="1—9"):
        life_service.create(note_text="有文字但没图片", images=[], **common)
    with pytest.raises(ValueError, match="1—9"):
        life_service.create(
            note_text="一次传了太多图片",
            images=[(f"{index}.png", image) for index in range(10)],
            **common,
        )


def test_image_per_file_and_total_limits_are_checked_before_writing(
    life_db: Database,
    life_service: LifeCaseService,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = life_db.create_workspace("大小校验")
    image = image_bytes()
    monkeypatch.setattr(life_module, "MAX_IMAGE_BYTES", len(image) - 1)
    with pytest.raises(ValueError, match="单张图片不能超过"):
        life_service.create(
            workspace_id=workspace["id"], note_text="单张过大", occurred_at=None,
            sync_ip=True, images=[("large.png", image)],
        )
    assert not list(life_service.root.iterdir())

    monkeypatch.setattr(life_module, "MAX_IMAGE_BYTES", len(image) + 1)
    monkeypatch.setattr(life_module, "MAX_TOTAL_BYTES", len(image) * 2 - 1)
    with pytest.raises(ValueError, match="合计不能超过"):
        life_service.create(
            workspace_id=workspace["id"], note_text="合计过大", occurred_at=None,
            sync_ip=True, images=[("1.png", image), ("2.png", image)],
        )
    assert not list(life_service.root.iterdir())


def test_model_derivative_is_bounded_jpeg_without_exif_and_not_persisted(
    life_db: Database,
    life_service: LifeCaseService,
):
    workspace = life_db.create_workspace("隐私处理")
    exif = Image.Exif()
    exif[270] = "sensitive-description"
    exif[274] = 6
    original = image_bytes("JPEG", size=(2200, 120), exif=exif)
    case = life_service.create(
        workspace_id=workspace["id"], note_text="一张带拍摄信息的横图", occurred_at=None,
        sync_ip=False, images=[("phone.jpg", original)],
    )
    before = {path.name for path in (life_service.root / case["id"]).iterdir()}

    values = life_service.model_image_inputs(case["id"])

    assert len(values) == 1 and values[0].startswith("data:image/jpeg;base64,")
    derivative = base64.b64decode(values[0].split(",", 1)[1])
    with Image.open(io.BytesIO(derivative)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= life_module.MODEL_IMAGE_LONG_EDGE
        assert not image.getexif()
    after = {path.name for path in (life_service.root / case["id"]).iterdir()}
    assert after == before
    path, _ = life_service.original_path(case["id"], case["media"][0]["id"])
    assert path.read_bytes() == original


def test_cases_are_workspace_isolated_and_archive_is_reversible(
    life_db: Database,
    life_service: LifeCaseService,
):
    first = life_db.create_workspace("品牌甲")
    second = life_db.create_workspace("品牌乙")
    image = image_bytes()
    first_case = life_service.create(
        workspace_id=first["id"], note_text="甲的日常", occurred_at="2026-09-11",
        sync_ip=True, images=[("a.png", image)],
    )
    second_case = life_service.create(
        workspace_id=second["id"], note_text="乙的日常", occurred_at="2026-09-12",
        sync_ip=True, images=[("b.png", image)],
    )

    assert [item["id"] for item in life_service.list(first["id"])] == [first_case["id"]]
    assert [item["id"] for item in life_service.list(second["id"])] == [second_case["id"]]
    life_service.set_archived(first_case["id"], True)
    assert life_service.list(first["id"]) == []
    assert life_service.list(first["id"], include_archived=True)[0]["archived"] is True
    life_service.set_archived(first_case["id"], False)
    assert life_service.list(first["id"])[0]["archived"] is False


def test_permanent_delete_removes_originals_but_keeps_detached_drafts(
    life_db: Database,
    life_service: LifeCaseService,
):
    workspace = life_db.create_workspace("删除行为")
    case = life_service.create(
        workspace_id=workspace["id"], note_text="准备归档的小事", occurred_at=None,
        sync_ip=True, images=[("a.png", image_bytes())],
    )
    task_id, draft_id, now = new_id("task"), new_id("draft"), utc_now()
    life_db.execute(
        """INSERT INTO copy_tasks
        (id,workspace_id,content_type,platform,topic,goal,offer,constraints_json,source_urls_json,
         web_research,status,stage,source_case_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'{}','[]',0,'FINAL_READY','FINAL',?,?,?)""",
        (task_id, workspace["id"], "moments", "微信朋友圈", case["title"], "记录生活", "", case["id"], now, now),
    )
    life_db.execute(
        """INSERT INTO draft_versions
        (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (draft_id, task_id, workspace["id"], 1, "EDITOR", json_dumps({"body": "保留的成稿"}), "保留的成稿", 1, now),
    )
    folder = life_service.root / case["id"]
    assert folder.is_dir()

    life_service.delete(case["id"])

    assert not folder.exists()
    assert life_db.one("SELECT id FROM life_cases WHERE id=?", (case["id"],)) is None
    assert life_db.one("SELECT source_case_id FROM copy_tasks WHERE id=?", (task_id,))["source_case_id"] is None
    assert life_db.one("SELECT body_text FROM draft_versions WHERE id=?", (draft_id,))["body_text"] == "保留的成稿"


def test_storage_path_rejects_traversal(life_service: LifeCaseService):
    with pytest.raises(ValueError, match="存储路径无效"):
        life_service._case_folder("../outside")
    with pytest.raises(ValueError, match="存储路径无效"):
        life_service._checked_file(life_service.root, "../outside.png")


def test_delete_rolls_original_folder_back_when_database_delete_fails(
    life_db: Database,
    life_service: LifeCaseService,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = life_db.create_workspace("删除回滚")
    original = image_bytes()
    case = life_service.create(
        workspace_id=workspace["id"], note_text="删除时数据库突然失败", occurred_at=None,
        sync_ip=True, images=[("a.png", original)],
    )
    real_transaction = life_db.transaction

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            if sql.strip().startswith("DELETE FROM life_cases"):
                raise RuntimeError("simulated database failure")
            return self.connection.execute(sql, params)

    @contextmanager
    def failing_transaction():
        with real_transaction() as connection:
            yield FailingConnection(connection)

    monkeypatch.setattr(life_db, "transaction", failing_transaction)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        life_service.delete(case["id"], workspace["id"])

    restored = life_service.get(case["id"], workspace["id"])
    path, _ = life_service.original_path(case["id"], restored["media"][0]["id"], workspace["id"])
    assert path.read_bytes() == original
    assert not list(life_service.root.glob(".trash-*"))


def test_service_startup_cleans_only_internal_delete_trash(life_db: Database, tmp_path: Path):
    root = tmp_path / "life-cases"
    stale = root / ".trash-case_stale-delete_stale"
    keep = root / "ordinary-folder"
    stale.mkdir(parents=True)
    keep.mkdir()
    (stale / "old.jpg").write_bytes(b"old")
    (keep / "keep.txt").write_text("keep", encoding="utf-8")

    LifeCaseService(db=life_db, root=root)

    assert not stale.exists()
    assert (keep / "keep.txt").read_text(encoding="utf-8") == "keep"
