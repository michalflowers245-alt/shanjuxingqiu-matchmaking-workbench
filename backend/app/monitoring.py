from __future__ import annotations

import hashlib
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import Database, database, json_dumps, json_loads, new_id, utc_now
from .platform_browser import PlatformBrowserCollector, PlatformBrowserError, platform_browser_collector
from .providers import SearchGateway, sanitize_provider_text


# ``web`` is a first-party, public-web discovery source.  It is deliberately
# distinct from Xiaohongshu and Douyin so the UI never presents public-search
# signals as if they were platform-native metrics.
SUPPORTED_PLATFORMS = {"xiaohongshu", "douyin", "web"}

# Only broad content-domain terms are extracted from a saved IP profile.  This
# avoids turning personal details, contact information, or arbitrary profile
# prose into external search queries.  The list intentionally covers the
# common creator directions supported by the workbench and can fall back to a
# neutral public-trend query when no match is available.
IP_DOMAIN_TERMS = (
    "人工智能", "AI", "企业服务", "内容创作", "短视频", "小红书", "抖音",
    "相亲", "婚恋", "脱单", "恋爱", "两性", "情感", "关系", "婚姻", "同城交友", "社交", "约会", "单身",
    "女性成长", "个人成长", "年轻人", "大学生", "教育", "教培", "留学", "求职", "职场", "创业", "副业",
    "心理", "健康", "母婴", "亲子", "美妆", "服装", "摄影", "旅游", "宠物", "家居", "餐饮", "本地生活",
    "电商", "品牌", "营销", "理财", "房产", "健身", "营养", "阅读", "法律", "医疗",
)
FALLBACK_IP_TERMS = ("年轻人生活", "内容创作", "个人成长")
_PII_OR_CONTACT_RE = re.compile(
    r"(?:\d{6,}|@|微信|微\s*信|手机号|手机号码|电话|住址|地址|身份证|QQ|邮箱|mail)", re.IGNORECASE
)

# These are broad, non-sensitive routing markers.  When a profile matches
# this creator category, the daily radar uses the two execution plans that
# ship with this workbench: a local/offline matchmaking account and a
# first-person female dating-and-matchmaker account.  It never exposes the
# profile prose that caused the routing decision.
MATCHMAKING_PROFILE_MARKERS = frozenset({
    "相亲", "婚恋", "脱单", "红娘", "同城交友", "线下交友", "单身", "恋爱", "择偶", "约会",
})
SAFE_MATCHMAKING_MARKETS = ("长沙",)


class RadarError(RuntimeError):
    """A safe, user-facing error from a platform discovery run."""


class RadarBusyError(RadarError):
    pass


