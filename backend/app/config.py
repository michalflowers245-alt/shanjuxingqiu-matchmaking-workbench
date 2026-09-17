from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / ".workbench"
UPLOAD_DIR = STATE_DIR / "uploads"
EXPORT_DIR = STATE_DIR / "exports"
ASSET_DIR = STATE_DIR / "assets"
LIFE_CASE_DIR = STATE_DIR / "life_cases"
RADAR_CAPTURE_DIR = STATE_DIR / "radar_captures"
SECRET_FILE = STATE_DIR / "secrets.json"
DB_PATH = STATE_DIR / "copy_workbench.sqlite3"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5177
    worker_poll_seconds: float = 0.7
    job_lease_seconds: int = 180
    max_job_retries: int = 2
    max_upload_bytes: int = 30 * 1024 * 1024


def ensure_directories() -> None:
    for directory in (STATE_DIR, UPLOAD_DIR, EXPORT_DIR, ASSET_DIR, LIFE_CASE_DIR, RADAR_CAPTURE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


load_dotenv()
ensure_directories()
settings = Settings(
    host=os.getenv("WORKBENCH_HOST", "127.0.0.1"),
    port=int(os.getenv("WORKBENCH_PORT", "5177")),
)
