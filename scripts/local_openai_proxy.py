#!/usr/bin/env python3
"""Loopback-only OpenAI-compatible proxy backed by the local Codex OAuth session.

The proxy deliberately owns no credential material.  It delegates authentication to
the installed ``codex`` CLI, which opens its normal login flow when required.  It is
only intended for local applications on this computer.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "low"
ALLOWED_REASONING_EFFORTS = {"low", "medium", "high", "max"}
HOST = "127.0.0.1"
PORT = int(os.getenv("WORKBENCH_LOCAL_PROXY_PORT", "8787"))
MAX_BODY_BYTES = 24 * 1024 * 1024
MAX_MODEL_WAIT_SECONDS = 720

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.build_info import calculate_build_id


BUILD_ID = calculate_build_id(PROJECT_ROOT)


def sanitize_message(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"Bearer\s+\S+", "Bearer [已隐藏]", text, flags=re.I)
    text = re.sub(r"sk-[A-Za-z0-9_.*-]+", "[已隐藏]", text, flags=re.I)
    text = re.sub(r"(?:access|refresh|id)[_-]?token[=:]\s*\S+", "token=[已隐藏]", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:320]


def request_text(messages: list[dict[str, Any]]) -> tuple[str, list[Path]]:
    """Convert standard chat messages to one Codex prompt and temporary image files."""
    rendered: list[dict[str, Any]] = []
    image_paths: list[Path] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, str):
            rendered.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("messages 的 content 必须是文本或内容数组")
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            if kind in {"text", "input_text"}:
                text_parts.append(str(part.get("text") or ""))
                continue
            if kind not in {"image_url", "input_image"}:
                continue
            image = part.get("image_url")
            url = str(image.get("url") if isinstance(image, dict) else image or "")
            if not url.startswith("data:image/") or ";base64," not in url:
                raise ValueError("本机代理仅接收工作台上传的图片，不读取远程图片 URL")
            header, encoded = url.split(",", 1)
            mime = header.split(";", 1)[0].removeprefix("data:image/").lower()
            suffix = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}.get(mime, ".img")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("图片数据无法读取") from exc
            if not data or len(data) > 12 * 1024 * 1024:
                raise ValueError("单张图片必须小于 12 MB")
            handle = tempfile.NamedTemporaryFile(prefix="workbench-image-", suffix=suffix, delete=False)
            try:
                handle.write(data)
            finally:
                handle.close()
            image_paths.append(Path(handle.name))
        rendered.append({"role": role, "content": "\n".join(text_parts)})
    if not rendered:
        raise ValueError("messages 不能为空")
    prompt = (
        "你是本机 OpenAI-compatible API 的文本生成引擎。严格按消息顺序回答最后一个用户请求。"
        "不要执行命令、读写文件、浏览网页或解释代理实现；只输出应答正文。\n\n"
        "以下是 OpenAI Chat Completions 消息：\n"
        + json.dumps(rendered, ensure_ascii=False)
    )
    return prompt, image_paths


def run_codex(
    messages: list[dict[str, Any]],
    model: str,
    timeout_seconds: int,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> tuple[str, int]:
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("未找到 codex 命令；请安装并登录 Codex 后再试")
    prompt, images = request_text(messages)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="workbench-codex-") as directory:
        root = Path(directory)
        output_path = root / "result.txt"
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--cd",
            str(root),
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--model",
            model or DEFAULT_MODEL,
            "--output-last-message",
            str(output_path),
        ]
        for image in images:
            command.extend(["--image", str(image)])
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(20, min(timeout_seconds, MAX_MODEL_WAIT_SECONDS)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Codex 本机代理请求超时") from exc
        finally:
            for image in images:
                image.unlink(missing_ok=True)
        if completed.returncode != 0:
            diagnostic = sanitize_message(completed.stderr)
            if "login" in diagnostic.lower() or "auth" in diagnostic.lower():
                raise PermissionError("Codex 尚未完成 OAuth 登录，请先在本机登录 Codex")
            raise RuntimeError(f"Codex 本机代理请求失败：{diagnostic or '未知错误'}")
        if not output_path.is_file():
            raise RuntimeError("Codex 没有返回可读取的结果")
        answer = output_path.read_text(encoding="utf-8", errors="replace").strip()
        if not answer:
            raise RuntimeError("Codex 返回了空结果")
    return answer, int((time.monotonic() - started) * 1000)


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "WorkbenchLocalProxy/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, code: str) -> None:
        self._json(status, {"error": {"message": sanitize_message(message), "type": "local_proxy_error", "code": code}})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {
                "ok": True,
                "service": "codex-oauth-local-proxy",
                "build_id": BUILD_ID,
                "runtime_build_id": BUILD_ID,
                "pid": os.getpid(),
                "default_model": DEFAULT_MODEL,
                "reasoning_effort": "adaptive",
                "default_reasoning_effort": DEFAULT_REASONING_EFFORT,
            })
            return
        if self.path == "/v1/models":
            self._json(HTTPStatus.OK, {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "local-codex-oauth"}]})
            return
        self._error(HTTPStatus.NOT_FOUND, "接口不存在", "not_found")

    def do_POST(self) -> None:
        if self.path not in {"/v1/chat/completions", "/chat/completions"}:
            self._error(HTTPStatus.NOT_FOUND, "接口不存在", "not_found")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.BAD_REQUEST, "请求大小无效", "invalid_request")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "请求 JSON 无法读取", "invalid_json")
            return
        if body.get("stream"):
            self._error(HTTPStatus.BAD_REQUEST, "本机代理当前不支持流式响应", "stream_unsupported")
            return
        messages = body.get("messages")
        if not isinstance(messages, list):
            self._error(HTTPStatus.BAD_REQUEST, "messages 必须是数组", "invalid_messages")
            return
        model = str(body.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        reasoning_effort = str(body.get("reasoning_effort") or DEFAULT_REASONING_EFFORT).strip().lower()
        if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            self._error(HTTPStatus.BAD_REQUEST, "reasoning_effort 必须是 low、medium、high 或 max", "invalid_reasoning_effort")
            return
        # A complete multi-day package can still take several minutes. Keep
        # the local HTTP request open instead of reporting a healthy proxy as
        # unavailable halfway through generation.
        timeout = MAX_MODEL_WAIT_SECONDS
        try:
            answer, latency_ms = run_codex(messages, model, timeout, reasoning_effort)
        except PermissionError as exc:
            self._error(HTTPStatus.UNAUTHORIZED, str(exc), "oauth_login_required")
            return
        except TimeoutError as exc:
            self._error(HTTPStatus.GATEWAY_TIMEOUT, str(exc), "timeout")
            return
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "invalid_request")
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.BAD_GATEWAY, str(exc), "codex_failed")
            return
        estimated_prompt = max(1, sum(len(str(item.get("content") or "")) for item in messages) // 4)
        estimated_completion = max(1, len(answer) // 4)
        self._json(HTTPStatus.OK, {
            "id": f"chatcmpl-local-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": estimated_prompt, "completion_tokens": estimated_completion, "total_tokens": estimated_prompt + estimated_completion},
            "system_fingerprint": f"latency-{latency_ms}ms",
        })


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
