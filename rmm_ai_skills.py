"""
Load server-side AI skills (Markdown + YAML frontmatter) for the web AI assistant.
"""

from __future__ import annotations

import os
import re

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def ai_skills_dir() -> str:
    """Directory containing *.md skill files (override with RMM_AI_SKILLS_DIR)."""
    custom = (os.environ.get("RMM_AI_SKILLS_DIR") or "").strip()
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
        "body": body.strip(),
        "filename": filename,
    }


def list_ai_skills() -> list[dict]:
    """Return skill metadata for the operator UI (no body text)."""
    directory = ai_skills_dir()
    if not os.path.isdir(directory):
        return []
    rows: list[dict] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        skill = load_skill_file(path)
        if not skill:
            continue
        rows.append({
            "id": skill["id"],
            "title": skill["title"],
            "description": skill["description"],
            "default": skill["default"],
            "filename": skill["filename"],
        })
    return rows


def resolve_ai_skills(skill_ids: list[str] | None) -> list[dict]:
    """
    Load full skill bodies.

    - skill_ids is None: skills marked default: true in frontmatter
    - skill_ids is a list: exactly those ids (empty list = no skills)
    """
    available = {row["id"]: row for row in (_load_all_skills_full())}
    if skill_ids is None:
        return [available[sid] for sid in available if available[sid].get("default")]
    chosen: list[dict] = []
    for raw in skill_ids:
        sid = (raw or "").strip().lower()
        if sid and sid in available:
            chosen.append(available[sid])
    return chosen


def _load_all_skills_full() -> list[dict]:
    directory = ai_skills_dir()
    if not os.path.isdir(directory):
        return []
    rows: list[dict] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        skill = load_skill_file(path)
        if skill:
            rows.append(skill)
    return rows


def compose_system_prompt(
    base: str,
    *,
    skill_ids: list[str] | None,
    session_context: str = "",
) -> str:
    """Merge base prompt, selected skills, and optional session context."""
    parts = [base.rstrip()]
    skills = resolve_ai_skills(skill_ids)
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
