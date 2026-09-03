"""Skills: reusable instructions the agent follows for a kind of task.

A skill is a markdown file with a small front matter header, stored under
`config/skills/<slug>/SKILL.md` - the same shape Anthropic's skill repositories
use, so a skill published on GitHub can be imported as is. When a task has
skills attached, their text is appended to the agent's system prompt.
"""

import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import PROJECT_ROOT
from app.logger import logger


SKILLS_DIR = PROJECT_ROOT / "config" / "skills"
MAX_SKILL_CHARS = 60_000
RAW_HOST = "https://raw.githubusercontent.com"
GITHUB_API = "https://api.github.com"
# where Manus keeps skills inside its own sandbox; rewritten to our folder
FOREIGN_ROOTS = ("/home/ubuntu/skills/", "/mnt/skills/", "/opt/skills/")
# Things that exist only inside the agent a skill came from. "blocking" means
# the skill cannot work here at all; "partial" means the method still applies
# but some data source has to be wired up locally first.
FOREIGN_MARKERS = [
    {
        "match": "/opt/.manus",
        "level": "blocking",
        "note": "скрипты навыка обращаются к внутренней среде Manus (/opt/.manus)",
    },
    {
        "match": "sandbox-runtime",
        "level": "blocking",
        "note": "скрипты рассчитаны на runtime песочницы Manus",
    },
    {
        "match": "from data_api",
        "level": "blocking",
        "note": "скрипты импортируют ApiClient из песочницы Manus",
    },
    {
        "match": "Yahoo/get_",
        "level": "partial",
        "note": "навык вызывает коннектор Yahoo из Manus",
        "advice": "Опишите эквивалентный источник котировок в «Настройки → Свои API» "
        "и замените в тексте навыка вызовы вида Yahoo/get_stock_chart на имя своего инструмента.",
    },
    {
        "match": "Twitter/",
        "level": "partial",
        "note": "навык вызывает коннектор Twitter из Manus",
        "advice": "Подключите свой источник данных X/Twitter через «Свои API» или MCP-плагин "
        "и поправьте вызовы в тексте навыка.",
    },
    {
        "match": "LinkedIn/",
        "level": "partial",
        "note": "навык вызывает коннектор LinkedIn из Manus",
        "advice": "Подключите источник данных LinkedIn через «Свои API» и поправьте вызовы в тексте.",
    },
    {
        "match": "manus-api",
        "level": "partial",
        "note": "навык обращается к API самого Manus",
        "advice": "Эти вызовы здесь не работают: уберите их из текста или замените своими инструментами.",
    },
]
# a skill folder may carry scripts and references; keep the import bounded
MAX_BUNDLE_FILES = 40
MAX_BUNDLE_BYTES = 512 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


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
        "compatibility": load_compatibility(slug),
        "files": bundle_files(slug),
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


def localise(text: str, folder: Path) -> str:
    """Point paths from another agent's sandbox at this installation."""
    for root in FOREIGN_ROOTS:
        text = re.sub(re.escape(root) + r"[\w.-]+/?", str(folder) + "/", text)
    return text


def inspect_compatibility(texts: Dict[str, str]) -> Dict[str, Any]:
    """Verdict on a skill: works here, needs wiring up, or cannot work at all."""
    notes, advice = [], []
    level = "ok"
    for marker in FOREIGN_MARKERS:
        where = [name for name, text in texts.items() if marker["match"] in text]
        if not where:
            continue
        notes.append(f"{marker['note']} ({', '.join(sorted(where)[:3])})")
        if marker.get("advice") and marker["advice"] not in advice:
            advice.append(marker["advice"])
        if marker["level"] == "blocking":
            level = "blocking"
        elif level != "blocking":
            level = "partial"
    return {"status": level, "notes": notes, "advice": " ".join(advice)}


def read_texts(folder: Path) -> Dict[str, str]:
    """Readable contents of a skill folder, keyed by relative path."""
    texts = {}
    for path in folder.rglob("*"):
        if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
            continue
        try:
            texts[str(path.relative_to(folder))] = path.read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            continue
    return texts


