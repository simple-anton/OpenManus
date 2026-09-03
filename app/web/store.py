"""A catalogue of skills published on GitHub.

Skills live in repositories as `<path>/SKILL.md` with a name and description in
their front matter. This module walks the repositories it is told about, keeps
what it finds in a cache next to the config, and can rank the catalogue against
a task described in plain language using the configured model.

GitHub is read without credentials by default, which is enough for browsing but
rate-limited to 60 requests an hour per address. A personal access token, if the
user adds one, raises that and unlocks code search.
"""

import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import PROJECT_ROOT
from app.llm import LLM
from app.logger import logger
from app.web import skills as skills_store


CACHE_PATH = PROJECT_ROOT / "config" / "store_cache.json"
SOURCES_PATH = PROJECT_ROOT / "config" / "store_sources.json"
# the importer already knows these hosts; keep one source of truth
GITHUB_API = skills_store.GITHUB_API
RAW_HOST = skills_store.RAW_HOST

# repositories the catalogue starts with; the user can add more in the interface
DEFAULT_SOURCES = ["anthropics/skills"]
CACHE_TTL = 6 * 3600
MAX_SKILLS_PER_REPO = 60
MAX_CANDIDATES_FOR_MODEL = 40


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = token_value()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def token_value() -> str:
    """The GitHub token from the config, if the user added one."""
    section = {}
    try:  # the store section is optional and not part of AppConfig
        import tomllib

        path = PROJECT_ROOT / "config" / "config.toml"
        if path.exists():
            with path.open("rb") as handle:
                section = tomllib.load(handle).get("store", {})
    except Exception:  # pragma: no cover - a broken config must not break the store
        section = {}
    return (section.get("github_token") or "").strip()


def read_sources() -> List[str]:
    if SOURCES_PATH.is_file():
        try:
            stored = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, list) and stored:
                return stored
        except (json.JSONDecodeError, OSError):
            pass
    return list(DEFAULT_SOURCES)


