from __future__ import annotations

import difflib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .authenticity import citation_tokens, new_unsupported_tokens, scan_expression
from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .knowledge import KnowledgeService, fetch_public_page, knowledge_service
from .life_cases import LifeCaseService
from .prompts import CONTENT_TYPES, editor_contract, product_mode_contract, research_contract, writer_contract
from .providers import ModelGateway, ModelResult, ProviderError, SearchGateway, model_gateway, search_gateway
from .quality import assess_draft
from .security import vault
from .skills import SkillPackage, SkillRegistry, skill_registry


WEIGHTS = {
    "conversion": 0.30,
    "insight": 0.20,
    "brand": 0.15,
    "evidence": 0.15,
    "originality": 0.10,
    "platform": 0.10,
}


def clamp_score(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def weighted_score(scores: dict[str, Any]) -> float:
    return round(sum(clamp_score(scores.get(key)) * weight for key, weight in WEIGHTS.items()), 1)


def profile_fields(db: Database, workspace_id: str) -> dict[str, Any]:
    row = db.one("SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (workspace_id,))
    return json_loads(row.get("profile_json") if row else None, {}) or {}


def _radar_platform_search_source(url: str, constraints: dict[str, Any]) -> dict[str, str] | None:
    """Return metadata for an official radar search link without fetching it.

    Platform search pages are deliberately carried into a task as human
    review pointers.  Fetching them again is slow and can be blocked by the
    platform, so they must remain links rather than being treated as scraped
    evidence.  Arbitrary user URLs retain the normal fetch path.
    """
    raw_links = constraints.get("radar_platform_searches")
    if not isinstance(raw_links, list):
        return None
    for raw in raw_links:
        if not isinstance(raw, dict) or str(raw.get("url") or "").strip() != url:
            continue
        platform = str(raw.get("platform") or "").strip().lower()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        host = parsed.hostname.lower() if parsed.hostname else ""
        permitted = (
            parsed.scheme == "https"
            and ((platform == "douyin" and (host == "douyin.com" or host.endswith(".douyin.com")))
                 or (platform == "xiaohongshu" and (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))))
        )
        if not permitted:
            continue
        label = str(raw.get("label") or ("抖音搜索" if platform == "douyin" else "小红书搜索")).strip()[:80]
        return {"platform": platform, "label": label}
    return None


def performance_insights(db: Database, workspace_id: str, content_type: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = [workspace_id]
    clause = ""
    if content_type:
        clause = " AND t.content_type=?"
        params.append(content_type)
    rows = db.all(
        f"""SELECT p.*,t.content_type,t.topic,d.package_json
        FROM performance_records p
        LEFT JOIN copy_tasks t ON t.id=p.task_id
        LEFT JOIN draft_versions d ON d.id=p.draft_version_id
        WHERE p.workspace_id=?{clause}
        ORDER BY p.recorded_at DESC LIMIT 50""",
        params,
    )
    scored: list[dict[str, Any]] = []
    for row in rows:
        reach = max(int(row.get("reads") or 0), int(row.get("impressions") or 0), 1)
        package = json_loads(row.get("package_json"), {}) or {}
        score = (
            int(row.get("inquiries") or 0) * 3
            + int(row.get("contacts") or 0) * 5
            + int(row.get("sales") or 0) * 10
            + int(row.get("interactions") or 0) * 0.2
        ) / reach
        scored.append({
            "topic": row.get("topic") or "",
            "content_type": row.get("content_type"),
            "title": package.get("title", ""),
            "cta": package.get("cta", ""),
            "inquiry_rate": round(int(row.get("inquiries") or 0) / reach, 4),
            "contact_rate": round(int(row.get("contacts") or 0) / reach, 4),
            "sales": int(row.get("sales") or 0),
            "score": round(score, 4),
        })
    return sorted(scored, key=lambda item: item["score"], reverse=True)[:6]


class LocalResearchEngine:
    @staticmethod
    def research(task: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
        source_ids = [row["source_key"] for row in sources[:4]]
        return {
            "brief": f"围绕“{task['topic']}”寻找具体客户处境、可信依据和清楚的下一步。",
            "customer_questions": [
                f"“{task['topic']}”与我的当前处境有什么关系？",
                "判断这件事是否适合我，需要核对哪些条件？",
                "行动之前最大的顾虑和成本是什么？",
            ],
            "objections": ["担心只讲概念", "担心承诺没有证据", "不知道下一步是否适合自己"],
            "competitor_patterns": [],
            "angles": [{"title": "具体场景 + 判断标准", "why": "先筛选目标读者，再降低决策成本", "source_ids": source_ids, "source_urls": []}],
            "recommended_angle": {"title": "从真实决策场景切入", "reason": "兼顾理解、信任和行动"},
            "evidence_gaps": [] if source_ids else ["尚未找到可公开核验的资料"],
            "conversion_hypothesis": "用具体场景筛选读者，用证据和边界建立信任，最后给一个低门槛动作。",
        }


class WorkflowEngine:
    def __init__(
        self,
        db: Database = database,
        knowledge: KnowledgeService = knowledge_service,
        models: ModelGateway = model_gateway,
        search: SearchGateway = search_gateway,
        skills: SkillRegistry = skill_registry,
        life_cases: LifeCaseService | None = None,
    ):
        self.db = db
        self.knowledge = knowledge
        self.models = models
        self.search = search
        self.skills = skills
        self.life_cases = life_cases or LifeCaseService(db=db, root=db.path.parent / "life_cases")
        self.local = LocalResearchEngine()

    def _role(self, workspace_id: str, role_key: str) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM role_configs WHERE workspace_id=? AND role_key=? AND enabled=1",
            (workspace_id, role_key),
        )
        if not row:
            raise RuntimeError(f"数字员工未启用：{role_key}")
        return row

    def _has_model(self, workspace_id: str, role_key: str) -> bool:
        checker = getattr(self.models, "profile_is_usable", None)
        if callable(checker):
            return bool(checker(workspace_id, role_key))
        profile = self.models.resolve_profile(workspace_id, role_key)
        return bool(profile and vault.get(profile.get("secret_ref")))

    def _skill(self, role_key: str) -> SkillPackage:
        return self.skills.for_role(role_key)

    def _record_skill(self, task: dict[str, Any], skill: SkillPackage, round_number: int = 0) -> None:
        self._save_artifact(
            task,
            f"skill_usage_{skill.role}",
            round_number,
            {"skill_id": skill.name, "skill_version": skill.version, "status": "applied"},
        )

    def run(self, task_id: str) -> None:
        task = self.db.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,))
        if not task:
            return
        if not self.db.one("SELECT id FROM research_packets WHERE task_id=?", (task_id,)):
            self.run_research(task)
            task = self.db.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,)) or task
            if not self.db.one("SELECT id FROM research_packets WHERE task_id=?", (task_id,)):
                return
        if task.get("review_target_id"):
            target = self.db.one(
                "SELECT * FROM draft_versions WHERE id=? AND task_id=?",
                (task["review_target_id"], task_id),
            )
            if not target:
                raise RuntimeError("待审稿版本不存在")
            self.run_editor(task, target)
            self.db.execute("UPDATE copy_tasks SET review_target_id=NULL WHERE id=?", (task_id,))
            return
        review = self.db.one(
            "SELECT * FROM editor_reviews WHERE task_id=? ORDER BY round DESC,created_at DESC LIMIT 1",
            (task_id,),
        )
        if review and review["decision"] == "PASS":
            self._finish(task_id, task["workspace_id"], review["draft_version_id"], float(review["total_score"]))
            return
        if review and int(review["round"]) >= self.max_revisions(task["workspace_id"]):
            self._needs_attention(task, "主编已退回修改一次，仍有需要本人确认的问题")
            return
        if not self._has_model(task["workspace_id"], "writer"):
            self._needs_model({**task, "stage": "WRITER"}, "研究资料已保留；请在设置中连接本机代理，连接后可从文案员工继续")
            return
        latest = self.db.one("SELECT * FROM draft_versions WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,))
        revision_writer_ready = bool(
            review
            and latest
            and review.get("decision") == "REVISE"
            and latest.get("origin") == "WRITER_REVISION"
            and int(task.get("revision_round") or 0) > int(review.get("round") or 0)
            and str(latest.get("created_at") or "") >= str(review.get("created_at") or "")
        )
        if not latest or (review and not revision_writer_ready):
            instructions = json_loads(review.get("instructions_json") if review else None, []) or []
            latest = self.run_writer(task, instructions)
            task = self.db.one("SELECT * FROM copy_tasks WHERE id=?", (task_id,)) or task
        self.run_editor(task, latest)

    def max_revisions(self, workspace_id: str) -> int:
        row = self.db.one("SELECT max_revisions FROM workflows WHERE workspace_id=? AND is_default=1", (workspace_id,))
        return max(0, min(1, int(row["max_revisions"] if row else 0)))

    def pass_score(self, workspace_id: str) -> float:
        row = self.db.one("SELECT pass_score FROM workflows WHERE workspace_id=? AND is_default=1", (workspace_id,))
        return float(row["pass_score"] if row else 80)

    def run_research(self, task: dict[str, Any]) -> dict[str, Any]:
        workspace_id, task_id = task["workspace_id"], task["id"]
        self._role(workspace_id, "researcher")
        skill = self._skill("researcher")
        constraints = json_loads(task.get("constraints_json"), {}) or {}
        if str(constraints.get("product_mode") or "") == "moments_life_case":
            return self._run_life_case_research(task, skill)
        self._stage(task, "RESEARCHER", "研究员正在搜索资料和客户问题")
        brand = profile_fields(self.db, workspace_id)
        local_rows = self.knowledge.retrieve(workspace_id, f"{task['topic']} {task['offer']} {task['goal']}", 10)
        sources: list[dict[str, Any]] = []
        if any(str(value or "").strip() for value in brand.values()):
            sources.append({
                "source_key": "B1",
                "kind": "brand_profile",
                "title": "用户填写的我的 IP 信息",
                "url": "",
                "published_at": None,
                "excerpt": json_dumps(brand)[:2500],
                "credibility": 1.0,
                "document_id": None,
                "search_mode": "user_profile",
            })
        sources.extend([
            {
                "source_key": f"K{index}",
                "kind": "knowledge",
                "title": row["title"],
                "url": row.get("source_uri") or "",
                "published_at": None,
                "excerpt": row["content"][:1600],
                "credibility": 0.9 if row.get("is_gold") else 0.8,
                "document_id": row["document_id"],
                "search_mode": "knowledge",
            }
            for index, row in enumerate(local_rows, start=1)
        ])
        for url in json_loads(task.get("source_urls_json"), []) or []:
            clean_url = str(url)
            platform_pointer = _radar_platform_search_source(clean_url, constraints)
            if platform_pointer:
                index = sum(row["source_key"].startswith("U") for row in sources) + 1
                sources.append({
                    "source_key": f"U{index}",
                    "kind": "platform_search",
                    "title": f"{platform_pointer['label']}：{constraints.get('radar_search_query') or task['topic']}",
                    "url": clean_url,
                    "published_at": None,
                    "excerpt": "这是官方平台搜索入口，供在平台内核对真实视频、笔记和评论；工作台不会抓取或把它当作平台数据。",
                    "credibility": 0.7,
                    "document_id": None,
                    "search_mode": "official_platform_search",
                })
                continue
            try:
                final_url, title, text = fetch_public_page(clean_url)
                index = sum(row["source_key"].startswith("U") for row in sources) + 1
                sources.append({
                    "source_key": f"U{index}",
                    "kind": "specified_url",
                    "title": title,
                    "url": final_url,
                    "published_at": None,
                    "excerpt": text[:1800],
                    "credibility": 0.7,
                    "document_id": None,
                    "search_mode": "specified_url",
                })
            except Exception as exc:
                self.db.emit(task_id, workspace_id, "RESEARCHER", "SOURCE_WARNING", f"指定网页读取失败：{type(exc).__name__}")

        mode_contract = product_mode_contract(
            task["content_type"], json_loads(task.get("constraints_json"), {}) or {}
        )
        search_mode = "disabled"
        if int(task.get("web_research") or 0):
            try:
                web_rows = self.search.search(workspace_id, task["topic"], 8)
            except Exception:
                web_rows = []
            search_mode = "multi_source" if web_rows else "unavailable"
            for index, row in enumerate(web_rows, start=1):
                sources.append({
                    "source_key": f"R{index}",
                    "kind": "web",
                    "title": row.get("title") or row.get("url") or "联网来源",
                    "url": row.get("url") or "",
                    "published_at": row.get("published_at"),
                    "excerpt": str(row.get("excerpt") or "")[:1600],
                    "credibility": float(row.get("credibility") or 0.55),
                    "document_id": None,
                    "search_mode": row.get("search_mode") or "public_web",
                })
            if web_rows:
                self.db.emit(task_id, workspace_id, "RESEARCHER", "SEARCH_COMPLETED", f"联网搜索完成，共找到 {len(web_rows)} 条来源")
            else:
                self.db.emit(task_id, workspace_id, "RESEARCHER", "SEARCH_DEGRADED", "联网暂时没有结果，已自动使用资料库和主题继续")

        payload = {
            "task": self._task_payload(task),
            "brand_profile": brand,
            "sources": sources,
            "historical_performance": performance_insights(self.db, workspace_id, task["content_type"]),
            "required_output": research_contract(),
        }
        deep_mode = str(constraints.get("quality_mode") or "fast") == "deep"
        if deep_mode and self._has_model(workspace_id, "researcher"):
            try:
                packet_value = self.models.generate_json(
                    workspace_id,
                    task_id,
                    "researcher",
                    skill.instructions + "\n\n" + mode_contract + "\n\n" + research_contract(),
                    payload,
                    schema=skill.output_schema,
                    web_search=False,
                ).data
            except ProviderError:
                packet_value = self.local.research(task, sources)
                self.db.emit(task_id, workspace_id, "RESEARCHER", "MODEL_FALLBACK", "研究模型暂时未响应，已使用本地研究结果继续")
        else:
            packet_value = self.local.research(task, sources)
            event_type = "FAST_RESEARCH" if self._has_model(workspace_id, "researcher") else "MODEL_NOTICE"
            message = "联网来源和资料库已在本机快速整理，正在直接生成正文" if event_type == "FAST_RESEARCH" else "研究资料已在本机整理，正文会等待模型"
            self.db.emit(task_id, workspace_id, "RESEARCHER", event_type, message)

        self._save_sources(task, sources)
        packet = self._normalize_packet(packet_value, task, sources)
        packet["search_mode"] = search_mode
        packet["searched_at"] = utc_now()
        now = utc_now()
        self.db.execute(
            """INSERT INTO research_packets(id,task_id,packet_json,created_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET packet_json=excluded.packet_json,updated_at=excluded.updated_at""",
            (new_id("research"), task_id, json_dumps(packet), now, now),
        )
        self._record_skill(task, skill)
        self.db.emit(task_id, workspace_id, "RESEARCHER", "STAGE_COMPLETED", f"研究员完成，共整理 {len(sources)} 条来源", {"search_mode": search_mode})
        return packet

    def _run_life_case_research(self, task: dict[str, Any], skill: SkillPackage) -> dict[str, Any]:
        """Use the original photos exactly once; a saved packet resumes without retransmitting them."""
        workspace_id, task_id = task["workspace_id"], task["id"]
        self._stage(task, "RESEARCHER", "研究员正在识别图片中的真实细节")
        case_id = str(task.get("source_case_id") or "")
        if not case_id:
            raise RuntimeError("生活案例任务缺少素材关联")
        try:
            life_case = self.life_cases.get(case_id, workspace_id)
        except KeyError as exc:
            raise RuntimeError("关联的生活案例已不存在") from exc
        if not self._has_model(workspace_id, "researcher"):
            self._needs_model(
                {**task, "stage": "RESEARCHER"},
                "生活案例和原图已保留；请连接本机代理后从研究员继续识别图片",
            )
            return {}

        sources: list[dict[str, Any]] = [
            {
                "source_key": "L1",
                "kind": "life_case_text",
                "title": "用户提供的生活案例文字",
                "url": "",
                "published_at": life_case["occurred_at"],
                "excerpt": life_case["note_text"],
                "credibility": 1.0,
                "document_id": None,
                "search_mode": "user_material",
            }
        ]
        sources.extend(
            {
                "source_key": f"P{index}",
                "kind": "life_case_image",
                "title": f"用户上传的生活图片 {index}",
                "url": "",
                "published_at": life_case["occurred_at"],
                "excerpt": "仅可引用研究员从该图片直接看见并写入视觉事实清单的内容。",
                "credibility": 1.0,
                "document_id": None,
                "search_mode": "user_material",
            }
            for index, _ in enumerate(life_case["media"], start=1)
        )
        mode_contract = product_mode_contract(task["content_type"], json_loads(task.get("constraints_json"), {}) or {})
        payload = {
            "task": self._task_payload(task),
            "brand_profile": profile_fields(self.db, workspace_id) if life_case["sync_ip"] else {},
            "life_case": {
                "note_text": life_case["note_text"],
                "occurred_at": life_case["occurred_at"],
                "sync_ip": life_case["sync_ip"],
                "images": [
                    {
                        "source_id": f"P{index}",
                        "ordinal": item["ordinal"],
                        "width": item["width"],
                        "height": item["height"],
                    }
                    for index, item in enumerate(life_case["media"], start=1)
                ],
            },
            "sources": sources,
            "historical_performance": [],
            "required_output": research_contract() + "\n" + mode_contract,
        }
        result = self.models.generate_json(
            workspace_id,
            task_id,
            "researcher",
            skill.instructions + "\n\n" + mode_contract + "\n\n" + research_contract(),
            payload,
            schema=skill.output_schema,
            web_search=False,
            image_inputs=self.life_cases.model_image_inputs(case_id),
        )
        self._save_sources(task, sources)
        packet = self._normalize_packet(result.data, task, sources)
        packet["search_mode"] = "life_case_vision"
        packet["searched_at"] = utc_now()
        now = utc_now()
        self.db.execute(
            """INSERT INTO research_packets(id,task_id,packet_json,created_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET packet_json=excluded.packet_json,updated_at=excluded.updated_at""",
            (new_id("research"), task_id, json_dumps(packet), now, now),
        )
        self._record_skill(task, skill)
        self.db.emit(
            task_id,
            workspace_id,
            "RESEARCHER",
            "STAGE_COMPLETED",
            f"研究员完成，共核对 {len(life_case['media'])} 张生活图片",
            {"search_mode": "life_case_vision"},
        )
        return packet

    def run_writer(self, task: dict[str, Any], revision_instructions: list[str] | None = None) -> dict[str, Any]:
        workspace_id, task_id = task["workspace_id"], task["id"]
        self._role(workspace_id, "writer")
        skill = self._skill("writer")
        is_revision = bool(revision_instructions)
        round_number = int(task.get("revision_round") or 0) + (1 if is_revision else 0)
        self.db.execute("UPDATE copy_tasks SET revision_round=?,updated_at=? WHERE id=?", (round_number, utc_now(), task_id))
        self._stage(task, "WRITER", "文案员工正在写完整初稿" if not is_revision else "文案员工正在按主编意见修改")
        packet_row = self.db.one("SELECT packet_json FROM research_packets WHERE task_id=?", (task_id,))
        packet = json_loads(packet_row.get("packet_json") if packet_row else None, {}) or {}
        sources = self.db.all("SELECT * FROM research_sources WHERE task_id=? ORDER BY source_key", (task_id,))
        previous_row = self.db.one("SELECT * FROM draft_versions WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)) if is_revision else None
        previous = json_loads(previous_row.get("package_json") if previous_row else None, {}) or {}
        mode_contract = product_mode_contract(
            task["content_type"], json_loads(task.get("constraints_json"), {}) or {}
        )
        constraints = json_loads(task.get("constraints_json"), {}) or {}
        life_mode = str(constraints.get("product_mode") or "") == "moments_life_case"
        payload = {
            "task": self._task_payload(task),
            "brand_profile": profile_fields(self.db, workspace_id) if not life_mode or bool(constraints.get("sync_ip", True)) else {},
            "research_packet": packet,
            "sources": sources,
            "previous_draft": previous,
            "revision_instructions": revision_instructions or [],
            "historical_performance": [] if life_mode else performance_insights(self.db, workspace_id, task["content_type"]),
            "required_output": writer_contract(task["content_type"], revision=is_revision) + "\n" + mode_contract,
        }
        result = self.models.generate_json(
            workspace_id,
            task_id,
            "writer",
            skill.instructions + "\n\n" + writer_contract(task["content_type"], revision=is_revision) + "\n\n" + mode_contract,
            payload,
            schema=skill.output_schema,
        ).data
        package = self._normalize_draft(result, task)
        draft_id = self._insert_draft_version(
            task,
            package,
            "WRITER_REVISION" if is_revision else "WRITER",
            previous_row["id"] if previous_row else None,
        )
        self._record_skill(task, skill, round_number)
        self._save_artifact(task, "writer_draft", round_number, {"draft_id": draft_id, "title": package["title"]}, package["body"])
        self.db.emit(task_id, workspace_id, "WRITER", "STAGE_COMPLETED", "文案员工已交稿，正在交给主编", {"draft_id": draft_id})
        return self.db.one("SELECT * FROM draft_versions WHERE id=?", (draft_id,)) or {}

    def run_editor(self, task: dict[str, Any], draft_row: dict[str, Any]) -> dict[str, Any]:
        workspace_id, task_id = task["workspace_id"], task["id"]
        self._role(workspace_id, "editor")
        skill = self._skill("editor")
        round_number = int(task.get("revision_round") or 0)
        self._stage(task, "EDITOR", "主编正在快速核对证据、逻辑、转化和表达")
        draft = json_loads(draft_row.get("package_json"), {}) or {}
        sources = self.db.all("SELECT * FROM research_sources WHERE task_id=? ORDER BY source_key", (task_id,))
        packet_row = self.db.one("SELECT packet_json FROM research_packets WHERE task_id=?", (task_id,))
        research_packet = json_loads(packet_row.get("packet_json") if packet_row else None, {}) or {}
        valid_ids = {row["source_key"] for row in sources}
        constraints = json_loads(task.get("constraints_json"), {}) or {}
        life_mode = str(constraints.get("product_mode") or "") == "moments_life_case"
        deep_mode = str(constraints.get("quality_mode") or "fast") == "deep"
        if not deep_mode:
            for claim in draft.get("claims") or []:
                if not isinstance(claim, dict) or str(claim.get("status") or "pending") != "verified":
                    continue
                evidence_ids = [str(item) for item in claim.get("evidence_ids") or []]
                if not evidence_ids or any(item not in valid_ids for item in evidence_ids):
                    claim["status"] = "pending"
                    claim["evidence_ids"] = [item for item in evidence_ids if item in valid_ids]
        brand = profile_fields(self.db, workspace_id) if not life_mode or bool(constraints.get("sync_ip", True)) else {}
        pre_check_text = self._review_text(draft, life_mode)
        pre_expression = scan_expression(pre_check_text, task["content_type"])
        pre_quality = assess_draft(
            draft,
            content_type=task["content_type"],
            platform=task["platform"],
            valid_source_ids=valid_ids,
            sources=sources,
            constraints=json_loads(task.get("constraints_json"), {}) or {},
        )
        mode_contract = product_mode_contract(
            task["content_type"], json_loads(task.get("constraints_json"), {}) or {}
        )
        payload = {
            "task": self._task_payload(task),
            "brand_profile": brand,
            "draft": draft,
            "research_packet": research_packet,
            "sources": sources,
            "expression_findings": pre_expression["findings"],
            "deterministic_checks": pre_quality,
            "required_output": editor_contract() + "\n" + mode_contract,
        }
        if deep_mode:
            value = self.models.generate_json(
                workspace_id,
                task_id,
                "editor",
                skill.instructions + "\n\n" + editor_contract() + "\n\n" + mode_contract + "\n请同时输出经过局部修改的 final_draft。",
                payload,
                schema=skill.output_schema,
            ).data
        else:
            local_score = 92 if not pre_quality["blockers"] else 70
            value = {
                "final_draft": draft,
                "scores": {key: local_score for key in WEIGHTS},
                "blocking_issues": pre_quality["blockers"],
                "revision_instructions": pre_quality["revision_instructions"],
                "change_notes": [],
                "change_summary": "主编已完成本机快速核验。",
                "conversion_structure": draft.get("conversion_structure") or {},
                "risks": pre_quality["warnings"],
                "decision": "PASS" if not pre_quality["blockers"] else "REVISE",
            }
        review = self._normalize_editor(value, draft, task)
        candidate = review.pop("final_draft")
        before_body = self._review_text(draft, life_mode)
        after_body = self._review_text(candidate, life_mode)
        safety_issues = new_unsupported_tokens(before_body, after_body, valid_ids)
        similarity = difflib.SequenceMatcher(None, before_body, after_body, autojunk=False).ratio() if before_body and after_body else 0
        length_ratio = len(after_body) / max(1, len(before_body))
        if similarity < 0.34 or length_ratio < 0.5 or length_ratio > 1.7:
            safety_issues.append("主编改动幅度过大，已拒绝整篇重写候选稿")
        post_expression = scan_expression(after_body, task["content_type"])
        quality = assess_draft(
            candidate,
            content_type=task["content_type"],
            platform=task["platform"],
            valid_source_ids=valid_ids,
            sources=sources,
            constraints=json_loads(task.get("constraints_json"), {}) or {},
        )
        blockers = list(review["blocking_issues"])
        blockers.extend(quality["blockers"])
        blockers.extend(safety_issues)
        strong_expression = [item for item in post_expression["findings"] if item["severity"] == "strong"]
        if len(strong_expression) >= 3:
            blockers.append("仍有多处强风险模板化表达，需要局部修改")
        blockers = list(dict.fromkeys(blockers))
        risks = list(dict.fromkeys([*review["risks"], *quality["warnings"]]))
        instructions = list(dict.fromkeys([*review["revision_instructions"], *quality["revision_instructions"]]))
        total = weighted_score(review["scores"])
        decision = "PASS" if review["decision"] == "PASS" and not blockers and total >= self.pass_score(workspace_id) else "REVISE"

        accepted_candidate = not safety_issues
        review_draft_id = draft_row["id"]
        if accepted_candidate:
            review_draft_id = self._insert_draft_version(task, candidate, "EDITOR_FINAL" if decision == "PASS" else "EDITOR_CANDIDATE", draft_row["id"])
        self._record_skill(task, skill, round_number)
        self._save_artifact(task, "editor_check", round_number, {
            "draft_id": review_draft_id,
            "skill_id": skill.name,
            "skill_version": skill.version,
            "before_expression_score": pre_expression["score"],
            "after_expression_score": post_expression["score"],
            "change_notes": review["change_notes"],
            "quality": quality,
            "accepted_candidate": accepted_candidate,
        })
        review_id = new_id("review")
        self.db.execute(
            """INSERT INTO editor_reviews
            (id,task_id,draft_version_id,round,scores_json,total_score,blocking_issues_json,
             instructions_json,change_summary,conversion_structure_json,risks_json,decision,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                review_id, task_id, review_draft_id, round_number, json_dumps(review["scores"]), total,
                json_dumps(blockers), json_dumps(instructions), review["change_summary"],
                json_dumps(review["conversion_structure"]), json_dumps(risks), decision, utc_now(),
            ),
        )
        if decision == "PASS":
            self.db.emit(task_id, workspace_id, "EDITOR", "APPROVED", f"主编审核通过，评分 {total}", {"draft_id": review_draft_id})
            self._finish(task_id, workspace_id, review_draft_id, total)
        elif round_number < self.max_revisions(workspace_id):
            self.db.execute(
                "UPDATE copy_tasks SET status='QUEUED',stage='WRITER',lease_until=NULL,updated_at=? WHERE id=?",
                (utc_now(), task_id),
            )
            self.db.emit(task_id, workspace_id, "EDITOR", "RETURNED", "主编已退回文案员工修改一次", {"blocking_issues": blockers})
        else:
            self._needs_attention(task, "主编修改后仍有需要本人确认的问题：" + "；".join(blockers[:3]))
        return {**review, "id": review_id, "total_score": total, "decision": decision, "blocking_issues": blockers, "risks": risks}

    @staticmethod
    def _review_text(package: dict[str, Any], life_mode: bool) -> str:
        if not life_mode:
            return str(package.get("body") or "")
        deliverables = package.get("deliverables") if isinstance(package.get("deliverables"), dict) else {}
        rows = deliverables.get("life_case_variants") if isinstance(deliverables.get("life_case_variants"), list) else []
        combined = "\n\n".join(
            str(item.get("body") or "") for item in rows if isinstance(item, dict) and str(item.get("body") or "").strip()
        )
        return combined or str(package.get("body") or "")

    def _insert_draft_version(self, task: dict[str, Any], package: dict[str, Any], origin: str, parent_id: str | None = None) -> str:
        row = self.db.one("SELECT COALESCE(MAX(version),0)+1 AS version FROM draft_versions WHERE task_id=?", (task["id"],))
        version = int(row["version"] if row else 1)
        draft_id = new_id("draft")
        self.db.execute(
            """INSERT INTO draft_versions
            (id,task_id,workspace_id,version,origin,package_json,body_text,is_final,parent_version_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (draft_id, task["id"], task["workspace_id"], version, origin, json_dumps(package), package["body"], 0, parent_id, utc_now()),
        )
        return draft_id

    def _save_artifact(
        self,
        task: dict[str, Any],
        artifact_type: str,
        round_number: int,
        payload: dict[str, Any],
        body_text: str = "",
    ) -> None:
        self.db.execute(
            """INSERT INTO task_artifacts(id,task_id,workspace_id,artifact_type,round,payload_json,body_text,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id,artifact_type,round) DO UPDATE SET
            payload_json=excluded.payload_json,body_text=excluded.body_text,created_at=excluded.created_at""",
            (new_id("artifact"), task["id"], task["workspace_id"], artifact_type, round_number, json_dumps(payload), body_text, utc_now()),
        )

    def _save_sources(self, task: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM research_sources WHERE task_id=?", (task["id"],))
        now = utc_now()
        if not sources:
            return
        self.db.executemany(
            """INSERT INTO research_sources
            (id,task_id,source_key,kind,title,url,published_at,excerpt,credibility,document_id,metadata_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    new_id("source"), task["id"], row["source_key"], row["kind"], str(row["title"])[:300],
                    str(row.get("url") or "")[:2000], row.get("published_at"), str(row.get("excerpt") or "")[:5000],
                    float(row.get("credibility") or 0.5), row.get("document_id"),
                    json_dumps({"search_mode": row.get("search_mode") or row.get("kind"), "searched_at": now}), now,
                )
                for row in sources
            ],
        )

    @staticmethod
    def _normalize_packet(value: dict[str, Any], task: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
        url_to_id = {str(row.get("url")): row["source_key"] for row in sources if row.get("url")}
        valid_ids = {row["source_key"] for row in sources}
        angles: list[dict[str, Any]] = []
        for item in value.get("angles") or []:
            if not isinstance(item, dict):
                continue
            ids = [str(item_id) for item_id in item.get("source_ids") or [] if str(item_id) in valid_ids]
            ids.extend(url_to_id[url] for url in item.get("source_urls") or [] if url in url_to_id)
            angles.append({"title": str(item.get("title") or "候选方向"), "why": str(item.get("why") or ""), "source_ids": list(dict.fromkeys(ids))})
        observation = value.get("life_case_observation") if isinstance(value.get("life_case_observation"), dict) else {}
        return {
            "brief": str(value.get("brief") or f"围绕“{task['topic']}”完成研究"),
            "customer_questions": [str(item) for item in value.get("customer_questions") or []],
            "objections": [str(item) for item in value.get("objections") or []],
            "competitor_patterns": value.get("competitor_patterns") if isinstance(value.get("competitor_patterns"), list) else [],
            "angles": angles,
            "recommended_angle": value.get("recommended_angle") if isinstance(value.get("recommended_angle"), dict) else {"title": task["topic"], "reason": "贴合任务"},
            "evidence_gaps": [str(item) for item in value.get("evidence_gaps") or []],
            "conversion_hypothesis": str(value.get("conversion_hypothesis") or ""),
            "life_case_observation": {
                "user_facts": [str(item) for item in observation.get("user_facts") or []],
                "visible_facts": [str(item) for item in observation.get("visible_facts") or []],
                "uncertain_items": [str(item) for item in observation.get("uncertain_items") or []],
                "privacy_notes": [str(item) for item in observation.get("privacy_notes") or []],
            },
        }

    @staticmethod
    def _normalize_deliverables(value: Any) -> dict[str, Any]:
        raw = value if isinstance(value, dict) else {}
        posts: list[dict[str, Any]] = []
        for item in raw.get("moments_posts") or []:
            if not isinstance(item, dict):
                continue
            try:
                day = max(1, min(99, int(item.get("day") or len(posts) + 1)))
            except (TypeError, ValueError):
                day = len(posts) + 1
            content = str(item.get("content") or "").strip()
            if content:
                posts.append({
                    "day": day,
                    "time": str(item.get("time") or "").strip(),
                    "role": str(item.get("role") or "").strip(),
                    "purpose": str(item.get("purpose") or "").strip(),
                    "content": content,
                    "follow_up": str(item.get("follow_up") or "").strip(),
                })
        posts.sort(key=lambda item: item["day"])
        xhs_raw = raw.get("xiaohongshu_publish") if isinstance(raw.get("xiaohongshu_publish"), dict) else {}
        douyin_raw = raw.get("douyin_script") if isinstance(raw.get("douyin_script"), dict) else {}
        life_variants: list[dict[str, Any]] = []
        allowed_angles = {"daily", "reflection", "soft_business"}
        angle_labels = {"daily": "真实日常", "reflection": "有感而发", "soft_business": "轻度业务启发"}
        used_angles: set[str] = set()
        recommended_seen = False
        for item in raw.get("life_case_variants") or []:
            if not isinstance(item, dict):
                continue
            angle = str(item.get("angle") or "").strip()
            body = str(item.get("body") or "").strip()
            if angle not in allowed_angles or angle in used_angles or not body:
                continue
            recommended = bool(item.get("recommended")) and not recommended_seen
            if recommended:
                recommended_seen = True
            used_angles.add(angle)
            life_variants.append({
                "angle": angle,
                "label": angle_labels[angle],
                "body": body,
                "rationale": str(item.get("rationale") or "").strip(),
                "recommended": recommended,
            })
        angle_order = {"daily": 0, "reflection": 1, "soft_business": 2}
        life_variants.sort(key=lambda item: angle_order[item["angle"]])
        return {
            "moments_posts": posts,
            "xiaohongshu_publish": {
                "title": str(xhs_raw.get("title") or "").strip(),
                "body": str(xhs_raw.get("body") or "").strip(),
                "tags": [str(item).lstrip("#").strip() for item in xhs_raw.get("tags") or [] if str(item).strip()][:10],
            },
            "douyin_script": {
                "hook": str(douyin_raw.get("hook") or "").strip(),
                "script": str(douyin_raw.get("script") or "").strip(),
                "emotion_beats": [str(item).strip() for item in douyin_raw.get("emotion_beats") or [] if str(item).strip()][:8],
                "ending": str(douyin_raw.get("ending") or "").strip(),
            },
            "life_case_variants": life_variants,
        }

    @classmethod
    def _normalize_draft(cls, value: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        variants = value.get("platform_variants")
        if isinstance(variants, list):
            variants = {
                str(item.get("platform") or task["platform"]): str(item.get("content") or "")
                for item in variants if isinstance(item, dict)
            }
        if not isinstance(variants, dict):
            variants = {task["platform"]: str(value.get("body") or "")}
        title = str(value.get("title") or task["topic"])[:300]
        body = str(value.get("body") or "").strip()
        tags = [str(item).lstrip("#").strip() for item in value.get("tags") or [] if str(item).strip()][:20]
        deliverables = cls._normalize_deliverables(value.get("deliverables"))
        constraints = json_loads(task.get("constraints_json"), {}) or {}
        mode = str(constraints.get("product_mode") or "")
        if mode == "moments_ops" and deliverables["moments_posts"]:
            sections: list[str] = []
            for post in deliverables["moments_posts"]:
                heading = f"第{post['day']}天" + (f" · {post['time']}" if post["time"] else "")
                metadata = "｜".join(item for item in (post["role"], post["purpose"]) if item)
                section = f"{heading}\n{metadata}\n\n{post['content']}" if metadata else f"{heading}\n\n{post['content']}"
                if post["follow_up"]:
                    section += f"\n\n互动承接：{post['follow_up']}"
                sections.append(section)
            body = "\n\n---\n\n".join(sections)
            variants["朋友圈运营包"] = body
        elif mode == "xiaohongshu_ip" and deliverables["xiaohongshu_publish"]["body"]:
            publish = deliverables["xiaohongshu_publish"]
            title = publish["title"] or title
            body = publish["body"]
            tags = publish["tags"] or tags
            tag_line = " ".join(f"#{item}" for item in tags)
            variants["小红书发布版"] = f"{title}\n\n{body}" + (f"\n\n{tag_line}" if tag_line else "")
        elif mode == "douyin_emotion" and deliverables["douyin_script"]["script"]:
            script = deliverables["douyin_script"]
            body = script["script"]
            if script["hook"] and script["hook"] not in body[:120]:
                body = f"{script['hook']}\n\n{body}"
            variants["抖音口播版"] = body
        elif mode == "moments_life_case" and deliverables["life_case_variants"]:
            life_variants = deliverables["life_case_variants"]
            selected = next((item for item in life_variants if item["recommended"]), life_variants[0])
            body = selected["body"]
            variants = {item["label"] or item["angle"]: item["body"] for item in life_variants}
        conversion = value.get("conversion_structure") if isinstance(value.get("conversion_structure"), dict) else {}
        return {
            "title": title,
            "alternative_titles": [str(item)[:300] for item in value.get("alternative_titles") or []][:8],
            "body": body,
            "platform_variants": variants,
            "cta": "" if mode == "moments_life_case" else str(value.get("cta") or task["goal"]).strip(),
            "tags": tags,
            "deliverables": deliverables,
            "claims": [item for item in value.get("claims") or [] if isinstance(item, dict)],
            "image_briefs": [],
            "conversion_structure": {key: str(conversion.get(key) or "") for key in ("hook", "trust", "proof", "objection", "action")},
        }

    def _normalize_editor(self, value: dict[str, Any], draft: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        final_value = value.get("final_draft") if isinstance(value.get("final_draft"), dict) else draft
        return {
            "final_draft": self._normalize_draft(final_value, task),
            "scores": {key: clamp_score((value.get("scores") or {}).get(key)) for key in WEIGHTS},
            "blocking_issues": [str(item) for item in value.get("blocking_issues") or []],
            "revision_instructions": [str(item) for item in value.get("revision_instructions") or []],
            "change_notes": [item for item in value.get("change_notes") or [] if isinstance(item, dict)],
            "change_summary": str(value.get("change_summary") or "主编完成独立审核和局部表达调整。"),
            "conversion_structure": value.get("conversion_structure") if isinstance(value.get("conversion_structure"), dict) else draft.get("conversion_structure") or {},
            "risks": [str(item) for item in value.get("risks") or []],
            "decision": "PASS" if str(value.get("decision")).upper() == "PASS" else "REVISE",
        }

    def _gold_similarity(self, workspace_id: str, body: str) -> float:
        rows = self.knowledge.retrieve(workspace_id, body[:300], 10, gold_only=True)
        best = 0.0
        for row in rows:
            best = max(best, difflib.SequenceMatcher(None, body[:8000], row["content"][:8000], autojunk=False).ratio())
        return best

    def _stage(self, task: dict[str, Any], stage: str, message: str) -> None:
        self.db.execute(
            "UPDATE copy_tasks SET status='RUNNING',stage=?,last_error=NULL,updated_at=? WHERE id=?",
            (stage, utc_now(), task["id"]),
        )
        self.db.emit(task["id"], task["workspace_id"], stage, "STAGE_STARTED", message)

    def _finish(self, task_id: str, workspace_id: str, draft_id: str, score: float) -> None:
        self.db.execute("UPDATE draft_versions SET is_final=CASE WHEN id=? THEN 1 ELSE 0 END WHERE task_id=?", (draft_id, task_id))
        self.db.execute(
            "UPDATE copy_tasks SET status='FINAL_READY',stage='FINAL',lease_until=NULL,last_error=NULL,updated_at=? WHERE id=?",
            (utc_now(), task_id),
        )
        self.db.emit(task_id, workspace_id, "FINAL", "FINAL_READY", f"定稿已完成，主编评分 {score}", {"draft_id": draft_id})

    def _needs_attention(self, task: dict[str, Any], message: str) -> None:
        self.db.execute(
            "UPDATE copy_tasks SET status='NEEDS_ATTENTION',stage=?,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
            (task.get("stage") or "EDITOR", message[:500], utc_now(), task["id"]),
        )
        self.db.emit(task["id"], task["workspace_id"], task.get("stage") or "EDITOR", "NEEDS_ATTENTION", message[:300])

    def _needs_model(self, task: dict[str, Any], message: str) -> None:
        self.db.execute(
            "UPDATE copy_tasks SET status='NEEDS_MODEL',stage=?,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
            (task.get("stage") or "WRITER", message[:500], utc_now(), task["id"]),
        )
        self.db.emit(task["id"], task["workspace_id"], task.get("stage") or "WRITER", "NEEDS_MODEL", message[:300])

    @staticmethod
    def _task_payload(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "content_type": task["content_type"],
            "content_type_label": CONTENT_TYPES[task["content_type"]]["label"],
            "platform": task["platform"],
            "topic": task["topic"],
            "goal": task["goal"],
            "offer": task["offer"],
            "constraints": json_loads(task.get("constraints_json"), {}) or {},
        }


class TaskWorker:
    def __init__(self, engine: WorkflowEngine, db: Database = database, poll_seconds: float = 0.7):
        self.engine = engine
        self.db = db
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="copy-task-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def _claim(self) -> dict[str, Any] | None:
        now = utc_now()
        lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM copy_tasks WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE copy_tasks SET status='RUNNING',lease_until=?,updated_at=? WHERE id=? AND status='QUEUED'",
                (lease, now, row["id"]),
            )
            return dict(row)

    def _loop(self) -> None:
        while not self._stop.is_set():
            task = self._claim()
            if not task:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()
                continue
            try:
                self.engine.run(task["id"])
            except ProviderError as exc:
                current = self.db.one("SELECT * FROM copy_tasks WHERE id=?", (task["id"],)) or task
                if exc.category == "model":
                    self.engine._needs_model(current, str(exc))
                else:
                    self.engine._needs_attention(current, str(exc))
            except Exception as exc:
                current = self.db.one("SELECT * FROM copy_tasks WHERE id=?", (task["id"],)) or task
                self.engine._needs_attention(current, f"系统处理异常：{type(exc).__name__}")


workflow_engine = WorkflowEngine()
task_worker = TaskWorker(workflow_engine)
