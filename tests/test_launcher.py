from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "launch_workbench.py"
BATCH_PATH = Path(__file__).resolve().parents[1] / "启动文案工作台.bat"
MAC_LAUNCH_PATH = Path(__file__).resolve().parents[1] / "启动 AI 文案工作台.command"
SPEC = importlib.util.spec_from_file_location("launch_workbench", MODULE_PATH)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.fixture(autouse=True)
def local_proxy_is_ready(monkeypatch):
    """Keep existing backend-launcher tests isolated from the separate proxy lifecycle."""
    monkeypatch.setattr(launcher, "ensure_local_proxy", lambda _build_id: True)


def test_launcher_reuses_running_server(monkeypatch):
    opened: list[bool] = []
    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(
        launcher,
        "read_health",
        lambda: {"ok": True, "build_id": "build-current", "process_id": 4321},
    )
    monkeypatch.setattr(launcher, "open_app", lambda: opened.append(True))
    monkeypatch.setattr(launcher, "start_server", lambda: (_ for _ in ()).throw(AssertionError("不应重复启动")))
    monkeypatch.setattr(
        launcher,
        "terminate_workbench_process",
        lambda _pid: (_ for _ in ()).throw(AssertionError("最新版进程不应被终止")),
    )

    assert launcher.main() == 0
    assert opened == [True]


def test_launcher_waits_for_new_server_before_opening(monkeypatch):
    states = iter([None, None, {"ok": True, "build_id": "build-current", "process_id": 4567}])
    opened: list[bool] = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(launcher, "read_health", lambda: next(states))
    monkeypatch.setattr(launcher, "start_server", lambda: process)
    monkeypatch.setattr(launcher, "open_app", lambda: opened.append(True))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    assert launcher.main() == 0
    assert opened == [True]


def test_stale_managed_workbench_is_stopped_then_restarted(monkeypatch):
    states = iter([
        {"ok": True, "build_id": "build-old", "process_id": 2468},
        {"ok": True, "build_id": "build-current", "process_id": 9753},
    ])
    stopped: list[int] = []
    started: list[bool] = []
    opened: list[bool] = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(launcher, "read_health", lambda: next(states))
    monkeypatch.setattr(launcher, "is_managed_workbench_process", lambda pid, build: (pid, build) == (2468, "build-old"))
    monkeypatch.setattr(launcher, "terminate_workbench_process", lambda pid: stopped.append(pid) is None or True)
    monkeypatch.setattr(launcher, "wait_for_port_release", lambda: True)
    monkeypatch.setattr(launcher, "start_server", lambda: (started.append(True), process)[1])
    monkeypatch.setattr(launcher, "open_app", lambda: opened.append(True))

    assert launcher.main() == 0
    assert stopped == [2468]
    assert started == [True]
    assert opened == [True]


@pytest.mark.parametrize(
    "health",
    [
        {"ok": True, "process_id": 2468},
        {"ok": True, "build_id": "build-old"},
        {"ok": True, "build_id": "build-old", "process_id": 0},
        {"ok": True, "build_id": "build-old", "process_id": True},
    ],
)
def test_missing_or_invalid_health_identity_never_kills_process(monkeypatch, health, capsys):
    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(launcher, "read_health", lambda: health)
    monkeypatch.setattr(
        launcher,
        "terminate_workbench_process",
        lambda _pid: (_ for _ in ()).throw(AssertionError("身份信息不足时绝不能终止进程")),
    )
    monkeypatch.setattr(
        launcher,
        "start_server",
        lambda: (_ for _ in ()).throw(AssertionError("端口仍被旧服务占用时不能启动新版")),
    )

    assert launcher.main() == 2
    assert "手动关闭" in capsys.readouterr().err


def test_stale_unverified_process_is_not_killed(monkeypatch, capsys):
    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(
        launcher,
        "read_health",
        lambda: {"ok": True, "build_id": "build-old", "process_id": 2468},
    )
    monkeypatch.setattr(launcher, "is_managed_workbench_process", lambda _pid, _build: False)
    monkeypatch.setattr(
        launcher,
        "terminate_workbench_process",
        lambda _pid: (_ for _ in ()).throw(AssertionError("未确认进程归属时绝不能终止")),
    )

    assert launcher.main() == 2
    assert "无法安全确认旧进程" in capsys.readouterr().err


def test_managed_process_check_requires_same_health_port_and_python_command(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "read_health",
        lambda: {"ok": True, "build_id": "build-old", "process_id": 2468},
    )
    monkeypatch.setattr(launcher, "listening_process_id", lambda: 2468)
    monkeypatch.setattr(
        launcher,
        "_workbench_process_command",
        lambda _pid: (r"C:\Python313\python.exe", r'"C:\Python313\python.exe" -m backend.app.main'),
    )
    assert launcher.is_managed_workbench_process(2468, "build-old") is True

    monkeypatch.setattr(launcher, "listening_process_id", lambda: 9999)
    assert launcher.is_managed_workbench_process(2468, "build-old") is False


