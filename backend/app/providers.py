from __future__ import annotations

import json
import ipaddress
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .db import Database, database, json_dumps, utc_now
from .security import vault


LOCAL_PROXY_PROVIDER = "local_openai_proxy"
LOCAL_PROXY_PROTOCOL = "chat_completions"
DEFAULT_LOCAL_PROXY_URL = "http://127.0.0.1:8787/v1"
DEFAULT_LOCAL_PROXY_MODEL = "gpt-5.6-luna"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_PROTOCOL = "chat_completions"
DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"})
DEEPSEEK_VISION_MODELS = frozenset({"deepseek-v4-flash-vision-exp"})
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
MODEL_REQUEST_TIMEOUT_SECONDS = 720
ROLE_REASONING_EFFORT = {"researcher": "low", "writer": "low", "editor": "low"}


def normalize_local_proxy_url(value: str) -> str:
    """Accept only an OpenAI-compatible server running on this computer.

    The workbench intentionally does not accept remote endpoints or credentials.
    OAuth and any upstream authentication remain inside the local proxy process.
    """
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ProviderError("请填写本机 OpenAI 代理地址", category="model", code="missing_proxy_url")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise ProviderError("本机代理地址格式不正确", category="model", code="invalid_proxy_url") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError("本机代理地址必须以 http:// 或 https:// 开头", category="model", code="invalid_proxy_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderError("本机代理地址不能包含账号、参数或片段", category="model", code="invalid_proxy_url")
    host = parsed.hostname.rstrip(".").lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ProviderError("为保护资料，工作台只允许连接本机代理地址", category="model", code="non_local_proxy")
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ProviderError("请填写代理根地址或 /v1，不要填写具体接口路径", category="model", code="invalid_proxy_path")
    netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}/v1"


def normalize_deepseek_model(value: str) -> str:
    """Keep the settings UI on current, supported DeepSeek model identifiers."""
    model = str(value or "").strip()
    if model not in DEEPSEEK_MODELS:
        allowed = "、".join(sorted(DEEPSEEK_MODELS))
        raise ProviderError(
            f"请选择 DeepSeek 当前支持的模型：{allowed}",
            category="model",
            code="invalid_deepseek_model",
        )
    return model


def provider_label(provider: str | None) -> str:
    if provider == DEEPSEEK_PROVIDER:
        return "DeepSeek"
    if provider == LOCAL_PROXY_PROVIDER:
        return "本机代理"
    return "模型服务"


def model_profile_is_usable(profile: dict[str, Any] | None) -> bool:
    """Whether a stored profile can be selected for content generation.

    The actual secret stays in the system vault.  A profile only exposes an
    opaque reference (or a redacted `has_secret` flag in public responses).
    """
    if not profile or profile.get("protocol") != LOCAL_PROXY_PROTOCOL:
        return False
    if profile.get("verification_status") != "verified":
        return False
    provider = profile.get("provider")
    if provider == LOCAL_PROXY_PROVIDER:
        return bool(profile.get("base_url"))
    if provider == DEEPSEEK_PROVIDER:
        secret_ref = profile.get("secret_ref")
        if secret_ref:
            return bool(vault.get(secret_ref))
        return bool(profile.get("has_secret"))
    return False