def write_sources(sources: List[str]) -> None:
    cleaned = []
    for item in sources:
        item = item.strip().rstrip("/")
        match = re.search(r"([\w.-]+)/([\w.-]+)$", item)
        if not match:
            raise ValueError(f"«{item}» не похоже на репозиторий вида владелец/имя")
        cleaned.append(f"{match.group(1)}/{match.group(2)}")
    SOURCES_PATH.write_text(
        json.dumps(sorted(set(cleaned)), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_cache() -> Dict[str, Any]:
    if not CACHE_PATH.is_file():
        return {"updated": 0, "skills": [], "errors": []}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"updated": 0, "skills": [], "errors": []}


def write_cache(payload: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"Could not cache the skill catalogue: {exc}")


def _front_matter(text: str) -> Dict[str, str]:
    """The header of a published SKILL.md, read the same way the importer does."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    return skills_store.parse_front_matter(parts[1]) if len(parts) == 3 else {}


async def _repo_skills(
    client: httpx.AsyncClient, repo: str, errors: List[str]
) -> List[Dict[str, Any]]:
    """Every SKILL.md in one repository, with what its front matter says."""
    try:
        info = await client.get(
            f"{skills_store.GITHUB_API}/repos/{repo}", headers=_headers()
        )
        if info.status_code != 200:
            errors.append(f"{repo}: HTTP {info.status_code} при чтении репозитория")
            return []
        meta = info.json()
        branch = meta.get("default_branch", "main")
        tree = await client.get(
            f"{skills_store.GITHUB_API}/repos/{repo}/git/trees/{branch}?recursive=1",
            headers=_headers(),
        )
        if tree.status_code != 200:
            errors.append(f"{repo}: HTTP {tree.status_code} при чтении дерева файлов")
            return []
        blobs = tree.json().get("tree", [])
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"{repo}: {exc}")
        return []

    manifests = [
        item["path"]
        for item in blobs
        if item.get("type") == "blob" and item["path"].endswith("SKILL.md")
    ][:MAX_SKILLS_PER_REPO]

    found = []
    for path in manifests:
        try:
            payload = await client.get(
                f"{skills_store.RAW_HOST}/{repo}/{branch}/{path}"
            )
            if payload.status_code != 200:
                continue
        except httpx.HTTPError:
            continue
        text = payload.text
        meta_fields = _front_matter(text)
        folder = path[: -len("/SKILL.md")] if "/" in path else ""
        found.append(
            {
                "name": meta_fields.get("name") or (folder.split("/")[-1] or repo),
                "description": meta_fields.get("description", "")[:600],
                "repo": repo,
                "path": folder,
                "url": (
                    f"https://github.com/{repo}/tree/{branch}/{folder}"
                    if folder
                    else f"https://github.com/{repo}"
                ),
                "stars": meta.get("stargazers_count", 0),
                "updated": meta.get("pushed_at", ""),
                "owner": repo.split("/")[0],
                # a rough read of what the skill carries, for the ranking prompt
                "size": len(text),
                "has_scripts": "scripts/" in text,
            }
        )
    return found


async def refresh(sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """Re-read every source repository and rebuild the cache."""
    sources = sources or read_sources()
    errors: List[str] = []
    skills: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for repo in sources:
            skills.extend(await _repo_skills(client, repo, errors))
    payload = {
        "updated": time.time(),
        "sources": sources,
        "skills": sorted(skills, key=lambda item: (-item["stars"], item["name"])),
        "errors": errors,
    }
    write_cache(payload)
    logger.info(
        f"Skill catalogue refreshed: {len(skills)} skill(s), {len(errors)} error(s)"
    )
    return payload


async def catalogue(force: bool = False) -> Dict[str, Any]:
    cached = read_cache()
    if (
        force
        or not cached.get("skills")
        or time.time() - cached.get("updated", 0) > CACHE_TTL
    ):
        try:
            return await refresh()
        except Exception as exc:  # keep serving the old cache if the refresh fails
            logger.warning(f"Catalogue refresh failed: {exc}")
            cached.setdefault("errors", []).append(str(exc))
    return cached


def filter_skills(skills: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    query = query.strip().lower()
    if not query:
        return skills
    words = [word for word in re.split(r"\W+", query) if len(word) > 2]

    def score(skill: Dict[str, Any]) -> int:
        text = f"{skill['name']} {skill['description']} {skill['repo']}".lower()
        return sum(text.count(word) for word in words) + (5 if query in text else 0)

    scored = [(score(skill), skill) for skill in skills]
    return [skill for value, skill in sorted(scored, key=lambda p: -p[0]) if value > 0]


RANK_PROMPT = """Ты помогаешь подобрать навыки для ИИ-агента под задачу пользователя.

Задача пользователя:
{task}

Доступные навыки (номер, название, описание):
{candidates}

Выбери от одного до пяти самых подходящих. Ответь ТОЛЬКО массивом JSON, без пояснений вокруг:
[{{"n": номер, "why": "почему подходит именно для этой задачи, одно-два предложения по-русски"}}]
Если ничего не подходит, ответь []."""


async def recommend(task: str, limit: int = 5) -> Dict[str, Any]:
    """Rank the catalogue against a task, with the model's reasoning."""
    data = await catalogue()
    skills = data.get("skills", [])
    if not skills:
        return {
            "items": [],
            "note": "Каталог пуст: обновите его или добавьте источники.",
        }

    shortlist = filter_skills(skills, task)[:MAX_CANDIDATES_FOR_MODEL]
    if not shortlist:
        shortlist = skills[:MAX_CANDIDATES_FOR_MODEL]

    listing = "\n".join(
        f"{index + 1}. {item['name']} — {item['description'][:200]}"
        for index, item in enumerate(shortlist)
    )
    try:
        answer = await LLM().ask(
            messages=[
                {
                    "role": "user",
                    "content": RANK_PROMPT.format(task=task, candidates=listing),
                }
            ],
            stream=False,
        )
    except Exception as exc:
        logger.warning(f"Ranking by the model failed: {exc}")
        return {
            "items": [
                dict(item, why="Подобрано по совпадению слов: модель не ответила.")
                for item in shortlist[:limit]
            ],
            "note": f"Модель не смогла оценить список ({type(exc).__name__}), "
            "поэтому показан отбор по ключевым словам.",
        }

    picks = _parse_picks(answer, len(shortlist))
    if not picks:
        return {
            "items": [
                dict(
                    item,
                    why="Подобрано по совпадению слов: ответ модели не разобрался.",
                )
                for item in shortlist[:limit]
            ],
            "note": "Ответ модели не удалось разобрать — показан отбор по ключевым словам.",
            "raw": answer[:600],
        }

    items = []
    for pick in picks[:limit]:
        skill = shortlist[pick["n"] - 1]
        items.append(dict(skill, why=pick["why"]))
    return {"items": items, "note": ""}


def _parse_picks(answer: str, count: int) -> List[Dict[str, Any]]:
    """Small models wrap JSON in prose; take the first array that parses."""
    match = re.search(r"\[.*\]", answer, re.S)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    picks = []
    for item in parsed if isinstance(parsed, list) else []:
        try:
            index = int(item["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= index <= count:
            picks.append({"n": index, "why": str(item.get("why", ""))[:400]})
    return picks
