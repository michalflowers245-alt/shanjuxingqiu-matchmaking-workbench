from __future__ import annotations

import base64
import hashlib
import io
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - requirements install this in production
    pass

from .config import LIFE_CASE_DIR
from .db import Database, database, json_dumps, json_loads, new_id, utc_now


MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_IMAGE_COUNT = 9
MAX_IMAGE_PIXELS = 40_000_000
MODEL_IMAGE_LONG_EDGE = 2048

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

_FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
    "HEIF": ("image/heic", ".heic"),
    "HEIC": ("image/heic", ".heic"),
}


@dataclass(frozen=True)
class ValidatedImage:
    display_name: str
    data: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    sha256: str


class LifeCaseBusyError(RuntimeError):
    pass


class LifeCaseCleanupError(RuntimeError):
    pass


def _safe_display_name(value: str | None, extension: str) -> str:
    clean = Path((value or "生活图片").replace("\x00", "")).name.strip()
    clean = re.sub(r"[\r\n\t]+", " ", clean)[:180]
    return clean or f"生活图片{extension}"


def validate_image(data: bytes, display_name: str | None = None) -> ValidatedImage:
    """Validate by decoded bytes rather than trusting the browser MIME or suffix."""
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 12MB")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            image_format = str(probe.format or "").upper()
            if image_format not in _FORMATS:
                raise ValueError("仅支持 JPG、PNG、WebP、静态 GIF、HEIC/HEIF 图片")
            if image_format == "GIF" and bool(getattr(probe, "is_animated", False)):
                raise ValueError("暂不支持动态 GIF，请上传静态图片")
            width, height = probe.size
            if width < 1 or height < 1:
                raise ValueError("图片尺寸无效")
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError("图片像素过大，请压缩到 4000 万像素以内")
            probe.verify()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("图片内容损坏或格式与文件不符") from exc
    mime_type, extension = _FORMATS[image_format]
    return ValidatedImage(
        display_name=_safe_display_name(display_name, extension),
        data=data,
        mime_type=mime_type,
        extension=extension,
        width=int(width),
        height=int(height),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class LifeCaseService:
    def __init__(self, db: Database = database, root: Path = LIFE_CASE_DIR):
        self.db = db
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_trash()

    @staticmethod
    def normalize_date(value: str | None) -> str:
        clean = (value or "").strip()
        if not clean:
            return date.today().isoformat()
        try:
            return date.fromisoformat(clean).isoformat()
        except ValueError as exc:
            raise ValueError("发生日期必须使用 YYYY-MM-DD 格式") from exc

    @staticmethod
    def normalize_note(value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("请描述这件生活小事")
        if len(clean) > 5000:
            raise ValueError("生活描述不能超过 5000 字")
        return clean

    @staticmethod
    def make_title(occurred_at: str, note_text: str) -> str:
        summary = re.sub(r"\s+", " ", note_text).strip()[:24]
        return f"{occurred_at} · {summary}"

    def create(
        self,
        *,
        workspace_id: str,
        note_text: str,
        occurred_at: str | None,
        sync_ip: bool,
        images: Iterable[tuple[str | None, bytes]],
    ) -> dict[str, Any]:
        note = self.normalize_note(note_text)
        event_date = self.normalize_date(occurred_at)
        values = list(images)
        if not 1 <= len(values) <= MAX_IMAGE_COUNT:
            raise ValueError("每个生活案例需要上传 1—9 张图片")
        if sum(len(data) for _, data in values) > MAX_TOTAL_BYTES:
            raise ValueError("全部图片合计不能超过 30MB")
        validated = [validate_image(data, name) for name, data in values]
        case_id, now = new_id("case"), utc_now()
        folder = self._case_folder(case_id)
        folder.mkdir(parents=True, exist_ok=False)
        media_rows: list[tuple[Any, ...]] = []
        try:
            for ordinal, image in enumerate(validated, start=1):
                media_id = new_id("media")
                local_name = f"{media_id}{image.extension}"
                target = self._checked_file(folder, local_name)
                with target.open("xb") as handle:
                    handle.write(image.data)
                media_rows.append(
                    (
                        media_id,
                        case_id,
                        ordinal,
                        image.display_name,
                        image.mime_type,
                        local_name,
                        len(image.data),
                        image.sha256,
                        image.width,
                        image.height,
                        now,
                    )
                )
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO life_cases
                    (id,workspace_id,title,note_text,occurred_at,sync_ip,archived,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,0,?,?)""",
                    (
                        case_id,
                        workspace_id,
                        self.make_title(event_date, note),
                        note,
                        event_date,
                        int(sync_ip),
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    """INSERT INTO life_case_media
                    (id,case_id,ordinal,display_name,mime_type,local_name,byte_size,sha256,width,height,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    media_rows,
                )
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self.get(case_id)

    def list(self, workspace_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_archived else " AND archived=0"
        rows = self.db.all(
            f"""SELECT * FROM life_cases WHERE workspace_id=?{clause}
            ORDER BY occurred_at DESC,created_at DESC""",
            (workspace_id,),
        )
        return [self._public(row) for row in rows]

    def get(self, case_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        if workspace_id is None:
            row = self.db.one("SELECT * FROM life_cases WHERE id=?", (case_id,))
        else:
            row = self.db.one("SELECT * FROM life_cases WHERE id=? AND workspace_id=?", (case_id, workspace_id))
        if not row:
            raise KeyError(case_id)
        return self._public(row)

    def set_archived(self, case_id: str, archived: bool, workspace_id: str | None = None) -> dict[str, Any]:
        try:
            self.get(case_id, workspace_id)
        except KeyError:
            raise KeyError(case_id)
        self.db.execute(
            "UPDATE life_cases SET archived=?,updated_at=? WHERE id=?",
            (int(archived), utc_now(), case_id),
        )
        return self.get(case_id, workspace_id)

    def delete(self, case_id: str, workspace_id: str | None = None) -> None:
        self.get(case_id, workspace_id)
        folder = self._case_folder(case_id)
        trash = self._trash_folder(case_id)
        moved = False
        try:
            with self.db.transaction() as connection:
                if workspace_id is None:
                    current = connection.execute("SELECT id FROM life_cases WHERE id=?", (case_id,)).fetchone()
                else:
                    current = connection.execute(
                        "SELECT id FROM life_cases WHERE id=? AND workspace_id=?",
                        (case_id, workspace_id),
                    ).fetchone()
                if not current:
                    raise KeyError(case_id)
                busy = connection.execute(
                    """SELECT id FROM copy_tasks WHERE source_case_id=?
                    AND status IN ('QUEUED','RUNNING') LIMIT 1""",
                    (case_id,),
                ).fetchone()
                if busy:
                    raise LifeCaseBusyError("案例仍有任务正在处理，请先取消或等待完成")
                linked_tasks = connection.execute(
                    "SELECT id,constraints_json FROM copy_tasks WHERE source_case_id=?",
                    (case_id,),
                ).fetchall()
                for task in linked_tasks:
                    constraints = json_loads(task["constraints_json"], {}) or {}
                    constraints["source_case_deleted"] = True
                    connection.execute(
                        "UPDATE copy_tasks SET constraints_json=?,updated_at=? WHERE id=?",
                        (json_dumps(constraints), utc_now(), task["id"]),
                    )
                if folder.is_dir():
                    folder.rename(trash)
                    moved = True
                connection.execute("DELETE FROM life_cases WHERE id=?", (case_id,))
        except Exception:
            if moved and trash.is_dir() and not folder.exists():
                trash.rename(folder)
            raise
        if trash.is_dir():
            try:
                shutil.rmtree(trash)
            except OSError as exc:
                raise LifeCaseCleanupError(
                    "案例记录已删除，但本地原图仍在受控回收目录中，将在下次启动时继续清理"
                ) from exc

    def delete_workspace_files(self, workspace_id: str) -> None:
        for row in self.db.all("SELECT id FROM life_cases WHERE workspace_id=?", (workspace_id,)):
            folder = self._case_folder(str(row["id"]))
            if folder.is_dir():
                shutil.rmtree(folder)

    def original_path(
        self,
        case_id: str,
        media_id: str,
        workspace_id: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        if workspace_id is None:
            row = self.db.one(
                """SELECT m.*,c.workspace_id FROM life_case_media m
                JOIN life_cases c ON c.id=m.case_id WHERE m.id=? AND m.case_id=?""",
                (media_id, case_id),
            )
        else:
            row = self.db.one(
                """SELECT m.*,c.workspace_id FROM life_case_media m
                JOIN life_cases c ON c.id=m.case_id
                WHERE m.id=? AND m.case_id=? AND c.workspace_id=?""",
                (media_id, case_id, workspace_id),
            )
        if not row:
            raise KeyError(media_id)
        path = self._checked_file(self._case_folder(case_id), str(row["local_name"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, row

    def model_image_inputs(self, case_id: str) -> list[str]:
        """Create EXIF-free, bounded JPEG data URLs in memory; never persist derivatives."""
        rows = self.db.all(
            "SELECT id FROM life_case_media WHERE case_id=? ORDER BY ordinal",
            (case_id,),
        )
        values: list[str] = []
        for row in rows:
            path, _ = self.original_path(case_id, str(row["id"]))
            with Image.open(path) as source:
                source.seek(0)
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((MODEL_IMAGE_LONG_EDGE, MODEL_IMAGE_LONG_EDGE), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88, optimize=True, exif=b"")
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            values.append(f"data:image/jpeg;base64,{encoded}")
        return values

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        case_id = str(row["id"])
        media = self.db.all(
            """SELECT id,ordinal,display_name,mime_type,byte_size,sha256,width,height,created_at
            FROM life_case_media WHERE case_id=? ORDER BY ordinal""",
            (case_id,),
        )
        latest = self.db.one(
            "SELECT id,status,stage,updated_at FROM copy_tasks WHERE source_case_id=? ORDER BY created_at DESC LIMIT 1",
            (case_id,),
        )
        for item in media:
            item["media_url"] = (
                f"/api/life-cases/{case_id}/media/{item['id']}?workspace_id={row['workspace_id']}"
            )
        return {
            "id": case_id,
            "workspace_id": str(row["workspace_id"]),
            "title": str(row["title"]),
            "note_text": str(row["note_text"]),
            "occurred_at": str(row["occurred_at"]),
            "sync_ip": bool(row["sync_ip"]),
            "archived": bool(row["archived"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "media": media,
            "latest_task_id": str(latest["id"]) if latest else None,
            "latest_task_status": str(latest["status"]) if latest else None,
            "latest_task_stage": str(latest["stage"]) if latest else None,
        }

    def _case_folder(self, case_id: str) -> Path:
        folder = (self.root / case_id).resolve()
        root = self.root.resolve()
        if folder.parent != root:
            raise ValueError("生活案例存储路径无效")
        return folder

    def _trash_folder(self, case_id: str) -> Path:
        folder = (self.root / f".trash-{case_id}-{new_id('delete')}").resolve()
        if folder.parent != self.root.resolve():
            raise ValueError("生活案例回收路径无效")
        return folder

    def _cleanup_trash(self) -> None:
        root = self.root.resolve()
        candidates = list(self.root.glob(".trash-*"))
        if not candidates:
            return
        try:
            self.db.one("SELECT id FROM life_cases LIMIT 1")
        except sqlite3.OperationalError:
            # The service can be imported before a brand-new database is initialized.
            return
        for candidate in candidates:
            path = candidate.resolve()
            if path.parent != root or not path.is_dir():
                continue
            payload = path.name.removeprefix(".trash-")
            case_id, separator, _ = payload.partition("-delete_")
            if (
                not separator
                or not re.fullmatch(r"case_[A-Za-z0-9_]+", case_id)
                or not re.fullmatch(r"[A-Za-z0-9_]+", _)
            ):
                continue
            official = self._case_folder(case_id)
            case_exists = bool(self.db.one("SELECT id FROM life_cases WHERE id=?", (case_id,)))
            if case_exists and not official.exists():
                path.rename(official)
            else:
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _checked_file(folder: Path, local_name: str) -> Path:
        path = (folder / local_name).resolve()
        if path.parent != folder.resolve():
            raise ValueError("生活图片存储路径无效")
        return path


life_case_service = LifeCaseService()