def _next_run_at(interval_hours: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(hours=interval_hours)
    return value.isoformat(timespec="seconds")


def _stable_key(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


def _clean_platforms(values: list[str]) -> list[str]:
    platforms = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if value in SUPPORTED_PLATFORMS and value not in platforms:
            platforms.append(value)
    if not platforms:
        raise ValueError("请至少选择一个平台")
    return platforms


def douyin_web_search_link(query: str) -> str:
    """A platform-native HTTPS search page, safe to save as a task source."""
    clean_query = _text(query, 180)
    return f"https://www.douyin.com/search/{urllib.parse.quote(clean_query, safe='')}?type=video"


def xiaohongshu_web_search_link(query: str) -> str:
    """A platform-native HTTPS search page, without scraping any note data."""
    clean_query = _text(query, 180)
    return "https://www.xiaohongshu.com/search_result/?" + urllib.parse.urlencode({"keyword": clean_query})


def _official_platform_search_links(query: str) -> list[dict[str, str]]:
    """Return the two creator-facing search actions for a safely scoped query."""
    return [
        {
            "platform": "douyin",
            "label": "抖音搜索",
            "url": douyin_web_search_link(query),
            "source_mode": "official_search",
        },
        {
            "platform": "xiaohongshu",
            "label": "小红书搜索",
            "url": xiaohongshu_web_search_link(query),
            "source_mode": "official_search",
        },
    ]


def _text(value: Any, limit: int = 500) -> str:
    """Normalize display/search text without preserving markup or control chars."""
    clean = re.sub(r"<[^>]+>", " ", str(value or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _query_terms(query: str) -> list[str]:
    """Return meaningful words used to reject accidental string matches."""
    return [term.lower() for term in re.split(r"[\s,，、/|]+", _text(query, 180)) if len(term.strip()) >= 2]


def _relevance_score(item: dict[str, Any], query: str) -> int:
    terms = _query_terms(query)
    if not terms:
        return 0
    haystack = f"{_text(item.get('title'), 500)} {_text(item.get('excerpt'), 2000)}".lower()
    hits = sum(1 for term in terms if term in haystack)
    # Daily searches deliberately contain several concrete terms. Requiring
    # two prevents accidental matches such as “相亲相爱” from winning merely
    # because an unrelated video has a large like count.
    if len(terms) >= 2 and hits < 2:
        return -1
    return hits


def _heat_score(item: dict[str, Any]) -> float:
    """Rank by metrics the platform actually made visible on the card."""
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}

    def number(key: str) -> float:
        try:
            return max(0.0, float(metrics.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    return (
        number("plays") * 0.02
        + number("likes")
        + number("comments") * 3
        + number("favorites") * 2
        + number("shares") * 4
        + number("visible_engagement")
    )


def _profile_text(value: Any) -> str:
    """Collect profile prose locally only; its raw value is never returned or saved in radar results."""
    if isinstance(value, dict):
        return " ".join(_profile_text(item) for key, item in value.items() if not str(key).startswith("_"))
    if isinstance(value, (list, tuple, set)):
        return " ".join(_profile_text(item) for item in value)
    clean = _text(value, 4000)
    # A profile editor may keep a public bio and a contact method in the same
    # long field.  Discard only contact-bearing clauses so the rest of the IP
    # direction can still be used locally for safe, broad routing.
    clauses = re.split(r"[\r\n。；;！？!?，,]+", clean)
    return " ".join(clause for clause in clauses if clause and not _PII_OR_CONTACT_RE.search(clause))


def _ip_topic_anchors(profile: dict[str, Any]) -> tuple[list[str], bool]:
    """Return privacy-filtered, broad domain anchors for public trend search.

    The profile remains a local source of routing context.  We only emit a
    predefined domain label if it appears in profile text, never a free-form
    sentence, a name, contact detail, or an address.
    """
    profile_text = _profile_text(profile)
    has_context = bool(profile_text)
    lowered = profile_text.lower()
    anchors: list[str] = []
    for term in IP_DOMAIN_TERMS:
        if term.lower() in lowered and term not in anchors:
            anchors.append(term)
        if len(anchors) >= 3:
            break
    # Keep the fallback useful on a newly-created workspace while making it
    # clear in the run summary that it did not originate from a complete IP.
    return (anchors or list(FALLBACK_IP_TERMS), bool(anchors and has_context))


def _daily_ip_directions(profile_text: str, anchors: list[str]) -> list[dict[str, str]]:
    """Build creator-ready daily directions without leaking profile prose.

    Public-web results can help us assess whether there is fresh discussion,
    but they must not replace the platform links a creator needs to inspect.
    Matchmaking profiles therefore receive the two attached account plans as
    concrete content lanes, while other profiles receive safe domain lanes.
    """
    lowered = profile_text.lower()
    is_matchmaking = any(marker.lower() in lowered for marker in MATCHMAKING_PROFILE_MARKERS)
    if is_matchmaking:
        market = next((name for name in SAFE_MATCHMAKING_MARKETS if name in profile_text), "同城")
        return [
            {
                "persona": "柚子皮 · 同城线下相亲",
                "pillar": "线下交友活动 / 扩大异性圈",
                "search_query": f"{market} 相亲 线下交友活动",
                "title": f"{market}单身的人，为什么又开始走进线下交友活动？",
                "reason": "从本地单身人群愿不愿意出门认识新人切入，再落到活动、匹配与安全感这些真实顾虑。",
            },
            {
                "persona": "小豆子 · 女性脱单红娘",
                "pillar": "圈子小 / 主动认识异性",
                "search_query": "圈子小 女生 脱单",
                "title": "圈子小的女生，到底怎么开始脱单？",
                "reason": "从“想恋爱但身边没有合适的人”的具体困境说起，给出能开始执行的认识方式。",
            },
            {
                "persona": "红娘案例 · 双账号共用",
                "pillar": "相亲上岸 / 匹配观察",
                "search_query": "红娘 相亲 上岸 案例",
                "title": "红娘看过的相亲上岸，真正卡在了哪个细节？",
                "reason": "用一个可匿名的匹配场景讲清择偶、沟通与见面节奏，避免泛泛讲情感道理。",
            },
        ]

    directions: list[dict[str, str]] = []
    for anchor in anchors[:3]:
        directions.append({
            "persona": "已保存的 IP 方向",
            "pillar": anchor,
            "search_query": f"{anchor} 内容",
            "title": f"「{anchor}」里，大家最近在认真讨论什么？",
            "reason": "先去两个平台看真实内容和评论，再把讨论落回你的目标受众正在面对的具体选择。",
        })
    return directions


def _valid_public_url(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class TopicRadarService:
    def __init__(
        self,
        db: Database = database,
        public_search: SearchGateway | None = None,
        browser_collector: PlatformBrowserCollector | None = None,
        on_results_ready: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.public_search = public_search or SearchGateway(db)
        self.browser = browser_collector or platform_browser_collector
        self.on_results_ready = on_results_ready
        self._running: set[str] = set()
        self._lock = threading.RLock()

    def get_ip_daily_monitor(self, workspace_id: str) -> dict[str, Any] | None:
        return self.db.one(
            """SELECT * FROM topic_monitors
            WHERE workspace_id=? AND monitor_kind='ip_daily'
            ORDER BY updated_at DESC LIMIT 1""",
            (workspace_id,),
        )

    def ensure_ip_daily_monitor(self, workspace_id: str, interval_hours: int = 24) -> dict[str, Any]:
        """Create or reactivate the single daily, profile-routed monitor.

        This stores no copy of profile JSON.  It schedules a first run now;
        the caller can invoke ``run_monitor`` synchronously for an immediate
        result and the local worker handles subsequent daily checks.
        """
        interval = min(max(int(interval_hours), 1), 168)
        now = utc_now()
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM topic_monitors WHERE workspace_id=? AND monitor_kind='ip_daily' LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if existing:
                monitor_id = str(existing["id"])
                connection.execute(
                    """UPDATE topic_monitors
                    SET enabled=1,interval_hours=?,next_run_at=?,updated_at=? WHERE id=?""",
                    (interval, now, now, monitor_id),
                )
            else:
                monitor_id = new_id("monitor")
                connection.execute(
                    """INSERT INTO topic_monitors
                    (id,workspace_id,monitor_kind,query,platforms_json,interval_hours,enabled,next_run_at,last_status,created_at,updated_at)
                    VALUES(?,?, 'ip_daily', 'IP 自动热点', ?,?,1,?,'IDLE',?,?)""",
                    (monitor_id, workspace_id, json_dumps(["web"]), interval, now, now, now),
                )
        return self.get_monitor(monitor_id, workspace_id)

    def get_monitor(self, monitor_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM topic_monitors WHERE id=?"
        params: tuple[Any, ...] = (monitor_id,)
        if workspace_id:
            sql += " AND workspace_id=?"
            params = (monitor_id, workspace_id)
        row = self.db.one(sql, params)
        if not row:
            raise KeyError("监测主题不存在")
        return row

    def list_monitors(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.db.all(
            """SELECT m.*,
            (SELECT COUNT(*) FROM topic_monitor_items i
             WHERE i.run_id=(SELECT r.id FROM topic_monitor_runs r WHERE r.monitor_id=m.id ORDER BY r.started_at DESC LIMIT 1)
            ) AS latest_item_count
            FROM topic_monitors m WHERE m.workspace_id=? AND m.monitor_kind='ip_daily' ORDER BY m.updated_at DESC""",
            (workspace_id,),
        )

    def update_monitor(self, monitor_id: str, workspace_id: str, *, enabled: bool | None = None, interval_hours: int | None = None) -> dict[str, Any]:
        monitor = self.get_monitor(monitor_id, workspace_id)
        fields: dict[str, Any] = {"updated_at": utc_now()}
        if enabled is not None:
            fields["enabled"] = int(enabled)
            if enabled and not monitor.get("next_run_at"):
                fields["next_run_at"] = utc_now()
        if interval_hours is not None:
            fields["interval_hours"] = min(max(int(interval_hours), 1), 168)
            if bool(monitor.get("enabled")):
                fields["next_run_at"] = _next_run_at(fields["interval_hours"])
        clause = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE topic_monitors SET {clause} WHERE id=?", (*fields.values(), monitor_id))
        return self.get_monitor(monitor_id, workspace_id)

    def list_runs(self, monitor_id: str, workspace_id: str, limit: int = 12) -> list[dict[str, Any]]:
        self.get_monitor(monitor_id, workspace_id)
        rows = self.db.all(
            "SELECT * FROM topic_monitor_runs WHERE monitor_id=? AND workspace_id=? ORDER BY started_at DESC LIMIT ?",
            (monitor_id, workspace_id, min(max(limit, 1), 50)),
        )
        for row in rows:
            row["items"] = self.db.all(
                "SELECT * FROM topic_monitor_items WHERE run_id=? ORDER BY platform, discovered_at DESC",
                (row["id"],),
            )
        return rows

    def recover_interrupted_runs(self) -> int:
        """Close RUNNING rows left by a stopped or upgraded server process."""
        rows = self.db.all("SELECT id,monitor_id FROM topic_monitor_runs WHERE status='RUNNING'")
        if not rows:
            return 0
        now = utc_now()
        message = "上次刷新因工作台重启而中断，请重新刷新"
        with self.db.transaction() as connection:
            for row in rows:
                connection.execute(
                    "UPDATE topic_monitor_runs SET status='FAILED',error_message=?,completed_at=? WHERE id=?",
                    (message, now, row["id"]),
                )
                connection.execute(
                    """UPDATE topic_monitors
                    SET last_status='FAILED',last_error=?,updated_at=?
                    WHERE id=? AND last_status='RUNNING'""",
                    (message, now, row["monitor_id"]),
                )
        return len(rows)

    def start_monitor(self, monitor_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Start a refresh in the background and return its RUNNING row quickly."""
        monitor = self.get_monitor(monitor_id, workspace_id)
        with self._lock:
            if monitor_id in self._running:
                raise RadarBusyError("热点正在更新，完成后会自动显示")
            self._running.add(monitor_id)
        started = threading.Event()

        def run() -> None:
            try:
                self.run_monitor(
                    monitor_id,
                    str(monitor["workspace_id"]),
                    _already_claimed=True,
                    _started_event=started,
                )
            finally:
                # Also release a claim if a database error happens before
                # run_monitor reaches its own cleanup block.
                with self._lock:
                    self._running.discard(monitor_id)
                started.set()

        threading.Thread(target=run, daemon=True, name=f"topic-radar-{monitor_id[-8:]}").start()
        started.wait(timeout=1.0)
        runs = self.list_runs(monitor_id, str(monitor["workspace_id"]), limit=1)
        if not runs:
            raise RadarError("热点刷新未能启动，请稍后重试")
        return runs[0]

    def discover_topic(self, workspace_id: str, topic: str, limit: int = 8) -> dict[str, Any]:
        """Search the visible platform pages first, then use public indexes as fallback."""
        clean_topic = _text(topic, 180)
        if not clean_topic:
            raise ValueError("请填写要搜索的主题")
        if len(clean_topic) < 2:
            raise ValueError("主题至少需要 2 个字")
        wanted = min(max(int(limit), 1), 12)
        rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        warnings: list[str] = []
        for platform in ("xiaohongshu", "douyin"):
            platform_rows, summary, error = self._platform_sources(workspace_id, platform, clean_topic, wanted)
            rows.extend(platform_rows)
            summaries.append(summary)
            if error:
                warnings.append(error)
        platform_links = _official_platform_search_links(clean_topic)
        angles = self._topic_angles(clean_topic, rows, platform_links)
        visible_count = sum(1 for row in rows if row.get("source_mode") == "visible_browser")
        result = {
            "topic": clean_topic,
            "sources": rows,
            "angles": angles,
            "platform_search_links": platform_links,
            "platforms": summaries,
            "search_mode": "visible_browser" if visible_count else "public_index_fallback",
            "source_count": len(rows),
            "visible_source_count": visible_count,
            "message": (
                f"已读取 {visible_count} 条平台搜索页实际显示的内容，并生成 {len(angles)} 个可讲主题。"
                if visible_count else "平台采集浏览器尚未返回可见内容；本次使用公开网页索引和平台搜索入口兜底。"
            ),
            "warning": "；".join(dict.fromkeys(warnings)) or None,
        }
        if self.on_results_ready:
            try:
                self.on_results_ready(workspace_id, result)
            except Exception:
                # Automatic copy extraction must never hide usable radar results.
                pass
        return result

    def _platform_sources(
        self, workspace_id: str, platform: str, query: str, limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        try:
            # Read a wider candidate pool first, then keep the most relevant
            # and hottest cards instead of trusting the page's first rows.
            raw_rows = self.browser.search(platform, query, min(max(limit * 3, limit), 24))
            rows: list[dict[str, Any]] = []
            for row in raw_rows:
                relevance = _relevance_score(row, query)
                if relevance < 0:
                    continue
                raw = dict(row.get("raw") or {})
                raw.update({"search_query": query, "relevance_score": relevance})
                enriched = {
                    **row,
                    "platform": platform,
                    "source_mode": "visible_browser",
                    "credibility": 0.8,
                    "search_mode": "visible_browser",
                    "matched_query": query,
                    "raw": raw,
                    "metadata": {
                        "platform": platform,
                        "search_mode": "visible_browser",
                        "metrics": row.get("metrics") or {},
                        "author": row.get("author") or "",
                    },
                }
                enriched["raw"]["heat_score"] = round(_heat_score(enriched), 2)
                rows.append(enriched)
            rows.sort(
                key=lambda row: (
                    _heat_score(row),
                    int((row.get("raw") or {}).get("relevance_score") or 0),
                    -int((row.get("raw") or {}).get("rank") or 999),
                ),
                reverse=True,
            )
            rows = rows[:limit]
            return rows, {
                "platform": platform, "status": "success" if rows else "empty", "count": len(rows),
                "source_mode": "visible_browser",
                "message": (
                    f"已按相关性和页面可见互动数筛出 {len(rows)} 条热门内容。"
                    if rows else "搜索页有内容，但没有通过主题相关性筛选的结果。"
                ),
            }, None
        except PlatformBrowserError as browser_error:
            browser_message = str(browser_error)
            browser_code = browser_error.code

        # When the visible browser is open, a login wall or security check is
        # actionable and should return immediately.  Waiting on slower public
        # indexes here made an interactive search feel broken and could not
        # replace the missing platform page anyway.
        browser_is_running = False
        try:
            status_reader = getattr(self.browser, "status", None)
            browser_is_running = bool(status_reader and status_reader().get("running"))
        except Exception:
            browser_is_running = False
        if browser_is_running or browser_code in {"login_required", "verification_required", "no_visible_results", "no_valid_results"}:
            cached = self._cached_platform_sources(workspace_id, platform, query, limit)
            if cached:
                return cached, {
                    "platform": platform, "status": "success", "count": len(cached),
                    "source_mode": "cached_visible_browser",
                    "message": f"实时页面需要登录或验证；已显示上次成功保存的 {len(cached)} 条平台内容。",
                    "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
                }, browser_message
            return [], {
                "platform": platform, "status": "login_required", "count": 0,
                "source_mode": "visible_browser", "message": browser_message,
                "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
            }, browser_message

        indexed, index_error = self._indexed_platform_sources(workspace_id, platform, query, limit)
        if indexed:
            return indexed, {
                "platform": platform, "status": "success", "count": len(indexed),
                "source_mode": "public_index_fallback",
                "message": f"平台浏览器暂未可用；已从公开网页索引找到 {len(indexed)} 条平台原文链接。",
            }, browser_message
        return [], {
            "platform": platform, "status": "login_required", "count": 0,
            "source_mode": "visible_browser", "message": browser_message,
            "search_url": xiaohongshu_web_search_link(query) if platform == "xiaohongshu" else douyin_web_search_link(query),
        }, browser_message or index_error

    def _cached_platform_sources(
        self, workspace_id: str, platform: str, query: str, limit: int,
    ) -> list[dict[str, Any]]:
        """Reuse prior visible cards when the platform temporarily requires verification."""
        rows = self.db.all(
            """SELECT source_key,title,url,excerpt,author,published_at,metrics_json,raw_json,discovered_at
            FROM topic_monitor_items
            WHERE workspace_id=? AND platform=? AND source_mode='visible_browser' AND url!=''
            ORDER BY discovered_at DESC LIMIT 120""",
            (workspace_id, platform),
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            source_key = str(row.get("source_key") or "")
            if not source_key or source_key in seen:
                continue
            candidate = {
                "source_key": source_key,
                "title": _text(row.get("title"), 500),
                "url": str(row.get("url") or ""),
                "excerpt": _text(row.get("excerpt"), 2000),
                "author": _text(row.get("author"), 300),
                "published_at": row.get("published_at"),
                "metrics": json_loads(row.get("metrics_json"), {}) or {},
                "raw": json_loads(row.get("raw_json"), {}) or {},
            }
            relevance = _relevance_score(candidate, query)
            if relevance < 0:
                continue
            seen.add(source_key)
            raw = dict(candidate["raw"])
            raw.update({
                "search_query": query,
                "relevance_score": relevance,
                "cached_from": row.get("discovered_at"),
                "heat_score": round(_heat_score(candidate), 2),
            })
            result.append({
                **candidate,
                "platform": platform,
                "source_mode": "cached_visible_browser",
                "search_mode": "cached_visible_browser",
                "matched_query": query,
                "credibility": 0.72,
                "raw": raw,
                "metadata": {
                    "platform": platform,
                    "search_mode": "cached_visible_browser",
                    "metrics": candidate["metrics"],
                    "author": candidate["author"],
                },
            })
        result.sort(
            key=lambda item: (_heat_score(item), int((item.get("raw") or {}).get("relevance_score") or 0)),
            reverse=True,
        )
        return result[:limit]

    def _indexed_platform_sources(self, workspace_id: str, platform: str, query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        domain = "xiaohongshu.com/explore" if platform == "xiaohongshu" else "douyin.com/video"
        rows, error = self._public_sources(workspace_id, f"site:{domain} {query}", limit * 2)
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                host = (urllib.parse.urlsplit(str(row.get("url") or "")).hostname or "").lower()
            except ValueError:
                continue
            expected = "xiaohongshu.com" if platform == "xiaohongshu" else "douyin.com"
            if host != expected and not host.endswith(f".{expected}"):
                continue
            result.append({
                **row,
                "platform": platform,
                "source_mode": "public_index_fallback",
                "author": "",
                "metrics": {},
                "raw": {"search_mode": "public_index_fallback"},
                "metadata": {"platform": platform, "search_mode": "public_index_fallback"},
            })
            if len(result) >= limit:
                break
        return result, error

    def _public_sources(self, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        try:
            raw_rows = self.public_search.search(workspace_id, query, limit)
        except Exception as exc:
            return [], sanitize_provider_text(str(exc), 180) or "公开搜索暂时不可用"
        if not isinstance(raw_rows, list):
            return [], None
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            title = _text(raw.get("title"), 500)
            url = _valid_public_url(raw.get("url"))
            if not title or not url:
                continue
            source_key = _stable_key(url or title)
            if source_key in seen:
                continue
            seen.add(source_key)
            try:
                credibility = max(0.0, min(1.0, float(raw.get("credibility") or 0.5)))
            except (TypeError, ValueError):
                credibility = 0.5
            rows.append({
                "source_key": source_key,
                "title": title,
                "url": url,
                "excerpt": _text(raw.get("excerpt"), 2000),
                "published_at": str(raw.get("published_at") or "")[:100] or None,
                "credibility": credibility,
                "search_mode": _text(raw.get("search_mode") or "public_web", 80),
            })
            if len(rows) >= limit:
                break
        return rows, None

    @staticmethod
    def _topic_angles(
        topic: str,
        sources: list[dict[str, Any]],
        platform_links: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        platform_links = platform_links or _official_platform_search_links(topic)
        platform_urls = [str(item["url"]) for item in platform_links]
        platform_titles = [str(item["label"]) for item in platform_links]
        angles: list[dict[str, Any]] = []
        source_rows = [row for row in sources if row.get("platform") in {"xiaohongshu", "douyin"}]
        for source in source_rows[:8]:
            title = _text(source.get("title"), 100)
            if not title:
                continue
            angles.append({
                "title": title,
                "topic": topic,
                "reason": f"参考{('小红书' if source.get('platform') == 'xiaohongshu' else '抖音')}原内容《{title[:42]}》，换成你的目标受众能代入的现实场景。",
                "search_query": topic,
                "platform_search_links": platform_links,
                # The creation flow accepts HTTPS source URLs.  Only the two
                # platform-native search pages are carried forward; generic
                # web results remain background context and are never shown as
                # if they were platform content.
                "source_urls": [str(source.get("url") or "")] if source.get("url") else platform_urls,
                "source_titles": [title] if source.get("url") else platform_titles,
                "evidence_url": source.get("url"),
                "evidence_platform": source.get("platform"),
                "visible_metrics": source.get("metrics") or {},
            })
        fallback_angles = [
            (f"为什么大家开始重新讨论「{topic}」？", "从平台高频表达里找共同焦虑，再给出你的判断。"),
            (f"关于「{topic}」，最容易被忽略的 3 个细节", "把抽象话题变成三个可观察、可验证的真实细节。"),
            (f"同样面对「{topic}」，为什么结果完全不同？", "用两个常见选择对照，讲清成本、边界和判断标准。"),
            (f"第一次遇到「{topic}」，先别急着做决定", "服务第一次接触这个问题的人，给一份低风险行动清单。"),
            (f"看了平台上关于「{topic}」的讨论，我更想提醒你这一点", "从真实内容的共同表达中提炼一个清楚判断，形成有个人态度的口播。"),
            (f"「{topic}」里，大家嘴上说的和真正介意的为什么不同？", "抓住公开表达与真实顾虑之间的错位，形成评论区讨论点。"),
            (f"做过「{topic}」之后，我发现结果往往在开始前就决定了", "从准备、筛选和预期管理切入，给出有经验感的判断。"),
            (f"别只看结果：判断「{topic}」值不值得的 4 个信号", "提供可以保存和复用的判断标准，增强内容实用性。"),
            (f"「{topic}」最常见的误区，正在劝退真正需要的人", "回应平台内容中的典型误解，并说明怎样降低尝试成本。"),
            (f"如果重新经历一次「{topic}」，我会提前做好这件事", "用复盘视角组织内容，让建议有具体时点和动作。"),
            (f"为什么关于「{topic}」的建议很多，真正能做的却很少？", "筛掉空泛道理，只留下观众今天能验证的一步。"),
            (f"「{topic}」之后最值得复盘的，不是成没成功", "把注意力从单次结果转向能力、边界与长期选择。"),
        ]
        for title, reason in fallback_angles:
            if len(angles) >= 12:
                break
            angles.append({
                "title": title, "topic": topic, "reason": reason, "search_query": topic,
                "platform_search_links": platform_links, "source_urls": platform_urls,
                "source_titles": platform_titles,
            })
        return angles[:12]

    def _run_ip_daily(self, monitor: dict[str, Any]) -> dict[str, Any]:
        profile_row = self.db.one(
            "SELECT profile_json FROM brand_profiles WHERE workspace_id=?", (monitor["workspace_id"],)
        ) or {}
        profile = json_loads(profile_row.get("profile_json"), {}) or {}
        safe_profile = profile if isinstance(profile, dict) else {}
        anchors, profile_based = _ip_topic_anchors(safe_profile)
        directions = _daily_ip_directions(_profile_text(safe_profile), anchors)
        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        platform_summaries: list[dict[str, Any]] = []
        # One focused search per platform keeps the interactive refresh fast.
        # The two platforms use different profile directions, while all three
        # directions still contribute recommendation variants below.
        for platform in ("xiaohongshu", "douyin"):
            platform_count = 0
            modes: set[str] = set()
            platform_errors: list[str] = []
            direction_index = 0 if platform == "xiaohongshu" else min(1, len(directions) - 1)
            for direction in directions[direction_index:direction_index + 1]:
                rows, summary, error = self._platform_sources(
                    monitor["workspace_id"], platform, str(direction["search_query"]), 6,
                )
                collected.extend(rows)
                platform_count += len(rows)
                modes.add(str(summary.get("source_mode") or ""))
                if error:
                    platform_errors.append(error)
            # One focused query may legitimately have no cards.  Keep the
            # platform successful when its other query returned usable posts.
            if platform_errors and not platform_count:
                errors.extend(platform_errors)
            visible = "visible_browser" in modes
            cached = "cached_visible_browser" in modes
            platform_summaries.append({
                "platform": platform,
                "status": "success" if platform_count else "login_required",
                "count": platform_count,
                "source_mode": "visible_browser" if visible else "cached_visible_browser" if cached else "public_index_fallback",
                "message": (
                    f"已按相关性和可见互动数筛出 {platform_count} 条热门内容。"
                    if visible and platform_count else f"本轮找到 {platform_count} 条平台原文索引；打开采集浏览器登录后可读取更多。"
                    if platform_count and not cached else f"实时页面需要登录或验证；已显示上次保存的 {platform_count} 条热门内容。"
                    if cached and platform_count else (platform_errors[-1] if platform_errors else "暂未找到可见内容")
                ),
            })

        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in collected:
            key = str(item["source_key"])
            if key not in seen:
                seen.add(key)
                found.append(item)

        visible_count = sum(1 for item in found if item.get("source_mode") == "visible_browser")
        cached_count = sum(1 for item in found if item.get("source_mode") == "cached_visible_browser")
        public_count = sum(1 for item in found if item.get("source_mode") == "public_index_fallback")
        successful = bool(found)
        message = (
            f"已按已保存的 IP 方向查看双平台内容，其中 {visible_count} 条来自当前可见搜索页。"
            if profile_based else "尚未识别到可安全检索的 IP 方向，已先整理通用的双平台创作方向。"
        )
        return {
            "found": found,
            "errors": errors,
            "platforms": platform_summaries,
            "summary_extra": {
                "monitor_kind": "ip_daily",
                "profile_based": profile_based,
                "visible_signal_count": visible_count,
                "cached_signal_count": cached_count,
                "public_signal_count": public_count,
                "direction_count": len(directions),
                "message": message,
                "recommendations": self._ip_recommendations(directions, found),
            },
        }

    @staticmethod
    def _ip_recommendations(directions: list[dict[str, str]], signals: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Create up to twelve evidence-linked directions from visible platform cards."""
        signals = signals or []
        recommendations: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        # Alternate between the two platforms after sorting each one by its
        # visible engagement, so one platform cannot fill every card.
        by_platform = {
            platform: sorted(
                [item for item in signals if item.get("platform") == platform],
                key=_heat_score,
                reverse=True,
            )
            for platform in ("xiaohongshu", "douyin")
        }
        balanced_signals: list[dict[str, Any]] = []
        for index in range(6):
            for platform in ("xiaohongshu", "douyin"):
                if index < len(by_platform[platform]):
                    balanced_signals.append(by_platform[platform][index])

        for signal in balanced_signals[:10]:
            source_title = _text(signal.get("title"), 100)
            if not source_title:
                continue
            platform = str(signal.get("platform") or "")
            label = "小红书" if platform == "xiaohongshu" else "抖音"
            matched_query = _text(signal.get("matched_query") or (signal.get("raw") or {}).get("search_query"), 180)
            direction = next(
                (item for item in directions if _text(item.get("search_query"), 180) == matched_query),
                directions[len(recommendations) % max(1, len(directions))],
            )
            title = f"从{label}热帖《{source_title[:34]}》延伸：用户真正关心的是什么？"
            if title in seen_titles:
                continue
            seen_titles.add(title)
            query = matched_query or _text(direction.get("search_query"), 180)
            metrics = signal.get("metrics") or {}
            metric_label = ""
            cached = signal.get("source_mode") == "cached_visible_browser"
            if metrics.get("likes") is not None:
                metric_label = f"{'上次成功采集时' if cached else '当前搜索页'}可见点赞 {metrics['likes']}"
            elif metrics.get("visible_engagement") is not None:
                metric_label = f"{'上次成功采集时' if cached else '当前搜索页'}可见互动 {metrics['visible_engagement']}"
            evidence_phrase = "以此前保存的平台真实表达为证据" if cached else "以平台当前展示的真实表达为证据"
            recommendations.append({
                "title": title,
                "topic": title,
                "persona": _text(direction.get("persona"), 80),
                "pillar": _text(direction.get("pillar"), 100),
                "search_query": query,
                "reason": f"{metric_label + '；' if metric_label else ''}{evidence_phrase}，拆出它击中的顾虑、场景和可执行建议，再改写成你的账号视角。",
                "platform_search_links": _official_platform_search_links(query),
                "source_urls": [str(signal.get("url") or "")],
                "source_titles": [source_title],
                "evidence_url": signal.get("url"),
                "evidence_platform": platform,
                "visible_metrics": signal.get("metrics") or {},
            })
        for direction in directions[:3]:
            query = _text(direction.get("search_query"), 180)
            title = _text(direction.get("title"), 160)
            if not query or not title or title in seen_titles:
                continue
            seen_titles.add(title)
            platform_links = _official_platform_search_links(query)
            recommendations.append({
                "title": title,
                "topic": title,
                "persona": _text(direction.get("persona"), 80),
                "pillar": _text(direction.get("pillar"), 100),
                "search_query": query,
                "reason": _text(direction.get("reason"), 220),
                "platform_search_links": platform_links,
                # Keep the normal source-url contract compatible with task
                # creation, but only with official platform search pages.
                "source_urls": [item["url"] for item in platform_links],
                "source_titles": [item["label"] for item in platform_links],
                "public_signal_count": sum(1 for item in signals if item.get("source_mode") != "visible_browser"),
            })
        variants = (
            ("最容易被忽略的 3 个细节", "把高频讨论拆成可观察的细节，适合清单式内容。"),
            ("为什么有人愿意尝试，有人一看就退出？", "从参与门槛、信任和风险感解释同一主题下的不同选择。"),
            ("第一次遇到这个问题，先做哪一步？", "给新人一条低风险、可验证的最小行动。"),
        )
        for direction in directions:
            query = _text(direction.get("search_query"), 180)
            for suffix, reason in variants:
                if len(recommendations) >= 12:
                    break
                title = f"{_text(direction.get('pillar'), 60)}：{suffix}"
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                links = _official_platform_search_links(query)
                recommendations.append({
                    "title": title, "topic": title,
                    "persona": _text(direction.get("persona"), 80),
                    "pillar": _text(direction.get("pillar"), 100),
                    "search_query": query, "reason": reason,
                    "platform_search_links": links,
                    "source_urls": [item["url"] for item in links],
                    "source_titles": [item["label"] for item in links],
                })
        return recommendations[:12]

    def run_due(self) -> int:
        now = utc_now()
        monitors = self.db.all(
            """SELECT id FROM topic_monitors
            WHERE enabled=1 AND monitor_kind='ip_daily' AND (next_run_at IS NULL OR next_run_at<=?)
            ORDER BY COALESCE(next_run_at, created_at) ASC LIMIT 8""",
            (now,),
        )
        complete = 0
        for monitor in monitors:
            try:
                self.run_monitor(str(monitor["id"]))
                complete += 1
            except RadarBusyError:
                continue
            except Exception:
                # Individual errors are written to the run record and should
                # never stop the local scheduler from checking other topics.
                continue
        return complete

    def run_monitor(
        self,
        monitor_id: str,
        workspace_id: str | None = None,
        *,
        _already_claimed: bool = False,
        _started_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        monitor = self.get_monitor(monitor_id, workspace_id)
        if not _already_claimed:
            with self._lock:
                if monitor_id in self._running:
                    raise RadarBusyError("热点正在更新，完成后会自动显示")
                self._running.add(monitor_id)
        run_id, now = new_id("monitor_run"), utc_now()
        self.db.execute(
            "INSERT INTO topic_monitor_runs(id,monitor_id,workspace_id,status,started_at) VALUES(?,?,?,'RUNNING',?)",
            (run_id, monitor_id, monitor["workspace_id"], now),
        )
        self.db.execute(
            "UPDATE topic_monitors SET last_status='RUNNING',last_error=NULL,updated_at=? WHERE id=?",
            (now, monitor_id),
        )
        if _started_event:
            _started_event.set()
        try:
            kind = str(monitor.get("monitor_kind") or "manual")
            extra_summary: dict[str, Any] = {"monitor_kind": kind}
            if kind == "ip_daily":
                daily = self._run_ip_daily(monitor)
                platform_summaries = daily["platforms"]
                found = daily["found"]
                errors = daily["errors"]
                extra_summary.update(daily["summary_extra"])
            else:
                platforms = _clean_platforms(json_loads(monitor.get("platforms_json"), []))
                platform_summaries: list[dict[str, Any]] = []
                found: list[dict[str, Any]] = []
                errors: list[str] = []
                for platform in platforms:
                    if platform == "web":
                        rows, error = self._public_sources(monitor["workspace_id"], str(monitor["query"]), 8)
                        if error:
                            errors.append(error)
                            platform_summaries.append({
                                "platform": "web", "status": "error", "count": 0,
                                "source_mode": "public_web", "message": error,
                            })
                        else:
                            found.extend({
                                "platform": "web", "source_mode": "public_web", "source_key": row["source_key"],
                                "title": row["title"], "url": row["url"], "excerpt": row["excerpt"],
                                "author": "", "published_at": row["published_at"],
                                "metrics": {"credibility": row["credibility"]},
                                "raw": {"search_mode": row["search_mode"]},
                            } for row in rows)
                            platform_summaries.append({
                                "platform": "web", "status": "success", "count": len(rows),
                                "source_mode": "public_web",
                            })
                        continue
                    rows, summary, error = self._platform_sources(
                        monitor["workspace_id"], platform, str(monitor["query"]), 12,
                    )
                    found.extend(rows)
                    platform_summaries.append(summary)
                    if error:
                        errors.append(error)

            new_count = self._save_items(run_id, monitor, found)
            has_success = any(item["status"] == "success" for item in platform_summaries)
            has_action = any(item["status"] in {"manual_search", "login_required"} for item in platform_summaries)
            status = "DONE" if has_success and not errors else "PARTIAL" if has_success or has_action else "FAILED"
            summary = {
                "query": monitor["query"],
                "total": len(found),
                "new_count": new_count,
                "platforms": platform_summaries,
            }
            summary.update(extra_summary)
            completed_at = utc_now()
            next_run = _next_run_at(int(monitor.get("interval_hours") or 24)) if bool(monitor.get("enabled")) else None
            self.db.execute(
                """UPDATE topic_monitor_runs SET status=?,summary_json=?,error_message=?,completed_at=? WHERE id=?""",
                (status, json_dumps(summary), "；".join(errors)[:1000] or None, completed_at, run_id),
            )
            self.db.execute(
                """UPDATE topic_monitors
                SET last_run_at=?,next_run_at=?,last_status=?,last_error=?,updated_at=? WHERE id=?""",
                (completed_at, next_run, status, "；".join(errors)[:1000] or None, completed_at, monitor_id),
            )
            result = self.get_run(run_id, monitor["workspace_id"])
            if self.on_results_ready:
                try:
                    self.on_results_ready(str(monitor["workspace_id"]), result)
                except Exception:
                    # The radar result is complete even if its optional local
                    # extraction queue cannot be updated at this moment.
                    pass
            return result
        except Exception as exc:
            message = sanitize_provider_text(str(exc), 500) or "内容雷达本次刷新失败"
            completed_at = utc_now()
            next_run = _next_run_at(int(monitor.get("interval_hours") or 24)) if bool(monitor.get("enabled")) else None
            self.db.execute(
                "UPDATE topic_monitor_runs SET status='FAILED',error_message=?,completed_at=? WHERE id=?",
                (message, completed_at, run_id),
            )
            self.db.execute(
                """UPDATE topic_monitors SET last_run_at=?,next_run_at=?,last_status='FAILED',last_error=?,updated_at=? WHERE id=?""",
                (completed_at, next_run, message, completed_at, monitor_id),
            )
            return self.get_run(run_id, monitor["workspace_id"])
        finally:
            with self._lock:
                self._running.discard(monitor_id)

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM topic_monitor_runs WHERE id=? AND workspace_id=?", (run_id, workspace_id))
        if not row:
            raise KeyError("监测记录不存在")
        row["items"] = self.db.all(
            "SELECT * FROM topic_monitor_items WHERE run_id=? ORDER BY platform, discovered_at DESC",
            (run_id,),
        )
        return row

    def _save_items(self, run_id: str, monitor: dict[str, Any], items: list[dict[str, Any]]) -> int:
        now = utc_now()
        new_count = 0
        for item in items:
            source_key = str(item.get("source_key") or _stable_key(str(item.get("url") or item.get("title") or "")))
            prior = self.db.one(
                "SELECT id FROM topic_monitor_items WHERE monitor_id=? AND platform=? AND source_key=? LIMIT 1",
                (monitor["id"], item["platform"], source_key),
            )
            is_new = prior is None
            if is_new:
                new_count += 1
            self.db.execute(
                """INSERT OR IGNORE INTO topic_monitor_items
                (id,run_id,monitor_id,workspace_id,platform,source_mode,source_key,title,url,excerpt,author,published_at,metrics_json,raw_json,is_new,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("monitor_item"), run_id, monitor["id"], monitor["workspace_id"], item["platform"],
                    item["source_mode"], source_key, str(item.get("title") or "相关内容")[:500],
                    str(item.get("url") or "")[:2000], str(item.get("excerpt") or "")[:4000],
                    str(item.get("author") or "")[:300], str(item.get("published_at") or "") or None,
                    json_dumps(item.get("metrics") or {}), json_dumps(item.get("raw") or {}), int(is_new), now,
                ),
            )
        return new_count


class TopicRadarWorker:
    def __init__(self, service: TopicRadarService, poll_seconds: float = 60) -> None:
        self.service = service
        self.poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="topic-radar-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.run_due()
            except Exception:
                pass
            self._wake.wait(self.poll_seconds)
            self._wake.clear()


topic_radar_service = TopicRadarService()
topic_radar_worker = TopicRadarWorker(topic_radar_service)
