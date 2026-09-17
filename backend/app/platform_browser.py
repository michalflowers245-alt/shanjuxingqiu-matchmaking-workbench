from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import RADAR_CAPTURE_DIR, STATE_DIR


CDP_PORT = int(os.getenv("WORKBENCH_SOCIAL_CDP_PORT", "9228"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = STATE_DIR / "social_browser"
OCR_SCRIPT = Path(__file__).with_name("macos_vision_ocr.swift")


def _debug_platform(event: str) -> None:
    if os.getenv("WORKBENCH_DEBUG_PLATFORM") == "1":
        print(f"[platform-browser] {event}", file=sys.stderr, flush=True)


class PlatformBrowserError(RuntimeError):
    def __init__(self, message: str, *, code: str = "browser_error", diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


def _connect_over_cdp(playwright: Any, cdp_url: str):
    """Attach to newer Chrome without applying unsupported context defaults.

    Chrome 152 can reject Playwright's Browser.setDownloadBehavior command for
    an externally launched persistent profile.  Playwright 1.62 exposes
    no_defaults specifically for this kind of CDP attachment.  Keep a fallback
    for packaged installations that still use an older Playwright release.
    """
    try:
        return playwright.chromium.connect_over_cdp(cdp_url, no_defaults=True, timeout=6000)
    except TypeError:
        return playwright.chromium.connect_over_cdp(cdp_url, timeout=6000)


def _clean_xhs_title_hint(value: str) -> str:
    """Recover the real platform title from a generated recommendation title."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    quoted = re.search(r"《(.+?)》", text)
    if quoted:
        text = quoted.group(1).strip()
    text = re.sub(r"\s*[-–—]\s*小红书\s*$", "", text).strip()
    return text[:180]


def _search_url(platform: str, query: str) -> str:
    clean = str(query or "").strip()[:180]
    if platform == "xiaohongshu":
        return "https://www.xiaohongshu.com/search_result/?" + urllib.parse.urlencode({"keyword": clean})
    if platform == "douyin":
        return f"https://www.douyin.com/search/{urllib.parse.quote(clean, safe='')}?type=video"
    raise ValueError("不支持的平台")


def _chrome_path() -> str:
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ])
    elif os.name == "nt":
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
            if root:
                candidates.extend([
                    str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                ])
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            value = shutil.which(name)
            if value:
                candidates.append(value)
    return next((value for value in candidates if Path(value).is_file()), "")


def _metric_number(value: str) -> int | float | None:
    clean = str(value or "").strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万wW千kK]?)", clean)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit in {"万", "w"}:
        number *= 10000
    elif unit in {"千", "k"}:
        number *= 1000
    return int(number) if number.is_integer() else round(number, 1)


def _visible_metrics(text: str) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    patterns = {
        "likes": r"(?:点赞|获赞|赞)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:点赞|获赞|赞)",
        "comments": r"(?:评论)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:评论)",
        "shares": r"(?:分享|转发)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:分享|转发)",
        "favorites": r"(?:收藏)\s*[:：]?\s*(\d+(?:\.\d+)?\s*[万wW千kK]?)|(\d+(?:\.\d+)?\s*[万wW千kK]?)\s*(?:收藏)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            number = _metric_number(match.group(1) or match.group(2) or "")
            if number is not None:
                values[key] = number
    # Xiaohongshu search cards often show one bare engagement number after
    # the author/date without a visible "赞" label.  Preserve it under a
    # neutral name rather than claiming which native metric it represents.
    if not values:
        candidates = re.findall(r"(?:^|\s)(\d+(?:\.\d+)?\s*[万wW千kK]?)(?=\s|$)", str(text or ""))
        if candidates:
            number = _metric_number(candidates[-1])
            if number is not None:
                values["visible_engagement"] = number
    return values


def _safe_url(platform: str, value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return ""
    host = parsed.hostname or ""
    allowed = host == "douyin.com" or host.endswith(".douyin.com") if platform == "douyin" else host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
    if parsed.scheme != "https" or not allowed:
        return ""
    # Result links may need xsec_token to open, but unrelated tracking data is discarded.
    keep = {key: values for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items() if key in {"xsec_token", "xsec_source", "type"}}
    path = parsed.path
    # The visible Xiaohongshu result card points at /search_result/<note-id>
    # and carries the short-lived access token.  A hidden SEO link points at
    # /explore/<note-id> without that token and is rejected with error 300031.
    # Store the canonical post path while preserving only the access fields.
    if platform == "xiaohongshu":
        result_match = re.fullmatch(r"/search_result/([A-Za-z0-9]+)", path.rstrip("/"))
        if result_match:
            path = f"/explore/{result_match.group(1)}"
            if keep.get("xsec_token") and not any(keep.get("xsec_source") or []):
                keep["xsec_source"] = ["pc_search"]
    query = urllib.parse.urlencode(keep, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def _diagnostic_url(value: str) -> str:
    """Keep a useful page location in logs without persisting access tokens."""
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _public_post_url(platform: str, value: str) -> str:
    """Return a stable shareable URL; temporary platform access data stays in memory."""
    safe = _safe_url(platform, value)
    if platform != "xiaohongshu" or not safe:
        return safe
    parsed = urllib.parse.urlsplit(safe)
    if re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", parsed.path):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return safe


def _best_xhs_search_result(
    rows: list[dict[str, Any]], post_key: str, title_hint: str,
) -> str:
    """Return only the same post ID; a similar title is never an identity key."""
    for row in rows:
        url = _safe_url("xiaohongshu", str(row.get("href") or ""))
        if not url:
            continue
        path = urllib.parse.urlsplit(url).path
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", path)
        candidate_key = match.group(1) if match else ""
        if candidate_key == post_key:
            return url
    return ""


def _similar_xhs_search_result(
    rows: list[dict[str, Any]], post_key: str, title_hint: str,
) -> tuple[str, str]:
    """Find a title match only to report IDENTITY_MISMATCH, never to archive it."""
    normalized_hint = re.sub(r"\W+", "", str(title_hint or "")).lower()
    best_url, best_key, best_score = "", "", 0.0
    for row in rows:
        url = _safe_url("xiaohongshu", str(row.get("href") or ""))
        if not url:
            continue
        path = urllib.parse.urlsplit(url).path
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", path)
        candidate_key = match.group(1) if match else ""
        if not candidate_key or candidate_key == post_key:
            continue
        candidate_title = re.sub(r"\W+", "", str(row.get("title") or row.get("text") or "")).lower()
        if not normalized_hint or not candidate_title:
            continue
        score = difflib.SequenceMatcher(None, normalized_hint, candidate_title[: max(len(normalized_hint) * 2, 30)]).ratio()
        if normalized_hint in candidate_title or candidate_title in normalized_hint:
            score = max(score, 0.86)
        if score > best_score:
            best_url, best_key, best_score = url, candidate_key, score
    return (best_url, best_key) if best_score >= 0.58 else ("", "")


def _run_local_ocr(image_path: Path) -> dict[str, str]:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift or not OCR_SCRIPT.is_file():
        return {"status": "unavailable", "text": "", "message": "图片文字尚未提取：OCR（光学字符识别）运行环境不可用。"}
    try:
        result = subprocess.run([swift, str(OCR_SCRIPT), str(image_path)], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "text": "", "message": f"图片文字尚未提取：OCR 运行失败（{str(exc)[:120]}）。"}
    text = result.stdout.strip()
    if result.returncode != 0:
        return {"status": "failed", "text": "", "message": f"图片文字尚未提取：OCR 运行失败（{result.stderr.strip()[:120]}）。"}
    return {"status": "success" if text else "empty", "text": text, "message": "已使用 macOS Vision 在本机识别图片文字。" if text else "OCR 已运行，但没有识别到清晰文字。"}


class PlatformBrowserCollector:
    """Collect only content cards rendered in a dedicated, visible local browser.

    The browser profile keeps the platform's own login session.  The workbench
    never asks for a password, exports cookies, or attempts to solve a captcha.
    """

    def __init__(self, cdp_url: str = CDP_URL, profile_dir: Path = PROFILE_DIR) -> None:
        self.cdp_url = cdp_url.rstrip("/")
        self.profile_dir = profile_dir
        self._lock = threading.RLock()
        # Xiaohongshu result cards carry a short-lived xsec_token.  Persist only
        # the clean public URL, but keep the access URL in this process so the
        # automatic detail reader can reuse it immediately after a search.
        self._xhs_access_urls: dict[str, tuple[str, float]] = {}

    def _remember_xhs_access_url(self, value: str) -> str:
        access_url = _safe_url("xiaohongshu", value)
        public_url = _public_post_url("xiaohongshu", value)
        if not access_url or not public_url or "xsec_token=" not in access_url:
            return public_url
        now = time.monotonic()
        self._xhs_access_urls[public_url] = (access_url, now + 30 * 60)
        if len(self._xhs_access_urls) > 120:
            self._xhs_access_urls = {
                key: item for key, item in self._xhs_access_urls.items() if item[1] > now
            }
        return public_url

    def _recent_xhs_access_url(self, public_url: str) -> str:
        item = self._xhs_access_urls.get(public_url)
        if not item:
            return ""
        access_url, expires_at = item
        if expires_at <= time.monotonic():
            self._xhs_access_urls.pop(public_url, None)
            return ""
        return access_url

    def status(self) -> dict[str, Any]:
        chrome = _chrome_path()
        running = False
        try:
            response = httpx.get(f"{self.cdp_url}/json/version", timeout=0.8, trust_env=False)
            running = response.is_success
        except httpx.HTTPError:
            pass
        return {
            "available": bool(chrome),
            "running": running,
            "mode": "visible_local_browser",
            "profile_persistent": True,
            "session_storage": "local_chrome_profile",
            "message": (
                "平台采集浏览器已打开；登录状态保存在本机专用浏览器资料中。"
                if running else "请先打开平台采集浏览器并扫码登录一次；以后会复用本机登录状态。"
                if chrome else "没有找到 Google Chrome 或 Microsoft Edge，请先安装浏览器。"
            ),
        }

    def _start(self, initial_url: str) -> None:
        if self.status()["running"]:
            return
        chrome = _chrome_path()
        if not chrome:
            raise PlatformBrowserError("没有找到 Google Chrome 或 Microsoft Edge", code="browser_missing")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={CDP_PORT}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile_dir}",
                "--profile-directory=Default",
                "--no-first-run",
                "--no-default-browser-check",
                initial_url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.status()["running"]:
                return
            time.sleep(0.25)
        raise PlatformBrowserError("平台采集浏览器启动超时，请关闭后重试", code="browser_start_timeout")

    def open(self, platform: str = "both", query: str = "相亲") -> dict[str, Any]:
        platforms = ["xiaohongshu", "douyin"] if platform == "both" else [platform]
        if any(value not in {"xiaohongshu", "douyin"} for value in platforms):
            raise ValueError("请选择小红书、抖音或双平台")
        with self._lock:
            already_running = bool(self.status()["running"])
            self._start(_search_url(platforms[0], query))
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = _connect_over_cdp(playwright, self.cdp_url)
                    context = browser.contexts[0]
                    for value in (platforms if already_running else platforms[1:]):
                        page = context.new_page()
                        page.goto(_search_url(value, query), wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise PlatformBrowserError("浏览器已打开，但暂时无法连接搜索页，请稍后再试", code="browser_connect_error") from exc
        return self.status()

    def search(self, platform: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        if platform not in {"xiaohongshu", "douyin"}:
            raise ValueError("不支持的平台")
        if not self.status()["running"]:
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先点击“打开双平台浏览器”并登录", code="browser_not_running")
        with self._lock:
            try:
                from playwright.sync_api import sync_playwright
                _debug_platform(f"search:{platform}:playwright-start")
                with sync_playwright() as playwright:
                    _debug_platform(f"search:{platform}:connect-start")
                    browser = _connect_over_cdp(playwright, self.cdp_url)
                    _debug_platform(f"search:{platform}:connect-done")
                    context = browser.contexts[0]
                    host_marker = "xiaohongshu.com" if platform == "xiaohongshu" else "douyin.com"
                    page = next((item for item in context.pages if host_marker in item.url), None) or context.new_page()
                    selector = (
                        'a[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]'
                        if platform == "xiaohongshu" else
                        'a[href*="/video/"],a[href*="/note/"]'
                    )
                    target_url = _search_url(platform, query)
                    current = urllib.parse.urlsplit(page.url)
                    target = urllib.parse.urlsplit(target_url)
                    same_search = (
                        current.netloc == target.netloc
                        and current.path == target.path
                        and urllib.parse.parse_qs(current.query).get("keyword")
                        == urllib.parse.parse_qs(target.query).get("keyword")
                    )
                    _debug_platform(f"search:{platform}:goto-start")
                    if not (same_search and page.locator(selector).count() > 0):
                        page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                    _debug_platform(f"search:{platform}:goto-done")
                    try:
                        page.wait_for_selector(selector, state="attached", timeout=10000)
                        page.wait_for_timeout(500)
                    except Exception:
                        page.wait_for_timeout(1200)
                    # Chrome 152 can leave the CDP Input.dispatchMouseEvent
                    # command pending forever on an attached persistent
                    # profile.  The first result batch is already present in
                    # the DOM, so a synthetic wheel event is unnecessary.
                    _debug_platform(f"search:{platform}:evaluate-start")
                    rows = page.evaluate(
                        r"""({platform, limit}) => {
                          const wanted = platform === 'xiaohongshu'
                            ? 'section.note-item a.cover[href*="/search_result/"],section.note-item a.title[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]'
                            : 'a[href*="/video/"],a[href*="/note/"]';
                          const seen = new Set(); const output = [];
                          for (const link of document.querySelectorAll(wanted)) {
                            const href = link.href || ''; if (!href || seen.has(href)) continue;
                            if (platform === 'xiaohongshu' && link.offsetParent === null && !link.matches('section.note-item a.cover,section.note-item a.title')) continue;
                            let node = link; let best = link; let bestText = (link.innerText || '').trim();
                            for (let i = 0; i < 7 && node.parentElement; i += 1) {
                              node = node.parentElement; const text = (node.innerText || '').trim();
                              if (text.length >= 8 && text.length <= 1200 && text.length >= bestText.length) { best = node; bestText = text; }
                              if (node.matches('article,li,section,[data-e2e*="search"],[class*="note-item"],[class*="search-card"]')) break;
                            }
                            const image = best.querySelector('img[alt]');
                            const lines = bestText.split(/\n+/).map(v => v.trim()).filter(Boolean);
                            const platformTitle = platform === 'xiaohongshu' ? (best.querySelector('.title')?.innerText || '') : '';
                            const title = platformTitle || link.getAttribute('title') || link.getAttribute('aria-label') || image?.alt || lines.find(v => v.length > 5 && !/^\d+(\.\d+)?[万wWkK]?$/.test(v)) || '';
                            const authorNode = best.querySelector('[class*="author"] [class*="name"],[class*="author"],[class*="user-name"],[class*="nickname"]');
                            let metricText = '';
                            if (platform === 'xiaohongshu') metricText = (best.querySelector('.like-wrapper .count')?.innerText || '').trim();
                            else {
                              const durationIndex = lines.findIndex(v => /^\d{1,2}:\d{2}$/.test(v));
                              if (durationIndex >= 0 && /^\d+(\.\d+)?[万wWkK]?$/.test(lines[durationIndex + 1] || '')) metricText = lines[durationIndex + 1];
                            }
                            seen.add(href); output.push({href, title, text: bestText, author: (authorNode?.innerText || '').trim(), metricText});
                            if (output.length >= limit) break;
                          }
                          return {rows: output, bodyText: (document.body?.innerText || '').slice(0, 3000), pageTitle: document.title};
                        }""",
                        {"platform": platform, "limit": min(max(int(limit), 1), 24)},
                    )
                    _debug_platform(f"search:{platform}:evaluate-done")
                _debug_platform(f"search:{platform}:playwright-stop")
            except PlatformBrowserError:
                raise
            except Exception as exc:
                message = str(exc)
                if "Timeout" in message:
                    raise PlatformBrowserError("平台搜索页加载超时，请检查网络后重试", code="page_timeout") from exc
                raise PlatformBrowserError("暂时无法读取平台搜索页，请在采集浏览器中确认页面能正常打开", code="page_read_error") from exc
        raw_rows = rows.get("rows") if isinstance(rows, dict) else []
        body_text = str(rows.get("bodyText") or "") if isinstance(rows, dict) else ""
        page_title = str(rows.get("pageTitle") or "") if isinstance(rows, dict) else ""
        if not raw_rows:
            if any(marker in f"{page_title} {body_text}" for marker in ("验证码", "安全验证", "验证后继续", "完成验证")):
                raise PlatformBrowserError("平台要求完成安全验证，请在采集浏览器里完成后重新搜索", code="verification_required")
            if any(marker in body_text for marker in ("扫码登录", "登录后", "手机号登录")):
                raise PlatformBrowserError("平台尚未登录，请在采集浏览器里扫码登录后重新搜索", code="login_required")
            raise PlatformBrowserError("搜索页暂时没有读到内容卡片，请在采集浏览器确认页面后重试", code="no_visible_results")
        result: list[dict[str, Any]] = []
        for rank, raw in enumerate(raw_rows, start=1):
            if not isinstance(raw, dict):
                continue
            raw_url = str(raw.get("href") or "")
            url = self._remember_xhs_access_url(raw_url) if platform == "xiaohongshu" else _public_post_url(platform, raw_url)
            text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()[:2000]
            title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:300]
            if not url or not title:
                continue
            metrics = _visible_metrics(text)
            like_count = _metric_number(str(raw.get("metricText") or ""))
            if like_count is not None:
                metrics = {"likes": like_count}
            result.append({
                "source_key": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                "title": title,
                "url": url,
                "excerpt": text,
                "author": re.sub(r"\s+", " ", str(raw.get("author") or "")).strip()[:160],
                "published_at": None,
                "metrics": metrics,
                "raw": {"rank": rank, "collection": "visible_search_card"},
            })
        if not result:
            raise PlatformBrowserError("平台页面已打开，但没有得到可保存的内容链接", code="no_valid_results")
        return result

    def extract_xiaohongshu_detail(
        self, value: str, *, search_keyword: str = "", title_hint: str = "",
    ) -> dict[str, Any]:
        url = _safe_url("xiaohongshu", value)
        if not url or not re.search(r"/(?:explore|discovery/item)/[A-Za-z0-9]+", urllib.parse.urlsplit(url).path):
            raise PlatformBrowserError("这不是可提取的小红书原帖链接", code="invalid_post_url")
        if not self.status()["running"]:
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先点击“打开双平台浏览器”并登录", code="browser_not_running")
        post_key = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", urllib.parse.urlsplit(url).path).group(1)
        capture_dir = RADAR_CAPTURE_DIR / post_key
        capture_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = capture_dir / "page.png"
        resolution_mode = "direct_url"
        original_url = _public_post_url("xiaohongshu", url)
        recent_access_url = self._recent_xhs_access_url(original_url)
        if recent_access_url:
            url = recent_access_url
            resolution_mode = "recent_search_access_url"
        with self._lock:
            page = None
            keep_page_open = False
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = _connect_over_cdp(playwright, self.cdp_url)
                    context = browser.contexts[0]
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    initial = page.evaluate(r"""() => ({
                      title: document.title || '',
                      text: (document.body?.innerText || '').slice(0, 4000),
                      url: location.href
                    })""")
                    initial_diagnostic = urllib.parse.unquote(f"{initial.get('url')} {initial.get('title')} {initial.get('text')}")
                    unavailable = re.search(
                        r"内容不存在|笔记不存在|已删除|页面不存在|当前笔记暂时无法浏览|error_code=300031|你访问的页面不见了",
                        initial_diagnostic,
                    )
                    if unavailable and (title_hint.strip() or search_keyword.strip()):
                        recovered_url = ""
                        mismatch_url = ""
                        mismatch_post_key = ""
                        attempted_queries: list[str] = []
                        clean_title = _clean_xhs_title_hint(title_hint)
                        for query in (clean_title, title_hint.strip(), search_keyword.strip()):
                            query = query[:180]
                            if not query or query in attempted_queries:
                                continue
                            attempted_queries.append(query)
                            page.goto(_search_url("xiaohongshu", query), wait_until="domcontentloaded", timeout=45000)
                            try:
                                page.wait_for_selector('section.note-item a.cover[href*="/search_result/"]', state="visible", timeout=4000)
                            except Exception:
                                page.wait_for_timeout(700)
                            search_data = page.evaluate(r"""() => ({
                              title: document.title || '',
                              text: (document.body?.innerText || '').slice(0, 5000),
                              rows: [...document.querySelectorAll('section.note-item a.cover[href*="/search_result/"],section.note-item a.title[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]')].filter(link => link.offsetParent !== null).slice(0, 40).map(link => {
                                let node=link, best=link, bestText=(link.innerText||'').trim();
                                for(let i=0;i<7&&node.parentElement;i+=1){ node=node.parentElement; const text=(node.innerText||'').trim(); if(text.length>=bestText.length&&text.length<=1500){best=node;bestText=text;} if(node.matches('article,li,section,[class*="note-item"]'))break; }
                                const title=(best.querySelector('.title')?.innerText||link.getAttribute('title')||bestText.split(/\n+/).find(v=>v.trim().length>5)||'').trim();
                                return {href:link.href||'',title,text:bestText};
                              })
                            })""")
                            search_diagnostic = f"{search_data.get('title')} {search_data.get('text')}"
                            if re.search(r"验证码|安全验证|验证后继续|完成验证", search_diagnostic):
                                keep_page_open = True
                                page.bring_to_front()
                                raise PlatformBrowserError("小红书要求完成安全验证，请在采集浏览器中完成后重试", code="verification_required")
                            if re.search(r"扫码登录|手机号登录|登录后查看|请先登录", search_diagnostic):
                                keep_page_open = True
                                page.bring_to_front()
                                raise PlatformBrowserError("小红书登录已失效，请在采集浏览器扫码登录后重试", code="login_required")
                            recovered_url = _best_xhs_search_result(search_data.get("rows") or [], post_key, clean_title or title_hint)
                            if recovered_url:
                                break
                            mismatch_url, mismatch_post_key = _similar_xhs_search_result(
                                search_data.get("rows") or [], post_key, clean_title or title_hint,
                            )
                        if recovered_url:
                            url = recovered_url
                            self._remember_xhs_access_url(recovered_url)
                            resolution_mode = "search_result_recovered"
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(900)
                        else:
                            page.close()
                            page = None
                            if mismatch_url:
                                raise PlatformBrowserError(
                                    "搜索结果标题相似，但帖子 ID 不一致，已拒绝用其他帖子覆盖原记录",
                                    code="identity_mismatch",
                                    diagnostics={
                                        "target_post_id": post_key,
                                        "observed_post_id": mismatch_post_key,
                                        "candidate_url": _diagnostic_url(mismatch_url),
                                    },
                                )
                            raise PlatformBrowserError(
                                "原帖旧链接已失效，工作台按标题重新搜索后仍未找到同一篇帖子",
                                code="post_unavailable",
                                diagnostics={
                                    "reason": "stale_link_not_found_by_title",
                                    "post_key": post_key,
                                    "clean_title_hint": clean_title,
                                    "attempted_queries": attempted_queries,
                                    "initial_page_title": str(initial.get("title") or "")[:300],
                                    "initial_url": _diagnostic_url(str(initial.get("url") or "")),
                                },
                            )
                    for pattern in (re.compile(r"^展开$"), re.compile(r"^全文$"), re.compile(r"展开正文")):
                        for item in page.get_by_text(pattern).all()[:3]:
                            try:
                                if item.is_visible():
                                    item.click(timeout=1200)
                            except Exception:
                                pass
                    page.wait_for_timeout(250)
                    data = page.evaluate(r"""() => {
                      const clean = v => String(v || '').replace(/\u200b/g,'').replace(/[ \t]+/g,' ').replace(/\n{3,}/g,'\n\n').trim();
                      const visible = n => !!(n && (n.getClientRects().length || n.offsetParent !== null));
                      const texts = selectors => selectors.flatMap(s => [...document.querySelectorAll(s)].filter(visible).map(n => clean(n.innerText || n.textContent)).filter(Boolean));
                      const first = selectors => texts(selectors).sort((a,b) => b.length-a.length)[0] || '';
                      const meta = s => document.querySelector(s)?.getAttribute('content')?.trim() || '';
                      const json = [];
                      for (const n of document.querySelectorAll('script[type="application/ld+json"]')) { try { const v=JSON.parse(n.textContent||'null'); Array.isArray(v)?json.push(...v):v&&json.push(v); } catch {} }
                      const flat=[]; const walk=v=>{ if(!v||typeof v!=='object')return; flat.push(v); Object.values(v).forEach(walk); }; json.forEach(walk);
                      const article=flat.find(v=>v.articleBody||v.headline||/Article|Posting/i.test(String(v['@type']||'')))||{};
                      const author=typeof article.author==='string'?article.author:(article.author?.name||'');
                      const bodySelectors=['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc','.note-content .desc','[class*="note-detail"] [class*="desc"]','article [class*="content"]'];
                      const subtitle=texts(['[class*="subtitle"]','[class*="caption"]','[data-testid*="subtitle"]']).join('\n');
                      return {
                        title: clean(article.headline || meta('meta[property="og:title"]') || first(['h1','[data-testid="note-title"]','#detail-title','[class*="note-detail"] [class*="title"]'])),
                        author: clean(author || meta('meta[name="author"]') || first(['[data-testid="author-name"]','a[href*="/user/profile"] [class*="name"]','[class*="author"] [class*="name"]','[class*="nickname"]'])),
                        body: clean(article.articleBody || meta('meta[property="og:description"]') || meta('meta[name="description"]') || first(bodySelectors)),
                        publishedAt: clean(article.datePublished || meta('meta[property="article:published_time"]') || first(['time','[data-testid="publish-time"]','[class*="publish-time"]','[class*="date"]'])),
                        tags: [...new Set([...String(article.keywords||'').split(/[,，]/), ...texts(['a[href*="/search_result"]','a[href*="/search?keyword"]','[class*="tag"]']).flatMap(v=>v.split(/\s+/))].map(clean).filter(v=>v.startsWith('#')||v.length>1))].slice(0,30),
                        subtitle: clean(subtitle),
                        visibleText: clean(document.body?.innerText||'').slice(0,30000),
                        pageTitle: document.title, finalUrl: location.href,
                        imageCount: document.querySelectorAll('article img,[class*="note-detail"] img,[class*="swiper"] img').length,
                        videoCount: document.querySelectorAll('video').length
                      };
                    }""")
                    visible_text = str(data.get("visibleText") or "")
                    diagnostic_text = urllib.parse.unquote(f"{data.get('finalUrl')} {data.get('pageTitle')} {visible_text}")
                    if re.search(r"验证码|安全验证|验证后继续|完成验证", diagnostic_text):
                        keep_page_open = True
                        page.bring_to_front()
                        raise PlatformBrowserError("小红书要求完成安全验证，请在采集浏览器中完成后重试", code="verification_required")
                    if re.search(r"IP存在风险|访问过于频繁|网络环境.*重试|error_code=300012", diagnostic_text):
                        page.close()
                        page = None
                        raise PlatformBrowserError("小红书限制了当前网络或浏览器环境，请稍后在可靠网络中重试", code="platform_limited")
                    if re.search(r"扫码登录|手机号登录|登录后查看|请先登录", visible_text):
                        keep_page_open = True
                        page.bring_to_front()
                        raise PlatformBrowserError("小红书登录已失效，请在采集浏览器扫码登录后重试", code="login_required")
                    if re.search(r"内容不存在|笔记不存在|已删除|页面不存在|当前笔记暂时无法浏览|error_code=300031|你访问的页面不见了", diagnostic_text):
                        page.close()
                        page = None
                        raise PlatformBrowserError(
                            "帖子不存在、已删除或当前账号不可访问",
                            code="post_unavailable",
                            diagnostics={
                                "reason": "detail_page_unavailable_after_recovery",
                                "post_key": post_key,
                                "page_title": str(data.get("pageTitle") or "")[:300],
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                                "resolution_mode": resolution_mode,
                            },
                        )
                    screenshot_saved = False
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=False, timeout=5000)
                        screenshot_saved = screenshot_path.is_file()
                    except Exception:
                        # A platform animation can keep Playwright's screenshot
                        # stability check waiting.  The already-read copy is the
                        # primary result and must still be delivered.
                        screenshot_saved = False
                    image_texts: list[str] = []
                    ocr = {"status": "not_needed", "text": "", "message": "网页正文已读取，不需要 OCR。"}
                    if int(data.get("imageCount") or 0) > 0 and len(str(data.get("body") or "")) < 180:
                        images = page.locator('article img,[class*="note-detail"] img,[class*="swiper"] img')
                        for index in range(min(images.count(), 4)):
                            image = images.nth(index)
                            try:
                                box = image.bounding_box()
                                if not box or box["width"] < 220 or box["height"] < 140:
                                    continue
                                image_path = capture_dir / f"image-{index + 1}.png"
                                image.screenshot(path=str(image_path))
                                current = _run_local_ocr(image_path)
                                ocr = current
                                if current["text"]:
                                    image_texts.append(current["text"])
                            except Exception:
                                continue
                    image_text = "\n\n".join(dict.fromkeys(image_texts))
                    subtitle = str(data.get("subtitle") or "").strip()
                    asr_status = "subtitle_available" if subtitle else "unavailable" if int(data.get("videoCount") or 0) else "not_needed"
                    asr_message = "已读取页面可见字幕。" if subtitle else "视频口播暂未提取：页面没有可读字幕，工作台不会绕过平台限制下载视频。" if int(data.get("videoCount") or 0) else "帖子不是视频，或网页正文已经可读。"
                    blocks = [str(data.get("body") or "").strip(), image_text, subtitle]
                    merged = "\n\n".join(dict.fromkeys(block for block in blocks if block))
                    sources = {}
                    if data.get("title"): sources["title"] = "网页结构化数据或 DOM"
                    if data.get("author"): sources["author"] = "网页结构化数据或 DOM"
                    if data.get("body"): sources["body"] = "网页结构化数据或 DOM"
                    if image_text: sources["image_text"] = "macOS Vision OCR"
                    if subtitle: sources["video_subtitle"] = "页面可见字幕"
                    checks = [data.get("title"), data.get("author"), merged, data.get("publishedAt"), data.get("tags"), _visible_metrics(visible_text), screenshot_saved]
                    completeness = round(sum(bool(item) for item in checks) / len(checks) * 100)
                    result = {
                        "platform": "xiaohongshu", "url": original_url, "post_key": post_key,
                        "title": str(data.get("title") or "")[:500], "author": str(data.get("author") or "")[:300],
                        "published_at": str(data.get("publishedAt") or "")[:100] or None,
                        "body": str(data.get("body") or "")[:30000], "image_text": image_text[:30000],
                        "video_subtitle": subtitle[:30000], "video_speech": "", "merged_copy": merged[:60000],
                        "tags": data.get("tags") or [], "metrics": _visible_metrics(visible_text),
                        "screenshot_name": f"{post_key}/page.png" if screenshot_saved else "", "status": "success" if data.get("title") and merged else "partial" if merged else "failed",
                        "completeness": completeness, "field_sources": sources,
                        "ocr_status": ocr["status"], "ocr_message": ocr["message"],
                        "asr_status": asr_status, "asr_message": asr_message,
                        "failure_stage": "" if merged else "content_extract",
                        "error_message": "" if merged else "详情页已打开，但没有提取到正文、图片文字或字幕。",
                        "diagnostics": {"page_title": data.get("pageTitle"), "final_url": _diagnostic_url(str(data.get("finalUrl") or "")), "resolved_url": _diagnostic_url(url), "resolution_mode": resolution_mode, "visible_text_length": len(visible_text), "image_count": data.get("imageCount"), "video_count": data.get("videoCount"), "screenshot_saved": screenshot_saved},
                    }
                    page.close()
                    page = None
                    return result
            except PlatformBrowserError:
                raise
            except Exception as exc:
                raise PlatformBrowserError(f"提取详情失败：{str(exc)[:180]}", code="detail_extract_failed") from exc
            finally:
                if page is not None and not keep_page_open:
                    try: page.close()
                    except Exception: pass

    def archive_xiaohongshu_detail(
        self, value: str, *, search_keyword: str = "", title_hint: str = "",
    ) -> dict[str, Any]:
        """Read one authorized detail page and return exact text plus image bytes.

        Temporary signed URLs and response headers remain in memory.  The archive
        repository receives only response bytes and a query-free source reference.
        """
        url = _safe_url("xiaohongshu", value)
        parsed = urllib.parse.urlsplit(url)
        match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", parsed.path)
        if not url or not match:
            raise PlatformBrowserError("这不是可归档的小红书原帖链接", code="invalid_post_url")
        if not self.status()["running"]:
            raise PlatformBrowserError("平台采集浏览器尚未打开，请先打开并登录", code="browser_not_running")
        target_post_id = match.group(1)
        canonical_url = _public_post_url("xiaohongshu", url)
        access_url = self._recent_xhs_access_url(canonical_url) or url
        with self._lock:
            page = None
            keep_page_open = False
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = _connect_over_cdp(playwright, self.cdp_url)
                    context = browser.contexts[0]
                    page = context.new_page()
                    observed_responses: list[Any] = []

                    def remember_response(response: Any) -> None:
                        try:
                            content_type = str(response.headers.get("content-type") or "").lower()
                            if content_type.startswith("image/"):
                                observed_responses.append(response)
                        except Exception:
                            return

                    page.on("response", remember_response)
                    page.goto(access_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)
                    initial = page.evaluate(r"""() => ({
                      title: document.title || '',
                      text: (document.body?.innerText || '').slice(0, 5000),
                      url: location.href
                    })""")
                    initial_text = urllib.parse.unquote(
                        f"{initial.get('url')} {initial.get('title')} {initial.get('text')}"
                    )
                    if re.search(r"验证码|安全验证|验证后继续|完成验证", initial_text):
                        keep_page_open = True
                        page.bring_to_front()
                        raise PlatformBrowserError(
                            "小红书要求完成安全验证，请在采集浏览器中处理后点击继续补齐",
                            code="verification_required",
                        )
                    if re.search(r"扫码登录|手机号登录|登录后查看|请先登录", initial_text):
                        keep_page_open = True
                        page.bring_to_front()
                        raise PlatformBrowserError(
                            "小红书登录已失效，请扫码登录后点击继续补齐",
                            code="login_required",
                        )
                    unavailable = re.search(
                        r"内容不存在|笔记不存在|已删除|页面不存在|当前笔记暂时无法浏览|error_code=300031|你访问的页面不见了",
                        initial_text,
                    )
                    if unavailable:
                        queries = []
                        clean_title = _clean_xhs_title_hint(title_hint)
                        mismatch_url = ""
                        mismatch_post_id = ""
                        recovered_url = ""
                        for query in (clean_title, title_hint.strip(), search_keyword.strip()):
                            query = query[:180]
                            if not query or query in queries:
                                continue
                            queries.append(query)
                            page.goto(_search_url("xiaohongshu", query), wait_until="domcontentloaded", timeout=45000)
                            try:
                                page.wait_for_selector(
                                    'section.note-item a.cover[href*="/search_result/"]',
                                    state="visible",
                                    timeout=4000,
                                )
                            except Exception:
                                page.wait_for_timeout(700)
                            search_data = page.evaluate(r"""() => ({
                              text:(document.body?.innerText||'').slice(0,5000), title:document.title||'',
                              rows:[...document.querySelectorAll('section.note-item a.cover[href*="/search_result/"],section.note-item a.title[href*="/search_result/"],a[href*="/explore/"],a[href*="/discovery/item/"]')]
                                .filter(link=>link.offsetParent!==null).slice(0,40).map(link=>{
                                  let node=link,best=link,bestText=(link.innerText||'').trim();
                                  for(let i=0;i<7&&node.parentElement;i+=1){node=node.parentElement;const text=(node.innerText||'').trim();if(text.length>=bestText.length&&text.length<=1500){best=node;bestText=text;}if(node.matches('article,li,section,[class*="note-item"]'))break;}
                                  return {href:link.href||'',title:(best.querySelector('.title')?.innerText||link.getAttribute('title')||bestText.split(/\n+/).find(v=>v.trim().length>5)||'').trim(),text:bestText};
                                })
                            })""")
                            search_text = f"{search_data.get('title')} {search_data.get('text')}"
                            if re.search(r"验证码|安全验证|验证后继续|完成验证", search_text):
                                keep_page_open = True
                                page.bring_to_front()
                                raise PlatformBrowserError(
                                    "小红书要求完成安全验证，请处理后点击继续补齐",
                                    code="verification_required",
                                )
                            recovered_url = _best_xhs_search_result(
                                search_data.get("rows") or [], target_post_id, clean_title or title_hint,
                            )
                            if recovered_url:
                                break
                            mismatch_url, mismatch_post_id = _similar_xhs_search_result(
                                search_data.get("rows") or [], target_post_id, clean_title or title_hint,
                            )
                        if not recovered_url:
                            if mismatch_url:
                                raise PlatformBrowserError(
                                    "只找到了标题相似但 ID 不同的帖子，已拒绝归档",
                                    code="identity_mismatch",
                                    diagnostics={
                                        "target_post_id": target_post_id,
                                        "observed_post_id": mismatch_post_id,
                                        "candidate_url": _diagnostic_url(mismatch_url),
                                    },
                                )
                            raise PlatformBrowserError(
                                "原帖不存在、已删除或当前账号不可访问",
                                code="post_unavailable",
                                diagnostics={"target_post_id": target_post_id, "attempted_queries": queries},
                            )
                        access_url = recovered_url
                        self._remember_xhs_access_url(recovered_url)
                        page.goto(recovered_url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(900)

                    for pattern in (re.compile(r"^展开$"), re.compile(r"^全文$"), re.compile(r"展开正文")):
                        for item in page.get_by_text(pattern).all()[:3]:
                            try:
                                if item.is_visible():
                                    item.click(timeout=1200)
                            except Exception:
                                pass
                    page.wait_for_timeout(300)
                    data = page.evaluate(
                        r"""targetId => {
                          const visible = n => !!(n && (n.getClientRects().length || n.offsetParent !== null));
                          const firstVisible = selectors => {
                            for (const selector of selectors) for (const node of document.querySelectorAll(selector)) {
                              if (visible(node)) return node;
                            }
                            return null;
                          };
                          const stateRoots=[];
                          for (const key of ['__INITIAL_STATE__','__INITIAL_DATA__','__APOLLO_STATE__']) {
                            try { if (window[key] && typeof window[key]==='object') stateRoots.push(window[key]); } catch {}
                          }
                          const seen=new WeakSet(); let exact=null;
                          const walk=(value,depth=0)=>{
                            if (exact || !value || typeof value!=='object' || depth>12 || seen.has(value)) return;
                            seen.add(value);
                            let id='';
                            for (const key of ['noteId','note_id','postId','post_id','id']) {
                              try { if (typeof value[key]==='string') { id=value[key]; if(id===targetId) break; } } catch {}
                            }
                            if (id===targetId && (typeof value.desc==='string' || Array.isArray(value.imageList) || Array.isArray(value.images))) exact=value;
                            if (exact) return;
                            try { for (const child of Object.values(value)) walk(child,depth+1); } catch {}
                          };
                          stateRoots.forEach(root=>walk(root));
                          const rawFromState=exact && ['desc','noteDesc','description','content'].map(k=>exact[k]).find(v=>typeof v==='string');
                          const domBody=firstVisible(['[data-testid="note-content"]','[data-testid="post-content"]','#detail-desc','.note-content .desc','[class*="note-detail"] [class*="desc"]']);
                          const bodyRaw=typeof rawFromState==='string' ? rawFromState : (domBody ? (domBody.innerText ?? domBody.textContent ?? '') : null);
                          const bodySource=typeof rawFromState==='string' ? 'embedded_state' : (domBody ? 'expanded_dom' : 'unavailable');
                          const stateImages=exact && (exact.imageList || exact.image_list || exact.images);
                          const pickUrl=item=>{
                            if(typeof item==='string') return item;
                            if(!item||typeof item!=='object') return '';
                            for(const key of ['urlDefault','url_default','url','originalUrl','original_url','urlPre','url_pre']) if(typeof item[key]==='string'&&/^https?:/.test(item[key])) return item[key];
                            for(const key of ['urlList','url_list','urls']) if(Array.isArray(item[key])) { const found=item[key].find(v=>typeof v==='string'&&/^https?:/.test(v)); if(found)return found; }
                            return '';
                          };
                          let orderedBy='unknown'; let images=[]; let expected=null;
                          if(Array.isArray(stateImages) && stateImages.length){
                            images=stateImages.map((item,index)=>({url:pickUrl(item),ordinal:index+1,width:Number(item?.width||item?.imageWidth)||null,height:Number(item?.height||item?.imageHeight)||null,sourceKind:'embedded_state'})).filter(v=>v.url);
                            expected=stateImages.length; orderedBy='page_state';
                          } else {
                            const nodes=[...document.querySelectorAll('[class*="note-detail"] [class*="swiper"] img,[class*="note-detail"] img,article [class*="swiper"] img')].filter(visible);
                            const urls=new Set();
                            for(const node of nodes){
                              const candidates=[node.currentSrc,node.src,node.getAttribute('data-src'),node.getAttribute('data-original'),...(node.getAttribute('srcset')||'').split(',').map(v=>v.trim().split(/\s+/)[0])];
                              const selected=candidates.find(v=>typeof v==='string'&&/^https?:/.test(v));
                              if(!selected||urls.has(selected))continue;
                              const rect=node.getBoundingClientRect(); if(rect.width<180||rect.height<120)continue;
                              urls.add(selected); images.push({url:selected,ordinal:images.length+1,width:node.naturalWidth||null,height:node.naturalHeight||null,sourceKind:'ordered_carousel_dom'});
                            }
                            if(images.length){expected=images.length;orderedBy='carousel_dom';}
                          }
                          const canonical=document.querySelector('link[rel="canonical"]')?.href||location.href;
                          const finalMatch=canonical.match(/\/(?:explore|discovery\/item)\/([A-Za-z0-9]+)/) || location.href.match(/\/(?:explore|discovery\/item)\/([A-Za-z0-9]+)/);
                          const observedId=(exact && String(exact.noteId||exact.note_id||exact.postId||exact.post_id||exact.id||'')) || (finalMatch?.[1]||'');
                          const meta=s=>document.querySelector(s)?.getAttribute('content')||'';
                          const title=(exact && String(exact.title||exact.noteTitle||'')) || meta('meta[property="og:title"]') || firstVisible(['h1','[data-testid="note-title"]','#detail-title'])?.innerText || '';
                          const author=(exact && String(exact.user?.nickname||exact.author?.name||'')) || meta('meta[name="author"]') || firstVisible(['[data-testid="author-name"]','a[href*="/user/profile"] [class*="name"]'])?.innerText || '';
                          const publishedAt=(exact && String(exact.time||exact.publishTime||exact.publish_time||'')) || meta('meta[property="article:published_time"]') || '';
                          const coverUrl=meta('meta[property="og:image"]');
                          return {bodyRaw,bodySource,bodyVerified:bodySource==='embedded_state',images,expectedImageCount:expected,orderedBy,coverUrl,observedId,title,author,publishedAt,canonical,videoCount:document.querySelectorAll('video').length,pageTitle:document.title,finalUrl:location.href,visibleText:(document.body?.innerText||'').slice(0,5000)};
                        }""",
                        target_post_id,
                    )
                    diagnostic_text = urllib.parse.unquote(
                        f"{data.get('finalUrl')} {data.get('pageTitle')} {data.get('visibleText')}"
                    )
                    if re.search(r"验证码|安全验证|验证后继续|完成验证", diagnostic_text):
                        keep_page_open = True
                        page.bring_to_front()
                        raise PlatformBrowserError(
                            "小红书要求完成安全验证，请处理后点击继续补齐",
                            code="verification_required",
                        )
                    if re.search(r"IP存在风险|访问过于频繁|网络环境.*重试|error_code=300012", diagnostic_text):
                        raise PlatformBrowserError(
                            "小红书限制了当前网络或浏览器环境，请稍后重试",
                            code="platform_limited",
                        )
                    observed_post_id = str(data.get("observedId") or "")
                    if observed_post_id != target_post_id:
                        raise PlatformBrowserError(
                            "详情页帖子 ID 与目标帖子不一致，已拒绝归档",
                            code="identity_mismatch",
                            diagnostics={
                                "target_post_id": target_post_id,
                                "observed_post_id": observed_post_id,
                                "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            },
                        )

                    candidates: list[dict[str, Any]] = []
                    for item in data.get("images") or []:
                        if not isinstance(item, dict) or not str(item.get("url") or "").startswith(("http://", "https://")):
                            continue
                        candidates.append({
                            "role": "carousel",
                            "ordinal": int(item.get("ordinal") or len(candidates) + 1),
                            "source_url": str(item["url"]),
                            "source_kind": str(item.get("sourceKind") or "page_state"),
                            "ordered_by": str(data.get("orderedBy") or "unknown"),
                            "width": item.get("width"),
                            "height": item.get("height"),
                        })
                    cover_url = str(data.get("coverUrl") or "")
                    if cover_url.startswith(("http://", "https://")):
                        candidates.append({
                            "role": "cover", "ordinal": 1, "source_url": cover_url,
                            "source_kind": "og_image", "ordered_by": "cover_metadata",
                        })

                    response_by_url = {str(response.url): response for response in observed_responses}
                    assets: list[dict[str, Any]] = []
                    for candidate in candidates[:40]:
                        source_url = str(candidate["source_url"])
                        payload: bytes | None = None
                        declared = ""
                        http_status: int | None = None
                        source_kind = str(candidate["source_kind"])
                        error_code = ""
                        observed = response_by_url.get(source_url)
                        if observed is not None:
                            try:
                                http_status = int(observed.status)
                                declared = str(observed.headers.get("content-type") or "")
                                if http_status == 200:
                                    payload = observed.body()
                                    source_kind = f"{source_kind}+network_response"
                                else:
                                    error_code = f"HTTP_{http_status}"
                            except Exception:
                                payload = None
                        if payload is None:
                            try:
                                response = context.request.get(source_url, timeout=15000, fail_on_status_code=False)
                                http_status = int(response.status)
                                declared = str(response.headers.get("content-type") or declared)
                                if http_status == 200:
                                    body = response.body()
                                    if len(body) <= 25 * 1024 * 1024:
                                        payload = body
                                        source_kind = f"{source_kind}+authorized_context_request"
                                    else:
                                        error_code = "IMAGE_TOO_LARGE"
                                else:
                                    error_code = f"HTTP_{http_status}"
                            except Exception:
                                error_code = error_code or "RESPONSE_BODY_UNAVAILABLE"
                        assets.append({
                            **candidate,
                            "source_kind": source_kind,
                            "mime_declared": declared,
                            "http_status": http_status,
                            "bytes": payload,
                            "error_code": error_code if payload is None else "",
                            "rendition": "browser_returned",
                        })
                    result = {
                        "target_post_id": target_post_id,
                        "observed_post_id": observed_post_id,
                        "canonical_url": canonical_url,
                        "title": str(data.get("title") or ""),
                        "author": str(data.get("author") or ""),
                        "published_at": str(data.get("publishedAt") or "") or None,
                        "body_raw": data.get("bodyRaw"),
                        "body_source": str(data.get("bodySource") or "unavailable"),
                        "body_verified": bool(data.get("bodyVerified")),
                        "body_evidence": {
                            "source": str(data.get("bodySource") or "unavailable"),
                            "character_count": len(data.get("bodyRaw") or ""),
                            "same_post_id": True,
                        },
                        "expected_image_count": data.get("expectedImageCount"),
                        "video_count": int(data.get("videoCount") or 0),
                        "assets": assets,
                        "diagnostics": {
                            "page_title": str(data.get("pageTitle") or "")[:300],
                            "final_url": _diagnostic_url(str(data.get("finalUrl") or "")),
                            "ordered_by": str(data.get("orderedBy") or "unknown"),
                            "candidate_count": len(candidates),
                            "response_image_count": len(observed_responses),
                        },
                    }
                    page.close()
                    page = None
                    return result
            except PlatformBrowserError:
                raise
            except Exception as exc:
                raise PlatformBrowserError(
                    f"图文归档读取失败：{str(exc)[:180]}", code="archive_collect_failed"
                ) from exc
            finally:
                if page is not None and not keep_page_open:
                    try:
                        page.close()
                    except Exception:
                        pass


platform_browser_collector = PlatformBrowserCollector()
