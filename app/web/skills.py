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
GITHUB_API = "https://api.github.com"
# a skill folder may carry scripts and references; keep the import bounded
MAX_BUNDLE_FILES = 40
MAX_BUNDLE_BYTES = 512 * 1024


# Verified public skills in the same format, from Anthropic's open repository.
# These are not the skills Manus ships - those are not published anywhere.
PUBLIC_CATALOGUE = [
    {
        "title": "Создание навыков",
        "description": "Как писать и оформлять навыки: структура, формулировки, типичные ошибки.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/skill-creator",
    },
    {
        "title": "Сборка MCP-серверов",
        "description": "Как сделать собственный MCP-сервер, чтобы подключить его в разделе «Плагины».",
        "url": "https://github.com/anthropics/skills/tree/main/skills/mcp-builder",
    },
    {
        "title": "Работа над документом",
        "description": "Методика совместной подготовки документов: спецификаций, предложений, отчётов.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/doc-coauthoring",
    },
    {
        "title": "Фронтенд-дизайн",
        "description": "Правила вёрстки и оформления интерфейсов.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/frontend-design",
    },
    {
        "title": "Дизайн на холсте",
        "description": "Макеты, постеры, лендинги: композиция и типографика.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/canvas-design",
    },
    {
        "title": "Бренд-гайд",
        "description": "Как держать единый стиль в материалах.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/brand-guidelines",
    },
    {
        "title": "Внутренние коммуникации",
        "description": "Письма и объявления для команды.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/internal-comms",
    },
    {
        "title": "Темы оформления",
        "description": "Подбор цветовых тем и их применение.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/theme-factory",
    },
    {
        "title": "Тестирование веб-приложений",
        "description": "Как проверять веб-приложение в браузере и находить дефекты.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/webapp-testing",
    },
    {
        "title": "Генеративная графика",
        "description": "Алгоритмическое искусство и визуализации кодом.",
        "url": "https://github.com/anthropics/skills/tree/main/skills/algorithmic-art",
    },
]


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


def bundle_files(slug: str) -> List[str]:
    """Extra files that came with the skill, relative to its folder."""
    folder = SKILLS_DIR / slug
    if not folder.is_dir():
        return []
    return sorted(
        str(path.relative_to(folder))
        for path in folder.rglob("*")
        if path.is_file() and path.name != "SKILL.md"
    )


def prompt_for(slugs: List[str]) -> str:
    """The text appended to the system prompt for the attached skills."""
    parts = []
    for slug in slugs:
        skill = read_skill(slug)
        if skill:
            section = (
                f"# Навык: {skill['name']}\n"
                f"{skill['description']}\n\n{skill['body']}".strip()
            )
            extra = bundle_files(slug)
            if extra:
                # the skill text refers to its own files; say where they are
                section += (
                    f"\n\nФайлы этого навыка лежат в {SKILLS_DIR / slug} — "
                    f"открывайте их оттуда: {', '.join(extra[:20])}."
                )
            parts.append(section)
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
    if not url.startswith(("http://", "https://")):
        raise ValueError("Ссылка должна начинаться с http:// или https://")
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


def _repo_parts(url: str):
    """owner, repo, branch, path for a github.com repo or tree URL."""
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(/.*)?)?/?$",
        url.strip(),
    )
    if not match:
        return None
    owner, repo, branch, path = match.groups()
    return owner, repo, branch or "main", (path or "").strip("/")


async def _import_folder(
    client: httpx.AsyncClient, url: str
) -> Optional[Dict[str, Any]]:
    """Bring the whole skill folder, not just SKILL.md.

    Many published skills keep scripts and reference documents next to their
    SKILL.md and refer to them by relative path; without those files the skill
    is half a skill.
    """
    parts = _repo_parts(url)
    if parts is None:
        return None
    owner, repo, branch, path = parts

    try:
        response = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        )
        if response.status_code != 200:
            return None
        tree = response.json().get("tree", [])
    except (httpx.HTTPError, ValueError):
        return None

    prefix = f"{path}/" if path else ""
    blobs = [
        item
        for item in tree
        if item.get("type") == "blob"
        and item["path"].startswith(prefix)
        and item.get("size", 0) <= MAX_BUNDLE_BYTES
    ]
    skill_files = [item for item in blobs if item["path"].endswith("SKILL.md")]
    if not skill_files:
        return None

    # the SKILL.md closest to the requested path defines the skill
    skill_entry = min(skill_files, key=lambda item: item["path"].count("/"))
    root = skill_entry["path"][: -len("SKILL.md")]

    manifest = await client.get(
        f"{RAW_HOST}/{owner}/{repo}/{branch}/{skill_entry['path']}"
    )
    if manifest.status_code != 200:
        return None
    parsed = _parse(manifest.text)
    name = parsed["meta"].get("name") or (root.strip("/").split("/")[-1] or repo)
    skill = write_skill(
        slugify(name), name, parsed["meta"].get("description", ""), parsed["body"]
    )

    saved = []
    for item in blobs:
        if not item["path"].startswith(root) or item["path"] == skill_entry["path"]:
            continue
        if len(saved) >= MAX_BUNDLE_FILES:
            break
        relative = item["path"][len(root) :]
        try:
            payload = await client.get(
                f"{RAW_HOST}/{owner}/{repo}/{branch}/{item['path']}"
            )
            if payload.status_code != 200:
                continue
            target = SKILLS_DIR / skill["slug"] / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload.content)
            saved.append(relative)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(f"Skipped {item['path']}: {exc}")

    logger.info(f"Imported skill {skill['slug']} with {len(saved)} extra file(s)")
    skill["files"] = saved
    return skill


async def import_from_url(url: str) -> Dict[str, Any]:
    """Fetch a skill published on GitHub (or any raw markdown URL)."""
    errors = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        whole = await _import_folder(client, url)
        if whole is not None:
            return whole
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
            skill = write_skill(
                slugify(name),
                name,
                parsed["meta"].get("description", ""),
                parsed["body"],
            )
            # warn when the text leans on files we did not bring along
            if re.search(r"(scripts|references|assets|templates)/", parsed["body"]):
                skill["warning"] = (
                    "Навык ссылается на свои вспомогательные файлы, но скачан только SKILL.md. "
                    "Попробуйте импортировать ссылку на папку навыка на github.com."
                )
            skill["files"] = []
            return skill
    raise ValueError("Не удалось скачать навык. " + "; ".join(errors[:3]))
