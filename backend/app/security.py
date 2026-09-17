from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from .config import SECRET_FILE


MACOS_KEYCHAIN_SERVICE = "AI 文案工作台"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect_windows(value: bytes) -> bytes:
    in_blob, _ = _blob(value)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "copy-workbench", None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect_windows(value: bytes) -> bytes:
    in_blob, _ = _blob(value)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _run_macos_keychain(arguments: list[str], input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    """Call the system Keychain tool without putting a secret in argv or logs."""
    try:
        return subprocess.run(
            ["/usr/bin/security", *arguments],
            input=input_data,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法访问 macOS 钥匙串，密钥未保存") from exc


def _put_macos_keychain(reference: str, value: str) -> None:
    # With -w as the last argument, security prompts twice on stdin. This keeps
    # the value out of the process command line and shell history.
    result = _run_macos_keychain(
        ["add-generic-password", "-U", "-a", reference, "-s", MACOS_KEYCHAIN_SERVICE, "-w"],
        input_data=f"{value}\n{value}\n",
    )
    if result.returncode != 0:
        raise RuntimeError("无法写入 macOS 钥匙串，密钥未保存")


def _get_macos_keychain(reference: str) -> str | None:
    result = _run_macos_keychain(
        ["find-generic-password", "-a", reference, "-s", MACOS_KEYCHAIN_SERVICE, "-w"]
    )
    if result.returncode != 0:
        return None
    value = result.stdout.rstrip("\r\n")
    return value or None


def _delete_macos_keychain(reference: str) -> None:
    _run_macos_keychain(["delete-generic-password", "-a", reference, "-s", MACOS_KEYCHAIN_SERVICE])


class SecretVault:
    """Stores only opaque encrypted blobs outside the business database."""

    def __init__(self, path: Path = SECRET_FILE):
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def put(self, value: str, reference: str | None = None) -> str:
        value = value.strip()
        if not value:
            raise ValueError("密钥不能为空")
        if "\n" in value or "\r" in value:
            raise ValueError("密钥格式不正确")
        ref = reference or f"vault:{uuid4().hex}"
        if sys.platform == "darwin":
            _put_macos_keychain(ref, value)
            return ref
        if os.name != "nt":
            raise RuntimeError("当前系统未提供 DPAPI，拒绝以弱加密方式保存密钥")
        cipher = _protect_windows(value.encode("utf-8"))
        with self._lock:
            data = self._load()
            data[ref] = base64.b64encode(cipher).decode("ascii")
            self._save(data)
        return ref

    def get(self, reference: str | None) -> str | None:
        if not reference:
            return None
        if reference.startswith("env:"):
            return os.getenv(reference[4:])
        if sys.platform == "darwin":
            return _get_macos_keychain(reference)
        with self._lock:
            encoded = self._load().get(reference)
        if not encoded:
            return None
        if os.name != "nt":
            return None
        return _unprotect_windows(base64.b64decode(encoded)).decode("utf-8")

    def delete(self, reference: str | None) -> None:
        if not reference or reference.startswith("env:"):
            return
        if sys.platform == "darwin":
            _delete_macos_keychain(reference)
            return
        with self._lock:
            data = self._load()
            if reference in data:
                del data[reference]
                self._save(data)


vault = SecretVault()
