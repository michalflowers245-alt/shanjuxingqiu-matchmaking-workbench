from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


SKILL_ROOT = PROJECT_ROOT / "skills"
EXPECTED_SKILLS = {
    "researcher": "researcher-core",
    "writer": "copywriter-core",
    "editor": "editor-core",
}


@dataclass(frozen=True)
class SkillPackage:
    name: str
    role: str
    version: str
    description: str
    instructions: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    path: Path

    def public(self) -> dict[str, Any]:
        return {
            "id": self.name,
            "name": self.name,
            "role": self.role,
            "version": self.version,
            "description": self.description,
            "installed": True,
            "enabled": True,
            "status": "ready",
        }


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter 未闭合")
    metadata: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, text[end + 5 :].strip()


class SkillRegistry:
    def __init__(self, root: Path = SKILL_ROOT):
        self.root = root
        self._by_role: dict[str, SkillPackage] = {}
        self.reload()

    def reload(self) -> list[SkillPackage]:
        loaded: dict[str, SkillPackage] = {}
        for role, expected_name in EXPECTED_SKILLS.items():
            folder = self.root / expected_name
            skill_path = folder / "SKILL.md"
            schema_path = folder / "schema.json"
            if not skill_path.is_file() or not schema_path.is_file():
                raise RuntimeError(f"数字员工 Skill 缺失：{expected_name}")
            metadata, instructions = _frontmatter(skill_path.read_text(encoding="utf-8-sig"))
            schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
            schema_metadata = schema.get("metadata") or {}
            if metadata.get("name") != expected_name or schema_metadata.get("role") != role:
                raise RuntimeError(f"数字员工 Skill 身份不匹配：{expected_name}")
            if not instructions or not isinstance(schema.get("output"), dict):
                raise RuntimeError(f"数字员工 Skill 内容不完整：{expected_name}")
            loaded[role] = SkillPackage(
                name=expected_name,
                role=role,
                version=str(schema_metadata.get("version") or "1.0.0"),
                description=metadata.get("description") or "",
                instructions=instructions,
                input_schema=schema.get("input") or {},
                output_schema=schema["output"],
                path=folder,
            )
        self._by_role = loaded
        return list(loaded.values())

    def for_role(self, role: str) -> SkillPackage:
        package = self._by_role.get(role)
        if not package:
            raise RuntimeError(f"未安装数字员工 Skill：{role}")
        return package

    def list(self) -> list[dict[str, Any]]:
        return [self._by_role[role].public() for role in ("researcher", "writer", "editor")]


skill_registry = SkillRegistry()