def sanitize_provider_text(value: Any, limit: int = 500) -> str:
    """Keep upstream diagnostics useful without ever echoing secrets or image payloads."""
    text = str(value or "")
    text = re.sub(r"data:[^\s,;]+(?:;[^\s,]+)*;base64,[A-Za-z0-9+/=_-]+", "[图片数据已隐藏]", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{120,}(?![A-Za-z0-9+/=_-])", "[长编码内容已隐藏]", text)
    text = re.sub(r"sk-[A-Za-z0-9_.*-]+", "[密钥已隐藏]", text, flags=re.I)
    text = re.sub(r"Bearer\s+\S+", "Bearer [密钥已隐藏]", text, flags=re.I)
    text = re.sub(
        r"\b(?:api[ _-]?key|access[ _-]?token|authorization|secret)\s*(?:=|:)\s*(?:Bearer\s+)?[^\s,;]+",
        "[密钥已隐藏]",
        text,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip()[:limit]


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "provider",
        code: str = "provider_error",
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(sanitize_provider_text(message))
        self.category = category
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


def extract_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = clean.find("{"), clean.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(clean[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError as exc:
            raise ProviderError("模型返回内容无法按约定结构读取", code="invalid_json") from exc
    raise ProviderError("模型没有返回可读取的文案结果", code="invalid_json")


def _safe_provider_message(response: httpx.Response) -> str:
    try:
        value = response.json()
        message = str((value.get("error") or {}).get("message") or value.get("detail") or "")
    except (ValueError, AttributeError):
        message = ""
    return sanitize_provider_text(message, 240)


def _http_error(response: httpx.Response, provider_name: str = "本机代理") -> ProviderError:
    status = response.status_code
    detail = _safe_provider_message(response)
    suffix = f"：{detail}" if detail else ""
    if provider_name != "本机代理":
        if status == 401:
            return ProviderError(
                f"{provider_name} API Key 无效、已失效或拒绝当前请求（401）{suffix}",
                category="model", code="api_unauthorized", status_code=status,
            )
        if status == 403:
            return ProviderError(
                f"{provider_name} 当前没有调用这个模型的权限（403）{suffix}",
                category="model", code="permission_denied", status_code=status,
            )
        if status == 404:
            return ProviderError(
                f"{provider_name} 没有这个模型或不支持 Chat Completions（404）{suffix}",
                category="model", code="model_not_found", status_code=status,
            )
        if status == 429:
            return ProviderError(
                f"{provider_name} 暂时限流或额度不足（429）{suffix}",
                category="rate_limit", code="rate_limited", status_code=status, retryable=True,
            )
        if status >= 500:
            return ProviderError(
                f"{provider_name} 暂时不可用（{status}）{suffix}",
                category="network", code="upstream_unavailable", status_code=status, retryable=True,
            )
        return ProviderError(
            f"{provider_name} 请求未成功（{status}）{suffix}",
            category="provider", code="request_failed", status_code=status,
        )
    if status == 401:
        return ProviderError("本机代理尚未完成 OAuth 登录或拒绝当前请求（401）", category="model", code="proxy_unauthorized", status_code=status)
    if status == 403:
        return ProviderError("本机代理当前没有调用这个模型的权限（403）", category="model", code="permission_denied", status_code=status)
    if status == 404:
        return ProviderError(f"本机代理没有这个模型或不支持 Chat Completions（404）{suffix}", category="model", code="model_not_found", status_code=status)
    if status == 429:
        return ProviderError(f"本机代理暂时限流或上游额度不足（429）{suffix}", category="rate_limit", code="rate_limited", status_code=status, retryable=True)
    if status >= 500:
        return ProviderError(f"本机代理或其上游暂时不可用（{status}）{suffix}", category="network", code="upstream_unavailable", status_code=status, retryable=True)
    return ProviderError(f"本机代理请求未成功（{status}）{suffix}", category="provider", code="request_failed", status_code=status)


def _output_text(raw: dict[str, Any]) -> str:
    text = str(raw.get("output_text") or "")
    if text:
        return text
    pieces: list[str] = []
    for item in raw.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                pieces.append(str(part.get("text") or ""))
    return "".join(pieces)


def _chat_output_text(raw: dict[str, Any]) -> str:
    choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderError("本机代理没有返回 Chat Completions 结果", code="invalid_response")
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                pieces.append(str(item.get("text") or ""))
        if "".join(pieces).strip():
            return "".join(pieces)
    raise ProviderError("本机代理没有返回可读取的文案结果", code="invalid_response")


def _web_sources(raw: dict[str, Any]) -> list[dict[str, Any]]:
    text = _output_text(raw)
    found: dict[str, dict[str, Any]] = {}
    for item in raw.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            for source in action.get("sources", []):
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "")
                if url:
                    found[url] = {
                        "title": str(source.get("title") or url),
                        "url": url,
                        "excerpt": str(source.get("snippet") or source.get("description") or "")[:1000],
                        "published_at": source.get("published_at") or source.get("published_date"),
                        "credibility": 0.72,
                        "search_mode": "openai_web_search",
                    }
        for part in item.get("content", []):
            if not isinstance(part, dict):
                continue
            for annotation in part.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                citation = annotation.get("url_citation") if isinstance(annotation.get("url_citation"), dict) else annotation
                url = str(citation.get("url") or "")
                if not url:
                    continue
                start = int(citation.get("start_index") or 0)
                end = int(citation.get("end_index") or start)
                excerpt = text[max(0, start - 80) : min(len(text), end + 140)].strip()
                found[url] = {
                    "title": str(citation.get("title") or found.get(url, {}).get("title") or url),
                    "url": url,
                    "excerpt": excerpt or found.get(url, {}).get("excerpt") or "OpenAI 联网搜索引用来源",
                    "published_at": found.get(url, {}).get("published_at"),
                    "credibility": 0.72,
                    "search_mode": "openai_web_search",
                }
    return list(found.values())


@dataclass
class ModelResult:
    data: dict[str, Any]
    profile_id: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int
    web_sources: list[dict[str, Any]] = field(default_factory=list)


class ModelGateway:
    def __init__(self, db: Database = database):
        self.db = db

    def resolve_profile(self, workspace_id: str, role_key: str) -> dict[str, Any] | None:
        role = self.db.one(
            "SELECT model_profile_id FROM role_configs WHERE workspace_id=? AND role_key=?",
            (workspace_id, role_key),
        )
        if role and role.get("model_profile_id"):
            profile = self.db.one("SELECT * FROM model_profiles WHERE id=?", (role["model_profile_id"],))
            if profile:
                return profile
        return self.db.one(
            """SELECT * FROM model_profiles
            WHERE (workspace_id=? OR workspace_id IS NULL) AND is_default=1
            ORDER BY CASE WHEN workspace_id=? THEN 0 ELSE 1 END LIMIT 1""",
            (workspace_id, workspace_id),
        )

    @staticmethod
    def _request(
        base_url: str,
        body: dict[str, Any],
        timeout: float = MODEL_REQUEST_TIMEOUT_SECONDS,
        *,
        api_key: str | None = None,
        provider_name: str = "本机代理",
    ) -> dict[str, Any]:
        """Run a previously validated OpenAI-compatible Chat Completions request.

        Callers must normalize their own provider URL first.  This keeps the
        local proxy loopback-only while DeepSeek is pinned to its official
        HTTPS origin; neither route accepts a user-controlled endpoint here.
        """
        endpoint = f"{str(base_url).rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    endpoint,
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"连接{provider_name}超时，请检查连接后重试", category="network", code="timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接{provider_name}，请检查网络或服务状态", category="network", code="connection_error", retryable=True) from exc
        if response.is_redirect:
            raise ProviderError(f"{provider_name} 接口返回了异常重定向", category="network", code="redirect")
        if not response.is_success:
            raise _http_error(response, provider_name)
        try:
            raw = response.json()
        except ValueError as exc:
            raise ProviderError(f"{provider_name} 返回了无法读取的数据", code="invalid_response") from exc
        if not isinstance(raw, dict):
            raise ProviderError(f"{provider_name} 返回了无法读取的数据", code="invalid_response")
        return raw

    @staticmethod
    def _chat_body(
        *,
        model: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_tokens: int,
        schema: dict[str, Any] | None,
        web_search: bool,
        image_inputs: list[str] | None = None,
        reasoning_effort: str = "medium",
        thinking: str | None = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        output_contract = ""
        if schema:
            output_contract = (
                "\n\n输出要求：只输出一个可解析的 JSON 对象，不要 Markdown、解释或代码围栏。"
                "JSON 必须符合下面的结构约束：\n"
                + json_dumps(schema)
            )
        if web_search:
            output_contract += "\n\n联网来源由工作台的 RSS 与资料库处理；不要声称自己已联网检索。"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt + output_contract},
        ]
        if image_inputs:
            content: list[dict[str, Any]] = [{"type": "text", "text": json_dumps(payload)}]
            content.extend(
                {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}}
                for image_url in image_inputs
            )
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": json_dumps(payload)})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": reasoning_effort,
            "stream": False,
        }
        if thinking in {"enabled", "disabled"}:
            body["thinking"] = {"type": thinking}
        if json_mode and schema:
            body["response_format"] = {"type": "json_object"}
        return body

    def profile_is_usable(self, workspace_id: str, role_key: str = "writer") -> bool:
        return model_profile_is_usable(self.resolve_profile(workspace_id, role_key))

    def generate_json(
        self,
        workspace_id: str,
        task_id: str | None,
        role_key: str,
        system_prompt: str,
        payload: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
        web_search: bool = False,
        image_inputs: list[str] | None = None,
    ) -> ModelResult:
        profile = self.resolve_profile(workspace_id, role_key)
        if not profile:
            raise ProviderError("还没有连接可用模型", category="model", code="not_configured")
        provider = str(profile.get("provider") or "")
        name = provider_label(provider)
        if provider not in {LOCAL_PROXY_PROVIDER, DEEPSEEK_PROVIDER} or profile.get("protocol") != LOCAL_PROXY_PROTOCOL:
            raise ProviderError("请先在设置中连接可用模型", category="model", code="unsupported_provider")
        if profile.get("verification_status") != "verified":
            raise ProviderError(f"{name} 尚未通过连接测试，请先到设置中测试并保存", category="model", code="not_verified")
        model = str(profile.get("model") or "")
        api_key: str | None = None
        if provider == LOCAL_PROXY_PROVIDER:
            base_url = normalize_local_proxy_url(str(profile.get("base_url") or ""))
        else:
            model = normalize_deepseek_model(model)
            api_key = vault.get(profile.get("secret_ref"))
            if not api_key:
                raise ProviderError(
                    "DeepSeek API Key 未在本机钥匙串中找到，请在设置中重新填写并测试",
                    category="model", code="missing_deepseek_key",
                )
            base_url = DEEPSEEK_API_BASE_URL
            if image_inputs and model not in DEEPSEEK_VISION_MODELS:
                raise ProviderError(
                    "当前 DeepSeek 模型只处理文字；生活案例图片请切换到 DeepSeek V4 Flash Vision（实验）或本机代理",
                    category="model", code="vision_model_required",
                )
        started = time.perf_counter()
        try:
            body = self._chat_body(
                model=model,
                system_prompt=system_prompt,
                payload=payload,
                max_tokens=int(profile.get("max_tokens") or 6000),
                schema=schema,
                web_search=web_search,
                image_inputs=image_inputs,
                reasoning_effort=ROLE_REASONING_EFFORT.get(role_key, "medium"),
                # Flash is the fast default.  Pro can keep its deeper mode
                # when the user explicitly chooses it in settings.
                thinking=("enabled" if model == "deepseek-v4-pro" else "disabled") if provider == DEEPSEEK_PROVIDER else None,
                json_mode=provider == DEEPSEEK_PROVIDER,
            )
            if provider == DEEPSEEK_PROVIDER:
                raw = self._request(base_url, body, api_key=api_key, provider_name="DeepSeek")
            else:
                # Keep the local call shape stable for the OAuth proxy and
                # existing integrations that patch this method in tests.
                raw = self._request(base_url, body)
        except ProviderError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            self._log_usage(workspace_id, task_id, role_key, profile["id"], None, None, None, f"ERROR:{exc.code}", latency)
            if exc.category == "model" and exc.code not in {"vision_model_required", "missing_deepseek_key"}:
                self.db.execute(
                    "UPDATE model_profiles SET verification_status='error',last_error_code=?,last_error_message=?,updated_at=? WHERE id=?",
                    (exc.code, str(exc)[:300], utc_now(), profile["id"]),
                )
            raise
        latency = int((time.perf_counter() - started) * 1000)
        usage = raw.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if total_tokens is None and isinstance(input_tokens, int) and isinstance(output_tokens, int):
            total_tokens = input_tokens + output_tokens
        self._log_usage(workspace_id, task_id, role_key, profile["id"], input_tokens, output_tokens, total_tokens, "OK", latency)
        return ModelResult(
            extract_json(_chat_output_text(raw)), profile["id"], input_tokens, output_tokens, total_tokens, latency, []
        )

    def test_proxy(self, base_url: str, model: str) -> dict[str, Any]:
        clean_url = normalize_local_proxy_url(base_url)
        clean_model = str(model or "").strip()
        if not clean_model:
            raise ProviderError("请填写要使用的模型名称", category="model", code="missing_model")
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        started = time.perf_counter()
        raw = self._request(
            clean_url,
            self._chat_body(
                model=clean_model, system_prompt="只按结构返回连接测试结果。", payload={"request": "返回 ok=true"},
                max_tokens=256, schema=schema, web_search=False, reasoning_effort="low",
            ),
            timeout=180,
        )
        value = extract_json(_chat_output_text(raw))
        if value.get("ok") is not True:
            raise ProviderError("本机代理测试没有返回预期结果", category="model", code="invalid_test_response")
        model_check = {
            "status": "verified", "model": clean_model, "message": "本机代理模型生成正常",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        rss = SearchGateway(self.db).rss_search("OpenAI API", 2)
        search_check = {
            "status": "fallback" if rss else "unverified",
            "mode": "multi_source" if rss else "unavailable",
            "message": "热点研究将使用多个公开搜索源与资料库" if rss else "模型已连接；公开搜索源尚未检测到可用结果",
            "source_count": len(rss),
        }
        return {"model": model_check, "search": search_check, "checked_at": utc_now()}

    def test_deepseek(self, api_key: str, model: str) -> dict[str, Any]:
        """Verify a DeepSeek key before it is stored or made active."""
        clean_key = str(api_key or "").strip()
        if not clean_key:
            raise ProviderError("请填写 DeepSeek API Key", category="model", code="missing_deepseek_key")
        clean_model = normalize_deepseek_model(model)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        started = time.perf_counter()
        raw = self._request(
            DEEPSEEK_API_BASE_URL,
            self._chat_body(
                model=clean_model,
                system_prompt="只按结构返回连接测试结果。",
                payload={"request": "返回 ok=true"},
                max_tokens=256,
                schema=schema,
                web_search=False,
                reasoning_effort="low",
                thinking="enabled" if clean_model == "deepseek-v4-pro" else "disabled",
                json_mode=True,
            ),
            timeout=180,
            api_key=clean_key,
            provider_name="DeepSeek",
        )
        value = extract_json(_chat_output_text(raw))
        if value.get("ok") is not True:
            raise ProviderError("DeepSeek 测试没有返回预期结果", category="model", code="invalid_test_response")
        rss = SearchGateway(self.db).rss_search("AI 文案", 2)
        return {
            "model": {
                "status": "verified",
                "model": clean_model,
                "message": "DeepSeek 模型生成正常",
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
            "search": {
                "status": "fallback" if rss else "unverified",
                "mode": "multi_source" if rss else "unavailable",
                "message": "热点研究将使用多个公开搜索源与资料库" if rss else "模型已连接；公开搜索源尚未检测到可用结果",
                "source_count": len(rss),
            },
            "checked_at": utc_now(),
        }

    def embeddings(self, workspace_id: str, texts: list[str], model: str = "text-embedding-3-small") -> list[list[float]]:
        raise ProviderError(
            "当前本机代理接入只使用文本生成；知识库会继续使用本地全文检索",
            category="model",
            code="embeddings_unavailable",
        )

    def _log_usage(
        self, workspace_id: str, task_id: str | None, role_key: str, profile_id: str,
        input_tokens: int | None, output_tokens: int | None, total_tokens: int | None,
        status: str, latency_ms: int,
    ) -> None:
        self.db.execute(
            """INSERT INTO usage_calls
            (workspace_id,task_id,role_key,model_profile_id,input_tokens,output_tokens,total_tokens,status,latency_ms,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (workspace_id, task_id, role_key, profile_id, input_tokens, output_tokens, total_tokens, status, latency_ms, utc_now()),
        )


class SearchGateway:
    """Several public search sources used alongside the local model proxy."""

    def __init__(self, db: Database = database):
        self.db = db

    def search(self, workspace_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return self.rss_search(query, limit)

    def rss_search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        feeds = [
            "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "mkt": "zh-CN"}),
            "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "mkt": "zh-CN"}),
        ]

        def read_rss(url: str) -> list[dict[str, Any]]:
            try:
                response = httpx.get(
                    url, timeout=8, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 CopyWorkbench/2.0"},
                )
                response.raise_for_status()
                root = ET.fromstring(response.text)
            except (httpx.HTTPError, ET.ParseError):
                return []
            rows: list[dict[str, Any]] = []
            for item in root.findall(".//item"):
                item_url = str(item.findtext("link") or "").strip()
                title = str(item.findtext("title") or "").strip()
                if not item_url or not title:
                    continue
                rows.append({
                    "title": title,
                    "url": item_url,
                    "excerpt": re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip(),
                    "published_at": item.findtext("pubDate"),
                    "credibility": 0.55,
                    "search_mode": "bing_rss",
                })
            return rows

        def read_sogou() -> list[dict[str, Any]]:
            try:
                response = httpx.get(
                    "https://www.sogou.com/web", params={"query": query}, timeout=8,
                    follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 CopyWorkbench/2.0"},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                return []
            soup = BeautifulSoup(response.text, "html.parser")
            rows: list[dict[str, Any]] = []
            for heading in soup.select("h3"):
                anchor = heading.find("a", href=True)
                if not anchor:
                    continue
                title = anchor.get_text(" ", strip=True)
                href = urllib.parse.urljoin("https://www.sogou.com", str(anchor.get("href") or ""))
                if not title or not href:
                    continue
                container = heading.parent
                excerpt = container.get_text(" ", strip=True) if container else title
                date_match = re.search(r"20\d{2}-\d{2}-\d{2}", excerpt)
                rows.append({
                    "title": title,
                    "url": href,
                    "excerpt": excerpt[:1000],
                    "published_at": date_match.group(0) if date_match else None,
                    "credibility": 0.55,
                    "search_mode": "sogou_web",
                })
                if len(rows) >= limit:
                    break
            return rows

        readers = [read_sogou, *(lambda feed=feed: read_rss(feed) for feed in feeds)]
        with ThreadPoolExecutor(max_workers=len(readers)) as executor:
            batches = list(executor.map(lambda reader: reader(), readers))
        found: dict[str, dict[str, Any]] = {}
        for rows in batches:
            for row in rows:
                item_url = row["url"]
                if item_url not in found:
                    found[item_url] = row
                if len(found) >= limit:
                    return list(found.values())
        return list(found.values())


model_gateway = ModelGateway()
search_gateway = SearchGateway()
