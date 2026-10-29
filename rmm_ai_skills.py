"""
Load server-side AI skills (Markdown + YAML frontmatter) for the web AI assistant.
"""

from __future__ import annotations

from rmm_settings import getenv as settings_getenv

import os
import re

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def ai_skills_dir() -> str:
    """Directory containing *.md skill files (override with RMM_AI_SKILLS_DIR)."""
    custom = (settings_getenv("RMM_AI_SKILLS_DIR") or "").strip()
    if custom:
        return os.path.realpath(custom)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-skills")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, parts[2].lstrip("\n")


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _skill_id_from_name(filename: str, meta: dict[str, str]) -> str | None:
    raw = (meta.get("id") or os.path.splitext(filename)[0]).strip().lower()
    if _SKILL_ID_RE.match(raw):
        return raw
    return None


def load_skill_file(path: str) -> dict | None:
    filename = os.path.basename(path)
    if not filename.lower().endswith(".md"):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    skill_id = _skill_id_from_name(filename, meta)
    if not skill_id:
        return None
    title = (meta.get("title") or skill_id.replace("-", " ").title()).strip()
    description = (meta.get("description") or "").strip()
    return {
        "id": skill_id,
        "title": title,
        "description": description,
        "default": _truthy(meta.get("default")),
        "always": _truthy(meta.get("always")),
        "body": body.strip(),
        "filename": filename,
    }


def _skill_directories(directories: list[str] | None = None) -> list[str]:
    """Return ordered skill directories, with later directories overriding earlier ones."""
    if directories is None:
        return [ai_skills_dir()]
    return [os.path.realpath(str(directory)) for directory in directories if str(directory).strip()]


def list_ai_skills(
    directories: list[str] | None = None,
    enabled_overrides: dict[str, bool] | None = None,
) -> list[dict]:
    """Return skill metadata for the operator UI (no body text)."""
    rows_by_id: dict[str, dict] = {}
    for directory in _skill_directories(directories):
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            skill = load_skill_file(path)
            if not skill:
                continue
            rows_by_id[skill["id"]] = {
                "id": skill["id"],
                "title": skill["title"],
                "description": skill["description"],
                "default": skill["default"],
                "always": skill["always"],
                "filename": skill["filename"],
            }
    rows = []
    for skill_id, row in rows_by_id.items():
        row = dict(row)
        if enabled_overrides and skill_id in enabled_overrides:
            row["enabled"] = bool(enabled_overrides[skill_id])
        else:
            row["enabled"] = bool(row["default"] or row["always"])
        rows.append(row)
    return sorted(rows, key=lambda row: row["id"])


def resolve_ai_skills(
    skill_ids: list[str] | None,
    directories: list[str] | None = None,
    enabled_overrides: dict[str, bool] | None = None,
) -> list[dict]:
    """
    Load full skill bodies.

    - skill_ids is None: skills marked default: true or always: true in frontmatter
    - skill_ids is a list: those ids plus all always: true skills
    """
    available = {
        row["id"]: row
        for row in (_load_all_skills_full(directories))
        if not enabled_overrides or enabled_overrides.get(row["id"], True)
    }
    always = [skill for skill in available.values() if skill.get("always")]
    if skill_ids is None:
        return [
            skill
            for skill in available.values()
            if skill.get("default") or skill.get("always")
        ]
    chosen: list[dict] = []
    included = set()
    for skill in always:
        chosen.append(skill)
        included.add(skill["id"])
    for raw in skill_ids:
        sid = (raw or "").strip().lower()
        if sid and sid in available and sid not in included:
            chosen.append(available[sid])
            included.add(sid)
    return chosen


def _load_all_skills_full(directories: list[str] | None = None) -> list[dict]:
    """Load full skill bodies, allowing a workspace overlay directory."""
    rows_by_id: dict[str, dict] = {}
    for directory in _skill_directories(directories):
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            skill = load_skill_file(path)
            if skill:
                rows_by_id[skill["id"]] = skill
    return sorted(rows_by_id.values(), key=lambda row: row["id"])


def compose_system_prompt(
    base: str,
    *,
    skill_ids: list[str] | None,
    session_context: str = "",
    skill_documents: list[dict] | None = None,
    skill_directories: list[str] | None = None,
    enabled_overrides: dict[str, bool] | None = None,
) -> str:
    """Merge base prompt, selected skills, and optional session context."""
    parts = [base.rstrip()]
    skills = (
        [
            dict(skill)
            for skill in skill_documents
            if isinstance(skill, dict) and str(skill.get("body") or "").strip()
        ]
        if skill_documents is not None
        else resolve_ai_skills(skill_ids, skill_directories, enabled_overrides)
    )
    if skills:
        parts.append("\n## Operator skills\n")
        parts.append("Follow these server-provided skills when relevant:\n")
        for skill in skills:
            parts.append(f"\n### {skill['title']} ({skill['id']})\n")
            parts.append(skill["body"].strip())
            parts.append("")
    if session_context.strip():
        parts.append(session_context.strip())
    return "\n".join(parts).strip()
