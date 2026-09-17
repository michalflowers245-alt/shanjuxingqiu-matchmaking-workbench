from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path, PureWindowsPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / ".workbench"
HEALTH_URL = "http://127.0.0.1:5177/api/health"
APP_URL = "http://127.0.0.1:5177/"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
LOCAL_PROXY_PORT = 8787
LOCAL_PROXY_HEALTH_URL = f"http://127.0.0.1:{LOCAL_PROXY_PORT}/health"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.build_info import calculate_build_id


def frontend_needs_build(frontend_dir: Path = FRONTEND_DIR) -> bool:
    """Return True when the production bundle is missing or older than its inputs."""
    output = frontend_dir / "dist" / "index.html"
    if not output.is_file():
        return True

    output_mtime = output.stat().st_mtime_ns
    input_files = [
        frontend_dir / "index.html",
        frontend_dir / "package.json",
        frontend_dir / "package-lock.json",
        frontend_dir / "tsconfig.json",
        frontend_dir / "tsconfig.app.json",
        frontend_dir / "tsconfig.node.json",
        frontend_dir / "vite.config.ts",
        frontend_dir / "vite.config.js",
    ]
    source_dir = frontend_dir / "src"
    if source_dir.is_dir():
        input_files.extend(path for path in source_dir.rglob("*") if path.is_file())

    return any(
        path.is_file() and path.stat().st_mtime_ns > output_mtime
        for path in input_files
    )


def read_health() -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.2) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if response.status == 200 and isinstance(data, dict) and data.get("ok") is True else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def read_local_proxy_health() -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(LOCAL_PROXY_HEALTH_URL, timeout=1.2) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if response.status == 200 and isinstance(data, dict) and data.get("ok") is True else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_healthy() -> bool:
    return read_health() is not None


def current_build_id() -> str:
    return calculate_build_id(PROJECT_ROOT)


