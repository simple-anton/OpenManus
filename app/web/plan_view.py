"""План задачи в том виде, в каком его показывает интерфейс.

Планов у нас теперь два происхождения. В режиме «План» список пунктов пишет
планировщик до начала работы, и им же управляет поток. В режиме «Агент» список
ведёт сам агент инструментом `planning`: заводит, отмечает пункты сделанными по
ходу дела. Карточка в ленте у человека одна и та же, поэтому и превращение
плана в события — одно, здесь.

Форма плана внутри `PlanningTool`: словарь с `title`, `steps` и
`step_statuses`. Наружу отдаём то, что нужно карточке: подписи пунктов, их
состояние одним словом и номер текущего.
"""

from typing import Any, Dict, List, Optional, Set


# Состояния планировщика — в слова, которые понимает карточка в браузере.
MARKS = {
    "completed": "done",
    "in_progress": "active",
    "blocked": "blocked",
    "not_started": "waiting",
}

IN_PROGRESS = "in_progress"
NOT_STARTED = "not_started"
COMPLETED = "completed"


def active_plan(tool: Any) -> Optional[Dict[str, Any]]:
    """План, с которым инструмент работает сейчас, если он есть."""
    plans = getattr(tool, "plans", None)
    if not isinstance(plans, dict) or not plans:
        return None
    plan = plans.get(getattr(tool, "_current_plan_id", None))
    return plan if isinstance(plan, dict) else None


def current_index(plan: Dict[str, Any]) -> Optional[int]:
    """Номер пункта, который идёт прямо сейчас.

    Агент не обязан помечать пункт начатым — многие модели сразу ставят
    «сделано». Тогда текущим считаем первый непочатый: именно к нему агент и
    перейдёт.
    """
    statuses = plan.get("step_statuses", [])
    for index, status in enumerate(statuses):
        if status == IN_PROGRESS:
            return index
    for index, status in enumerate(statuses):
        if status == NOT_STARTED:
            return index
    return None


def state_from(
    plan: Optional[Dict[str, Any]],
    active: Optional[int] = None,
    budget: Optional[int] = None,
    partial: Optional[Set[int]] = None,
) -> Optional[Dict[str, Any]]:
    """План как событие `plan_state` для браузера.

    `partial` — пункты, которые сами признались, что сделаны наполовину. У
    планировщика такого состояния нет, всё несорванное он помечает выполненным;
    без этой пометки отчёт по наполовину закрытым пунктам выглядел бы у
    человека полностью зелёным.
    """
    if not plan:
        return None
    partial = partial or set()
    statuses: List[str] = plan.get("step_statuses", [])
    steps: List[Dict[str, str]] = []
    for index, text in enumerate(plan.get("steps", [])):
        status = statuses[index] if index < len(statuses) else NOT_STARTED
        mark = MARKS.get(status, "waiting")
        if mark == "done" and index in partial:
            mark = "partial"
        steps.append({"text": str(text), "status": mark})
    if not steps:
        return None
    return {
        "title": plan.get("title", ""),
        "steps": steps,
        "active": current_index(plan) if active is None else active,
        "budget": budget,
    }