@pytest.mark.skipif(os.name != "nt", reason="Windows process groups only")
def test_windows_shutdown_prefers_targeted_ctrl_break(monkeypatch):
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(launcher.os, "kill", lambda pid, event: signals.append((pid, event)))
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CTRL_BREAK 成功后不应调用 taskkill")),
    )

    assert launcher.terminate_workbench_process(2468) is True
    assert signals == [(2468, launcher.signal.CTRL_BREAK_EVENT)]


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill fallback only")
def test_windows_ctrl_break_system_error_falls_back_to_non_forced_taskkill(monkeypatch):
    calls: list[list[str]] = []

    def broken_ctrl_break(_pid, _event):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.os, "kill", broken_ctrl_break)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)

    assert launcher.terminate_workbench_process(2776) is True
    assert calls == [["taskkill", "/PID", "2776"]]
    assert "/F" not in calls[0]
    assert "/T" not in calls[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows shutdown race only")
def test_main_restarts_when_ctrl_break_raises_but_port_is_already_released(monkeypatch):
    states = iter([
        {"ok": True, "build_id": "build-old", "process_id": 31208},
        {"ok": True, "build_id": "build-current", "process_id": 31337},
    ])
    taskkill_calls: list[list[str]] = []
    started: list[bool] = []
    opened: list[bool] = []
    process = SimpleNamespace(poll=lambda: None)

    def ctrl_break_exited_then_raised(_pid, _event):
        raise SystemError("<built-in function kill> returned a result with an exception set")

    def missing_process(command, **_kwargs):
        taskkill_calls.append(command)
        return SimpleNamespace(returncode=128)

    monkeypatch.setattr(launcher, "current_build_id", lambda: "build-current")
    monkeypatch.setattr(launcher, "read_health", lambda: next(states))
    monkeypatch.setattr(launcher, "is_managed_workbench_process", lambda pid, build: (pid, build) == (31208, "build-old"))
    monkeypatch.setattr(launcher.os, "kill", ctrl_break_exited_then_raised)
    monkeypatch.setattr(launcher.subprocess, "run", missing_process)
    monkeypatch.setattr(launcher, "wait_for_port_release", lambda: True)
    monkeypatch.setattr(launcher, "start_server", lambda: (started.append(True), process)[1])
    monkeypatch.setattr(launcher, "open_app", lambda: opened.append(True))

    assert launcher.main() == 0
    assert taskkill_calls == [["taskkill", "/PID", "31208"]]
    assert started == [True]
    assert opened == [True]


def test_frontend_needs_build_when_bundle_is_missing(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "App.tsx").write_text("export default null", encoding="utf-8")

    assert launcher.frontend_needs_build(frontend) is True


def test_frontend_needs_build_when_source_is_newer(tmp_path: Path):
    frontend = tmp_path / "frontend"
    source = frontend / "src" / "App.tsx"
    bundle = frontend / "dist" / "index.html"
    source.parent.mkdir(parents=True)
    bundle.parent.mkdir(parents=True)
    source.write_text("old", encoding="utf-8")
    bundle.write_text("bundle", encoding="utf-8")
    newer = bundle.stat().st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(newer, newer))

    assert launcher.frontend_needs_build(frontend) is True


def test_frontend_bundle_is_current_when_inputs_are_older(tmp_path: Path):
    frontend = tmp_path / "frontend"
    source = frontend / "src" / "App.tsx"
    manifest = frontend / "package.json"
    bundle = frontend / "dist" / "index.html"
    source.parent.mkdir(parents=True)
    bundle.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    bundle.write_text("bundle", encoding="utf-8")
    newer = max(source.stat().st_mtime_ns, manifest.stat().st_mtime_ns) + 2_000_000_000
    os.utime(bundle, ns=(newer, newer))

    assert launcher.frontend_needs_build(frontend) is False


def test_batch_checks_life_case_image_dependencies_before_launching():
    content = BATCH_PATH.read_text(encoding="utf-8")

    assert "PIL" in content
    assert "pillow_heif" in content
    assert "multipart" in content
    assert "pip install -r requirements.txt" in content
    assert "--frontend-build-required" in content


def test_mac_launcher_bootstraps_a_portable_runtime():
    content = MAC_LAUNCH_PATH.read_text(encoding="utf-8")

    assert "python3.10" in content
    assert '-m venv "$VENV_DIR"' in content
    assert 'pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"' in content
    assert "WORKBENCH_SETUP_ONLY" in content
