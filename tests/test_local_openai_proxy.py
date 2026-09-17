from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_openai_proxy.py"
SPEC = importlib.util.spec_from_file_location("local_openai_proxy", MODULE_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def test_request_text_keeps_chat_messages_in_the_codex_prompt():
    prompt, images = proxy.request_text([
        {"role": "system", "content": "只返回 JSON"},
        {"role": "user", "content": "请返回连接结果"},
    ])

    assert '"role": "system"' in prompt
    assert '"role": "user"' in prompt
    assert "请返回连接结果" in prompt
    assert images == []


def test_request_text_rejects_remote_image_urls():
    with pytest.raises(ValueError, match="远程图片"):
        proxy.request_text([
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]},
        ])


def test_proxy_error_sanitizer_never_returns_bearer_or_key_values():
    message = proxy.sanitize_message("Bearer abcdefghijklmnopqrstuvwxyz sk-secret-value")
    assert "Bearer abcdefgh" not in message
    assert "sk-secret-value" not in message


def test_codex_request_uses_luna_with_default_low_reasoning(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    monkeypatch.setattr(proxy.shutil, "which", lambda _name: "/usr/local/bin/codex")

    def fake_run(command: list[str], **_kwargs):
        calls.append(command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"ok":true}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    result, _latency = proxy.run_codex([{"role": "user", "content": "返回 JSON"}], "gpt-5.6-luna", 30)

    assert result == '{"ok":true}'
    assert calls[0][calls[0].index("--model") + 1] == "gpt-5.6-luna"
    assert calls[0][calls[0].index("--config") + 1] == 'model_reasoning_effort="low"'


def test_codex_request_allows_luna_max_to_complete_long_generation(monkeypatch: pytest.MonkeyPatch):
    observed_timeout: list[int] = []

    monkeypatch.setattr(proxy.shutil, "which", lambda _name: "/usr/local/bin/codex")

    def fake_run(command: list[str], **kwargs):
        observed_timeout.append(kwargs["timeout"])
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"ok":true}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    proxy.run_codex([{"role": "user", "content": "生成完整内容"}], "gpt-5.6-luna", 999, "low")

    assert observed_timeout == [proxy.MAX_MODEL_WAIT_SECONDS]
