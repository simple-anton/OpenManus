"""Skills: reusable instructions the agent follows for a kind of task.

A skill is a markdown file with a small front matter header, stored under
`config/skills/<slug>/SKILL.md` - the same shape Anthropic's skill repositories
use, so a skill published on GitHub can be imported as is. When a task has
skills attached, their text is appended to the agent's system prompt.
"""

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import PROJECT_ROOT
from app.logger import logger


SKILLS_DIR = PROJECT_ROOT / "config" / "skills"
MAX_SKILL_CHARS = 60_000
RAW_HOST = "https://raw.githubusercontent.com"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", name.strip().lower()).strip("-")
    return slug[:60] or "skill"


def _parse(text: str) -> Dict[str, Any]:
    """Split `---` front matter (name/description) from the body."""
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = parts[2].lstrip("\n")
    return {"meta": meta, "body": body}


def _serialise(name: str, description: str, body: str) -> str:
    header = f"---\nname: {name}\ndescription: {description}\n---\n\n"
    return header + body.strip() + "\n"


def list_skills() -> List[Dict[str, Any]]:
    if not SKILLS_DIR.exists():
        return []
    skills = []
    for folder in sorted(SKILLS_DIR.iterdir()):
        skill = read_skill(folder.name)
        if skill:
            skills.append({k: v for k, v in skill.items() if k != "body"})
    return skills


def read_skill(slug: str) -> Optional[Dict[str, Any]]:
    path = SKILLS_DIR / slug / "SKILL.md"
    if not path.is_file():
        return None
    parsed = _parse(path.read_text(encoding="utf-8", errors="replace"))
    return {
        "slug": slug,
        "name": parsed["meta"].get("name", slug),
        "description": parsed["meta"].get("description", ""),
        "body": parsed["body"],
        "chars": len(parsed["body"]),
    }


def write_skill(slug: str, name: str, description: str, body: str) -> Dict[str, Any]:
    if len(body) > MAX_SKILL_CHARS:
        raise ValueError("Навык слишком длинный")
    slug = slugify(slug or name)
    folder = SKILLS_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        _serialise(name or slug, description, body), encoding="utf-8"
    )
    return read_skill(slug)


def delete_skill(slug: str) -> None:
    folder = SKILLS_DIR / slugify(slug)
    if folder.is_dir():
        shutil.rmtree(folder)


def prompt_for(slugs: List[str]) -> str:
    """The text appended to the system prompt for the attached skills."""
    parts = []
    for slug in slugs:
        skill = read_skill(slug)
        if skill:
            parts.append(
                f"# Навык: {skill['name']}\n"
                f"{skill['description']}\n\n{skill['body']}".strip()
            )
    if not parts:
        return ""
    return (
        "\n\nНиже приведены навыки — методики, которым нужно следовать "
        "при выполнении задачи.\n\n" + "\n\n---\n\n".join(parts)
    )


# ------------------------------------------------------------------ importing


def _raw_candidates(url: str) -> List[str]:
    """Turn a GitHub page URL into candidate raw URLs for a SKILL.md."""
    url = url.strip().rstrip("/")
    if url.startswith(RAW_HOST) or url.endswith(".md"):
        return [url]

    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(/.*)?)?$", url
    )
    if not match:
        return [url]
    owner, repo, branch, path = match.groups()
    path = (path or "").strip("/")
    branches = [branch] if branch else ["HEAD", "main", "master"]
    candidates = []
    for name in branches:
        prefix = f"{RAW_HOST}/{owner}/{repo}/{name}"
        candidates.append(f"{prefix}/{path}/SKILL.md" if path else f"{prefix}/SKILL.md")
        if path:
            candidates.append(f"{prefix}/{path}")
    return candidates


async def import_from_url(url: str) -> Dict[str, Any]:
    """Fetch a skill published on GitHub (or any raw markdown URL)."""
    errors = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for candidate in _raw_candidates(url):
            try:
                response = await client.get(candidate)
            except httpx.HTTPError as exc:
                errors.append(f"{candidate}: {exc}")
                continue
            if response.status_code != 200:
                errors.append(f"{candidate}: HTTP {response.status_code}")
                continue
            parsed = _parse(response.text)
            name = parsed["meta"].get("name") or Path(candidate).parent.name
            logger.info(f"Imported skill from {candidate}")
            return write_skill(
                slugify(name),
                name,
                parsed["meta"].get("description", ""),
                parsed["body"],
            )
    raise ValueError("Не удалось скачать навык. " + "; ".join(errors[:3]))
