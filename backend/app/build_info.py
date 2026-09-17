from __future__ import annotations

import hashlib
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def calculate_build_id(root: Path = PROJECT_ROOT) -> str:
    """Fingerprint all runtime instructions so a launcher can detect a stale process."""
    candidates = [*sorted((root / "backend" / "app").glob("*.py"))]
    candidates.extend(sorted(path for path in (root / "skills").rglob("*") if path.is_file()))
    candidates.extend(path for path in (root / "scripts").glob("local_openai_proxy.py") if path.is_file())
    candidates.append(root / "requirements.txt")
    digest = hashlib.sha256()
    for path in candidates:
        if not path.is_file():
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


BUILD_ID = calculate_build_id()
PROCESS_ID = os.getpid()


if __name__ == "__main__":
    print(BUILD_ID)