def save_compatibility(slug: str, verdict: Dict[str, Any]) -> None:
    try:
        (SKILLS_DIR / slug / "compatibility.json").write_text(
            json.dumps(verdict, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"Could not store the verdict for {slug}: {exc}")


def load_compatibility(slug: str) -> Dict[str, Any]:
    path = SKILLS_DIR / slug / "compatibility.json"
    if not path.is_file():
        return {"status": "ok", "notes": [], "advice": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "ok", "notes": [], "advice": ""}


def bundle_files(slug: str) -> List[str]:
    """Extra files that came with the skill, relative to its folder."""
    folder = SKILLS_DIR / slug
    if not folder.is_dir():
        return []
    return sorted(
        str(path.relative_to(folder))
        for path in folder.rglob("*")
        if path.is_file() and path.name not in {"SKILL.md", "compatibility.json"}
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


class Incompatible(ValueError):
    """A skill that cannot work in this installation at all."""

    def __init__(self, verdict: Dict[str, Any], name: str):
        self.verdict = verdict
        self.name = name
        super().__init__("; ".join(verdict["notes"]))


def _finish_import(
    slug: str, name: str, description: str, body: str, folder: Path, force: bool
) -> Dict[str, Any]:
    """Judge what was unpacked, then keep it, trim it, or refuse it."""
    texts = read_texts(folder)
    texts["SKILL.md"] = body
    verdict = inspect_compatibility(texts)

    if verdict["status"] == "blocking" and not force:
        shutil.rmtree(folder, ignore_errors=True)
        raise Incompatible(verdict, name)

    if verdict["status"] == "blocking":  # forced: keep the method, drop what cannot run
        for path in folder.rglob("*"):
            if path.is_file() and path.name != "SKILL.md":
                path.unlink()
        for path in sorted(folder.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
        verdict = {
            "status": "partial",
            "notes": verdict["notes"],
            "advice": "Импортирована только текстовая методика: скрипты навыка работают лишь "
            "внутри той среды, откуда он пришёл. Всё, что в тексте опирается на них, "
            "придётся заменить своими инструментами.",
        }

    skill = write_skill(slug, name, description, body)
    save_compatibility(skill["slug"], verdict)
    skill["compatibility"] = verdict
    skill["files"] = bundle_files(skill["slug"])
    return skill


def import_archive(data: bytes, force: bool = False) -> Dict[str, Any]:
    """Take a zipped skill folder, as exported by another agent."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Архив слишком большой")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Это не ZIP-архив: {exc}") from exc

    members = [item for item in archive.infolist() if not item.is_dir()]
    if len(members) > MAX_BUNDLE_FILES * 2:
        raise ValueError("В архиве слишком много файлов")

    manifests = [item for item in members if Path(item.filename).name == "SKILL.md"]
    if not manifests:
        raise ValueError("В архиве нет файла SKILL.md")
    manifest = min(manifests, key=lambda item: item.filename.count("/"))
    root = manifest.filename[: -len("SKILL.md")]

    parsed = _parse(archive.read(manifest).decode("utf-8", errors="replace"))
    name = parsed["meta"].get("name") or (root.strip("/").split("/")[-1] or "skill")
    slug = slugify(name)
    folder = SKILLS_DIR / slug
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)

    saved = 0
    for item in members:
        if item is manifest or not item.filename.startswith(root):
            continue
        relative = item.filename[len(root) :]
        target = (folder / relative).resolve()
        if folder.resolve() not in target.parents:
            continue  # never let an archive write outside its folder
        if item.file_size > MAX_BUNDLE_BYTES or saved >= MAX_BUNDLE_FILES:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(item))
        saved += 1

    skill = _finish_import(
        slug,
        name,
        parsed["meta"].get("description", ""),
        localise(parsed["body"], folder),
        folder,
        force,
    )
    logger.info(
        f"Imported skill {skill['slug']} from archive: "
        f"{len(skill['files'])} file(s), status {skill['compatibility']['status']}"
    )
    return skill


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
    client: httpx.AsyncClient, url: str, force: bool = False
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
    slug = slugify(name)
    folder = SKILLS_DIR / slug

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
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload.content)
            saved.append(relative)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(f"Skipped {item['path']}: {exc}")

    skill = _finish_import(
        slug,
        name,
        parsed["meta"].get("description", ""),
        localise(parsed["body"], folder),
        folder,
        force,
    )
    logger.info(
        f"Imported skill {skill['slug']} from {owner}/{repo}: "
        f"{len(skill['files'])} file(s), status {skill['compatibility']['status']}"
    )
    return skill


async def import_from_url(url: str, force: bool = False) -> Dict[str, Any]:
    """Fetch a skill published on GitHub (or any raw markdown URL)."""
    errors = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        whole = await _import_folder(client, url, force)
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
            slug = slugify(name)
            folder = SKILLS_DIR / slug
            folder.mkdir(parents=True, exist_ok=True)
            skill = _finish_import(
                slug,
                name,
                parsed["meta"].get("description", ""),
                localise(parsed["body"], folder),
                folder,
                force,
            )
            # warn when the text leans on files we did not bring along
            if re.search(r"(scripts|references|assets|templates)/", skill["body"]):
                skill["warning"] = (
                    "Навык ссылается на свои вспомогательные файлы, но скачан только SKILL.md. "
                    "Попробуйте импортировать ссылку на папку навыка на github.com."
                )
            return skill
    raise ValueError("Не удалось скачать навык. " + "; ".join(errors[:3]))
