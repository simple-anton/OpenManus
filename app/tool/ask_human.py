from app.tool import BaseTool


class AskHuman(BaseTool):
    """Add a tool to ask human for help."""

    name: str = "ask_human"
    description: str = "Use this tool to ask human for help."
    parameters: str = {
        "type": "object",
        "properties": {
            "inquire": {
                "type": "string",
                "description": "The question you want to ask human.",
            }
        },
        "required": ["inquire"],
    }

    async def execute(self, inquire: str) -> str:
        # В терминале (main.py, run_flow.py) спрашиваем человека через input().
        # Но там, где терминала нет — например, в контейнере веб-интерфейса, где
        # эту версию должны были подменить на браузерную, — input() завис бы
        # навсегда: читать ответ неоткуда. Проверяем, есть ли настоящий ввод, и
        # если нет — не виснем, а честно говорим агенту, что спросить не вышло.
        import sys

        stdin = getattr(sys, "stdin", None)
        try:
            interactive = bool(stdin) and stdin.isatty()
        except (ValueError, OSError):  # закрытый или подменённый stdin
            interactive = False
        if not interactive:
            return (
                "No interactive terminal is available to ask the human here. "
                "Continue on your own and state plainly which assumption you made."
            )
        return input(f"""Bot: {inquire}\n\nYou: """).strip()
