"""The planning flow, wired to a browser session.

Upstream's flow keeps the plan to itself: the steps and their statuses live in
the planning tool and only ever surface in the log. The interface needs them as
they change, so this subclass publishes the plan after it is written and around
every step it runs.
"""

from typing import Any, Dict, Optional

from app.agent.base import BaseAgent
from app.flow.planning import PlanningFlow
from app.web import plan_view


class WebPlanningFlow(PlanningFlow):
    """A planning flow that reports the plan and its progress to the session."""

    session: Any = None

    async def _create_initial_plan(self, request: str) -> None:
        await super()._create_initial_plan(request)
        self.publish_plan()

    async def _execute_step(self, executor: BaseAgent, step_info: dict) -> str:
        # publish before and after: the step turns "in progress", then done
        self.publish_plan()
        try:
            return await super()._execute_step(executor, step_info)
        finally:
            self.publish_plan()

    def plan_state(self) -> Optional[Dict[str, Any]]:
        """The plan as the browser needs it: steps, statuses, where we are."""
        plan = getattr(self.planning_tool, "plans", {}).get(self.active_plan_id)
        # Шаг, который сам признался «сделано частично», в плане всё равно
        # помечается completed: другого статуса у планировщика нет. Но человеку
        # разница важна — иначе отчёт, собранный по наполовину закрытым пунктам,
        # выглядит полностью зелёным. Берём признание шага из своих записей.
        partial = {
            record["index"] - 1
            for record in self.step_records
            if record.get("status") == "partial"
        }
        return plan_view.state_from(
            plan,
            active=self.current_step_index,
            budget=self.step_budget,
            partial=partial,
        )

    def publish_plan(self) -> None:
        state = self.plan_state()
        if state and self.session is not None:
            self.session.publish("plan_state", **state)
