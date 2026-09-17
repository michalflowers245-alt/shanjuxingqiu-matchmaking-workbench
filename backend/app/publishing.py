from __future__ import annotations

import html
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import STATE_DIR
from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .security import vault


PUBLISH_GATE = STATE_DIR / "stage_a_accepted"


class PublishError(RuntimeError):
    pass


class WaitingForUser(PublishError):
    pass


def publishing_enabled() -> bool:
    return PUBLISH_GATE.exists()


def body_to_html(text: str) -> str:
    paragraphs = [html.escape(part).replace("\n", "<br>") for part in text.split("\n\n") if part.strip()]
    return "".join(f"<p>{part}</p>" for part in paragraphs)


class Publisher:
    def __init__(self, db: Database = database):
        self.db = db

    def approve(
        self,
        workspace_id: str,
        task_id: str,
        draft_version_id: str,
        connector_id: str,
        scheduled_at: str | None,
    ) -> dict[str, Any]:
        if not publishing_enabled():
            raise PublishError("发布中心尚未启用：请先完成阶段A真实文案验收")
        draft = self.db.one(
            """SELECT d.*,t.platform,t.content_type,t.topic FROM draft_versions d
            JOIN copy_tasks t ON t.id=d.task_id
            WHERE d.id=? AND d.task_id=? AND d.workspace_id=? AND d.is_final=1""",
            (draft_version_id, task_id, workspace_id),
        )
        if not draft:
            raise PublishError("只能发布当前品牌中经主编通过的最终版本")
        connector = self.db.one(
            "SELECT * FROM connector_profiles WHERE id=? AND workspace_id=?",
            (connector_id, workspace_id),
        )
        if not connector:
            raise PublishError("发布账号不存在或不属于当前品牌")
        assets = self.db.all(
            "SELECT * FROM assets WHERE draft_version_id=? AND status='READY'",
            (draft_version_id,),
        )
        payload = {
            "draft": json_loads(draft["package_json"], {}),
            "platform": connector["platform"],
            "connector_name": connector["name"],
            "assets": [
                {"type": row["asset_type"], "local_path": row.get("local_path"), "remote_url": row.get("remote_url")}
                for row in assets
            ],
        }
        now = utc_now()
        job_id = new_id("publish")
        status = "SCHEDULED" if scheduled_at else "APPROVED"
        self.db.execute(
            """INSERT INTO publish_jobs
            (id,workspace_id,task_id,draft_version_id,connector_id,frozen_payload_json,scheduled_at,approved_at,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, workspace_id, task_id, draft_version_id, connector_id, json_dumps(payload), scheduled_at, now, status, now, now),
        )
        self.db.emit(task_id, workspace_id, "PUBLISHING", "PUBLISH_APPROVED", "发布快照已冻结并获得一次性确认", {"publish_job_id": job_id})
        return self.db.one("SELECT * FROM publish_jobs WHERE id=?", (job_id,)) or {}

    def run_job(self, job: dict[str, Any]) -> None:
        connector = self.db.one("SELECT * FROM connector_profiles WHERE id=?", (job["connector_id"],))
        if not connector:
            raise PublishError("发布账号已不存在")
        self.db.execute("UPDATE publish_jobs SET status='PUBLISHING',updated_at=? WHERE id=?", (utc_now(), job["id"]))
        payload = json_loads(job["frozen_payload_json"], {}) or {}
        try:
            if connector["platform"] == "wechat":
                result = self._publish_wechat(connector, payload)
            elif connector["platform"] == "xiaohongshu":
                result = self._publish_xiaohongshu(connector, payload)
            else:
                raise PublishError("暂不支持该发布平台")
        except WaitingForUser as exc:
            self.db.execute(
                "UPDATE publish_jobs SET status='WAITING_USER',last_error=?,updated_at=? WHERE id=?",
                (str(exc), utc_now(), job["id"]),
            )
            self.db.emit(job["task_id"], job["workspace_id"], "PUBLISHING", "WAITING_USER", str(exc), {"publish_job_id": job["id"]})
            return
        if result.get("status") != "PUBLISHED" or not result.get("platform_post_id"):
            status = result.get("status") or "VERIFY_REQUIRED"
            self.db.execute(
                "UPDATE publish_jobs SET status=?,result_json=?,last_error=?,updated_at=? WHERE id=?",
                (status, json_dumps(result), result.get("message"), utc_now(), job["id"]),
            )
            self.db.emit(job["task_id"], job["workspace_id"], "PUBLISHING", status, result.get("message") or "平台结果仍待核实", {"publish_job_id": job["id"]})
            return
        self.db.execute(
            """UPDATE publish_jobs SET status='PUBLISHED',platform_post_id=?,platform_url=?,screenshot_path=?,
            result_json=?,last_error=NULL,updated_at=? WHERE id=?""",
            (
                result["platform_post_id"], result.get("platform_url"), result.get("screenshot_path"),
                json_dumps(result), utc_now(), job["id"],
            ),
        )
        self.db.emit(job["task_id"], job["workspace_id"], "PUBLISHING", "PUBLISHED", "平台已返回真实作品标识", {"url": result.get("platform_url")})

    def _publish_wechat(self, connector: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        config = json_loads(connector.get("config_json"), {}) or {}
        app_id = config.get("app_id")
        app_secret = vault.get(connector.get("secret_ref"))
        thumb_media_id = config.get("thumb_media_id")
        if not app_id or not app_secret:
            raise PublishError("公众号 AppID 或 AppSecret 未配置")
        if not thumb_media_id:
            raise PublishError("公众号发布需要永久封面素材 thumb_media_id")
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            token_response = client.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={"grant_type": "client_credential", "appid": app_id, "secret": app_secret},
            )
            token_data = token_response.json()
            token = token_data.get("access_token")
            if not token:
                raise PublishError(f"公众号授权失败：{token_data.get('errmsg', '未知错误')}")
            draft = payload.get("draft") or {}
            article = {
                "title": draft.get("title", "")[:64],
                "author": config.get("author", ""),
                "digest": config.get("digest") or str(draft.get("body", ""))[:120],
                "content": body_to_html(str(draft.get("body", ""))),
                "content_source_url": config.get("content_source_url", ""),
                "thumb_media_id": thumb_media_id,
                "need_open_comment": int(bool(config.get("need_open_comment", True))),
                "only_fans_can_comment": int(bool(config.get("only_fans_can_comment", False))),
            }
            draft_response = client.post(
                "https://api.weixin.qq.com/cgi-bin/draft/add",
                params={"access_token": token},
                json={"articles": [article]},
            ).json()
            media_id = draft_response.get("media_id")
            if not media_id:
                raise PublishError(f"公众号草稿创建失败：{draft_response.get('errmsg', '未知错误')}")
            submit = client.post(
                "https://api.weixin.qq.com/cgi-bin/freepublish/submit",
                params={"access_token": token},
                json={"media_id": media_id},
            ).json()
            publish_id = submit.get("publish_id")
            if not publish_id:
                raise PublishError(f"公众号发布提交失败：{submit.get('errmsg', '未知错误')}")
            status_data = client.post(
                "https://api.weixin.qq.com/cgi-bin/freepublish/get",
                params={"access_token": token},
                json={"publish_id": publish_id},
            ).json()
        publish_status = status_data.get("publish_status")
        articles = status_data.get("article_detail", {}).get("item", [])
        article_url = articles[0].get("article_url") if articles else None
        if publish_status == 0 and article_url:
            return {"status": "PUBLISHED", "platform_post_id": publish_id, "platform_url": article_url, "raw_status": publish_status}
        return {
            "status": "SUBMITTED",
            "platform_post_id": publish_id,
            "platform_url": article_url,
            "raw_status": publish_status,
            "message": "公众号已受理发布，平台仍在处理；尚未标记为发布成功",
        }

    def _publish_xiaohongshu(self, connector: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        config = json_loads(connector.get("config_json"), {}) or {}
        user_data_dir = config.get("user_data_dir")
        if not user_data_dir:
            raise WaitingForUser("请先为小红书连接器指定本地浏览器登录目录")
        media_paths = [row.get("local_path") for row in payload.get("assets", []) if row.get("local_path")]
        if not media_paths:
            raise WaitingForUser("小红书图文发布至少需要一张已确认的本地图片")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WaitingForUser("需要先安装 Playwright 及 Chromium 浏览器组件") from exc
        screenshot_dir = STATE_DIR / "publish_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"xhs_{int(time.time())}.png"
        draft = payload.get("draft") or {}
        publish_url = config.get("publish_url") or "https://creator.xiaohongshu.com/publish/publish"
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(Path(user_data_dir).resolve()), headless=False)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(publish_url, wait_until="domcontentloaded", timeout=60000)
            if "login" in page.url.lower() or page.get_by_text("手机号登录").count() > 0:
                page.screenshot(path=str(screenshot_path), full_page=True)
                context.close()
                raise WaitingForUser("小红书登录态已失效，请在打开的浏览器中完成登录后重试")
            file_input = page.locator("input[type=file]").first
            if file_input.count() == 0:
                page.screenshot(path=str(screenshot_path), full_page=True)
                context.close()
                raise WaitingForUser("平台页面结构已变化，未找到媒体上传入口")
            file_input.set_input_files(media_paths)
            title_box = page.locator('input[placeholder*="标题"], textarea[placeholder*="标题"]').first
            body_box = page.locator('[contenteditable="true"], textarea[placeholder*="正文"], textarea[placeholder*="描述"]').first
            if title_box.count() == 0 or body_box.count() == 0:
                page.screenshot(path=str(screenshot_path), full_page=True)
                context.close()
                raise WaitingForUser("平台页面结构已变化，未找到标题或正文输入框")
            title_box.fill(str(draft.get("title", ""))[:20])
            body_box.fill(str(draft.get("body", ""))[:1000])
            if page.get_by_text("验证码").count() > 0 or page.get_by_text("安全验证").count() > 0:
                page.screenshot(path=str(screenshot_path), full_page=True)
                context.close()
                raise WaitingForUser("平台要求验证码或安全验证，请人工接管后重试")
            button = page.get_by_text("发布", exact=True).last
            if button.count() == 0:
                page.screenshot(path=str(screenshot_path), full_page=True)
                context.close()
                raise WaitingForUser("平台页面结构已变化，未找到发布按钮")
            button.click()
            page.wait_for_timeout(5000)
            page.screenshot(path=str(screenshot_path), full_page=True)
            current_url = page.url
            success = page.get_by_text("发布成功").count() > 0
            context.close()
        if success:
            return {
                "status": "VERIFY_REQUIRED",
                "platform_url": current_url,
                "screenshot_path": str(screenshot_path),
                "message": "页面显示发布成功，但尚未解析到平台作品ID；请在发布记录中核实链接",
            }
        return {
            "status": "VERIFY_REQUIRED",
            "platform_url": current_url,
            "screenshot_path": str(screenshot_path),
            "message": "已执行发布操作，但平台没有返回可验证的作品标识",
        }


class PublishWorker:
    def __init__(self, publisher: Publisher, db: Database = database):
        self.publisher = publisher
        self.db = db
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="publish-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not publishing_enabled():
                self._wake.wait(2)
                self._wake.clear()
                continue
            now = utc_now()
            job = self.db.one(
                """SELECT * FROM publish_jobs WHERE status IN ('APPROVED','SCHEDULED')
                AND (scheduled_at IS NULL OR scheduled_at<=?) ORDER BY created_at LIMIT 1""",
                (now,),
            )
            if not job:
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                self.publisher.run_job(job)
            except Exception as exc:
                self.db.execute(
                    "UPDATE publish_jobs SET status='FAILED',last_error=?,updated_at=? WHERE id=?",
                    (str(exc)[:500], utc_now(), job["id"]),
                )
                self.db.emit(job["task_id"], job["workspace_id"], "PUBLISHING", "PUBLISH_FAILED", f"发布失败：{str(exc)[:180]}")


publisher = Publisher()
publish_worker = PublishWorker(publisher)