def _valid_process_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def listening_process_id(port: int = 5177) -> int | None:
    """Return the sole local PID listening on the workbench port."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                encoding="mbcs",
                errors="replace",
                timeout=4,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            owners: set[int] = set()
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
                    continue
                if not parts[1].rsplit(":", 1)[-1].isdigit() or int(parts[1].rsplit(":", 1)[-1]) != port:
                    continue
                if parts[-1].isdigit() and int(parts[-1]) > 0:
                    owners.add(int(parts[-1]))
            return next(iter(owners)) if len(owners) == 1 else None

        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            check=False,
        )
        owners = {int(line) for line in (result.stdout or "").splitlines() if line.strip().isdigit() and int(line) > 0}
        return next(iter(owners)) if len(owners) == 1 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _workbench_process_command(process_id: int) -> tuple[str, str] | None:
    try:
        if os.name == "nt":
            command = (
                "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = "
                + str(process_id)
                + "\"; if($p){@{ExecutablePath=$p.ExecutablePath;CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                encoding="mbcs",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                return None
            return str(value.get("ExecutablePath") or ""), str(value.get("CommandLine") or "")

        if sys.platform == "darwin":
            result = subprocess.run(
                ["ps", "-p", str(process_id), "-o", "command="],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                check=False,
            )
            command_line = (result.stdout or "").strip()
            if result.returncode != 0 or not command_line:
                return None
            return command_line.split(maxsplit=1)[0], command_line

        command_path = Path(f"/proc/{process_id}/cmdline")
        if not command_path.is_file():
            return None
        command_line = command_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        executable = Path(f"/proc/{process_id}/exe").resolve()
        return str(executable), command_line
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return None


def is_managed_workbench_process(process_id: int, observed_build_id: str) -> bool:
    """Fail closed unless health, port ownership and process command all agree."""
    confirmed = read_health()
    if not confirmed:
        return False
    if _valid_process_id(confirmed.get("process_id")) != process_id:
        return False
    if confirmed.get("build_id") != observed_build_id:
        return False
    if listening_process_id() != process_id:
        return False
    process = _workbench_process_command(process_id)
    if not process:
        return False
    executable, command_line = process
    executable_path = PureWindowsPath(executable) if "\\" in executable else Path(executable)
    executable_name = executable_path.name.lower()
    normalized_command = re.sub(r"\s+", " ", command_line).strip().lower()
    return (
        executable_name.startswith("python")
        and "backend.app.main" in normalized_command
        and re.search(r"(?:^|\s)-m(?:\s|$)", normalized_command) is not None
    )


def terminate_workbench_process(process_id: int) -> bool:
    """Request a targeted graceful stop; never use a force/tree-wide kill."""
    try:
        if os.name == "nt":
            try:
                os.kill(process_id, signal.CTRL_BREAK_EVENT)
                return True
            except (OSError, SystemError):
                pass
            result = subprocess.run(
                ["taskkill", "/PID", str(process_id)],
                capture_output=True,
                timeout=6,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        os.kill(process_id, signal.SIGTERM)
        return True
    except (OSError, SystemError, subprocess.SubprocessError):
        return False


def is_managed_local_proxy(process_id: int, observed_build_id: str) -> bool:
    health = read_local_proxy_health()
    if not health or _valid_process_id(health.get("pid")) != process_id:
        return False
    if listening_process_id(LOCAL_PROXY_PORT) != process_id:
        return False
    process = _workbench_process_command(process_id)
    if not process:
        return False
    _executable, command_line = process
    return "local_openai_proxy.py" in command_line


def port_is_open(port: int = 5177) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait_for_port_release(port: int = 5177, timeout_seconds: float = 8) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not port_is_open(port):
            return True
        time.sleep(0.2)
    return not port_is_open(port)


def open_app() -> None:
    if os.getenv("WORKBENCH_NO_BROWSER") != "1":
        webbrowser.open(APP_URL, new=2)


def start_server() -> subprocess.Popen[bytes]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = STATE_DIR / "server.out.log"
    stderr_path = STATE_DIR / "server.err.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    creation_flags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.app.main"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    finally:
        stdout.close()
        stderr.close()
    return process


def start_local_proxy() -> subprocess.Popen[bytes]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = STATE_DIR / "local_proxy.out.log"
    stderr_path = STATE_DIR / "local_proxy.err.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            [sys.executable, "scripts/local_openai_proxy.py"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    finally:
        stdout.close()
        stderr.close()
    return process


def ensure_local_proxy(expected_build_id: str) -> bool:
    health = read_local_proxy_health()
    if health:
        process_id = _valid_process_id(health.get("pid"))
        if health.get("runtime_build_id") == expected_build_id and process_id is not None:
            return True
        if process_id is None or not is_managed_local_proxy(process_id, str(health.get("build_id") or "")):
            print("检测到非本工作台管理的本机代理占用 8787 端口，请先关闭它后再启动。", file=sys.stderr)
            return False
        terminate_workbench_process(process_id)
        if not wait_for_port_release(LOCAL_PROXY_PORT, 8):
            print("旧版本机代理未能安全退出。请手动关闭后再重新启动工作台。", file=sys.stderr)
            return False
    elif port_is_open(LOCAL_PROXY_PORT):
        print("8787 端口已被其他程序占用，无法安全启动本机代理。", file=sys.stderr)
        return False

    process = start_local_proxy()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        proxy_health = read_local_proxy_health()
        if proxy_health and proxy_health.get("build_id") == expected_build_id:
            return True
        if process.poll() is not None:
            break
        time.sleep(0.2)
    print("本机 OpenAI 代理启动失败，请查看 .workbench/local_proxy.err.log。", file=sys.stderr)
    return False


def main() -> int:
    expected_build_id = current_build_id()
    if not ensure_local_proxy(expected_build_id):
        return 1
    health = read_health()
    if health:
        running_build_id = health.get("build_id")
        process_id = _valid_process_id(health.get("process_id"))
        if not isinstance(running_build_id, str) or not running_build_id or process_id is None:
            print(
                "检测到旧版工作台仍在运行，但缺少安全重启信息。请先手动关闭旧工作台，再重新双击启动。",
                file=sys.stderr,
            )
            return 2
        if running_build_id != expected_build_id:
            if not is_managed_workbench_process(process_id, running_build_id):
                print(
                    "检测到工作台代码已更新，但无法安全确认旧进程。请先手动关闭旧工作台，再重新双击启动。",
                    file=sys.stderr,
                )
                return 2
            terminate_workbench_process(process_id)
            if not wait_for_port_release():
                print(
                    "旧版工作台未能安全退出。请手动关闭后再重新双击启动。",
                    file=sys.stderr,
                )
                return 2
            health = None
        else:
            open_app()
            print("AI 文案工作台已经打开。")
            return 0

    process = start_server()
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline:
        started_health = read_health()
        if started_health:
            started_pid = _valid_process_id(started_health.get("process_id"))
            if started_health.get("build_id") == expected_build_id and started_pid is not None:
                open_app()
                print("AI 文案工作台启动成功。")
                return 0
        if process.poll() is not None:
            break
        time.sleep(0.35)

    print("工作台启动失败，请查看 .workbench/server.err.log。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if "--frontend-build-required" in sys.argv[1:]:
        raise SystemExit(1 if frontend_needs_build() else 0)
    raise SystemExit(main())
