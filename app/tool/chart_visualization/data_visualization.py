import asyncio
import json
import os
from typing import Any, Hashable, Optional

import pandas as pd
from pydantic import Field, model_validator

from app.config import config
from app.llm import LLM
from app.logger import logger
from app.tool.base import BaseTool

# Рисование через VMind — отдельная программа на TypeScript в папке src рядом.
# Чтобы она запустилась, рядом же должны лежать её зависимости: node_modules,
# 475 пакетов, среди них puppeteer со вторым Chromium. В этот образ они не
# ставятся — вес и время сборки не окупаются одним инструментом. Без них
# `npx ts-node` падает ошибкой компилятора TypeScript
# («Cannot find name 'path'» и ещё десяток строк), по которой ни модель, ни
# человек не догадаются, в чём дело. Поэтому проверяем заранее и отвечаем
# словами, а заодно говорим, чем рисовать вместо этого.
VMIND_DIR = os.path.dirname(__file__)

DRAW_INSTEAD = """Постройте график сами через python_execute и matplotlib — он
установлен в образе. Как надо:
1. Прочитайте CSV, на который указывает csvFilePath (pandas.read_csv).
2. `import matplotlib; matplotlib.use("Agg")` — экрана в контейнере нет.
3. Подпишите обе оси с единицами измерения, дайте заголовок и укажите под
   графиком источник данных и его дату.
4. Сохраните картинку в {directory} как .png (dpi=120) и положите рядом сам
   скрипт .py — чтобы график можно было перестроить, а не только посмотреть.
5. Назовите в ответе полный путь к сохранённому файлу."""

NO_VMIND = (
    "Рисование через VMind в этой сборке не установлено: рядом с "
    "chartVisualize.ts нет папки node_modules с его зависимостями Node.js.\n"
    + DRAW_INSTEAD
)


def vmind_ready() -> bool:
    """Стоят ли зависимости Node.js, без которых рисовалка не запустится."""
    return os.path.isdir(os.path.join(VMIND_DIR, "node_modules"))


# Сколько последних строк вывода Node оставлять в ответе. Компилятор
# TypeScript выдаёт десятки строк подряд, и полезное в них — последние.
NODE_TAIL = 10


def _node_error(stderr: str, directory: str) -> str:
    """Ошибку рисовалки — коротко и с указанием, чем рисовать вместо неё."""
    lines = [line for line in (stderr or "").splitlines() if line.strip()]
    tail = "\n".join(lines[-NODE_TAIL:]) or "программа ничего не сказала"
    return (
        f"Рисовалка VMind не отработала. Последнее, что она сказала:\n{tail}\n"
        + DRAW_INSTEAD.format(directory=directory)
    )


class DataVisualization(BaseTool):
    name: str = "data_visualization"
    description: str = """Visualize statistical chart or Add insights in chart with JSON info from visualization_preparation tool. You can do steps as follows:
1. Visualize statistical chart
2. Choose insights into chart based on step 1 (Optional)
Outputs:
1. Charts (png/html)
2. Charts Insights (.md)(Optional)"""
    parameters: dict = {
        "type": "object",
        "properties": {
            "json_path": {
                "type": "string",
                "description": """file path of json info with ".json" in the end""",
            },
            "output_type": {
                "description": "Rendering format (html=interactive)",
                "type": "string",
                "default": "html",
                "enum": ["png", "html"],
            },
            "tool_type": {
                "description": "visualize chart or add insights",
                "type": "string",
                "default": "visualization",
                "enum": ["visualization", "insight"],
            },
            "language": {
                "description": "english(en) / chinese(zh)",
                "type": "string",
                "default": "en",
                "enum": ["zh", "en"],
            },
        },
        "required": ["code"],
    }
    llm: LLM = Field(default_factory=LLM, description="Language model instance")
    # Where charts and their data live. None means the shared workspace.
    directory: Optional[str] = None

    @model_validator(mode="after")
    def initialize_llm(self):
        """Initialize llm with default settings if not provided."""
        if self.llm is None or not isinstance(self.llm, LLM):
            self.llm = LLM(config_name=self.name.lower())
        return self

    @property
    def output_dir(self) -> str:
        return self.directory or str(config.workspace_root)

    def get_file_path(
        self,
        json_info: list[dict[str, str]],
        path_str: str,
        directory: str = None,
    ) -> list[str]:
        res = []
        for item in json_info:
            if os.path.exists(item[path_str]):
                res.append(item[path_str])
            elif os.path.exists(
                os.path.join(f"{directory or self.output_dir}", item[path_str])
            ):
                res.append(
                    os.path.join(f"{directory or self.output_dir}", item[path_str])
                )
            else:
                raise Exception(f"No such file or directory: {item[path_str]}")
        return res

    def success_output_template(self, result: list[dict[str, str]]) -> str:
        content = ""
        if len(result) == 0:
            return "Is EMPTY!"
        for item in result:
            content += f"""## {item['title']}\nChart saved in: {item['chart_path']}"""
            if "insight_path" in item and item["insight_path"] and "insight_md" in item:
                content += "\n" + item["insight_md"]
            else:
                content += "\n"
        return f"Chart Generated Successful!\n{content}"

    async def data_visualization(
        self, json_info: list[dict[str, str]], output_type: str, language: str
    ) -> str:
        data_list = []
        csv_file_path = self.get_file_path(json_info, "csvFilePath")
        for index, item in enumerate(json_info):
            df = pd.read_csv(csv_file_path[index], encoding="utf-8")
            df = df.astype(object)
            df = df.where(pd.notnull(df), None)
            data_dict_list = df.to_json(orient="records", force_ascii=False)

            data_list.append(
                {
                    "file_name": os.path.basename(csv_file_path[index]).replace(
                        ".csv", ""
                    ),
                    "dict_data": data_dict_list,
                    "chartTitle": item["chartTitle"],
                }
            )
        tasks = [
            self.invoke_vmind(
                dict_data=item["dict_data"],
                chart_description=item["chartTitle"],
                file_name=item["file_name"],
                output_type=output_type,
                task_type="visualization",
                language=language,
            )
            for item in data_list
        ]

        results = await asyncio.gather(*tasks)
        error_list = []
        success_list = []
        for index, result in enumerate(results):
            csv_path = csv_file_path[index]
            if "error" in result and "chart_path" not in result:
                error_list.append(f"Error in {csv_path}: {result['error']}")
            else:
                success_list.append(
                    {
                        **result,
                        "title": json_info[index]["chartTitle"],
                    }
                )
        if len(error_list) > 0:
            return {
                "observation": f"# Error chart generated{'\n'.join(error_list)}\n{self.success_output_template(success_list)}",
                "success": False,
            }
        else:
            return {"observation": f"{self.success_output_template(success_list)}"}

    async def add_insighs(
        self, json_info: list[dict[str, str]], output_type: str
    ) -> str:
        data_list = []
        chart_file_path = self.get_file_path(
            json_info, "chartPath", os.path.join(self.output_dir, "visualization")
        )
        for index, item in enumerate(json_info):
            if "insights_id" in item:
                data_list.append(
                    {
                        "file_name": os.path.basename(chart_file_path[index]).replace(
                            f".{output_type}", ""
                        ),
                        "insights_id": item["insights_id"],
                    }
                )
        tasks = [
            self.invoke_vmind(
                insights_id=item["insights_id"],
                file_name=item["file_name"],
                output_type=output_type,
                task_type="insight",
            )
            for item in data_list
        ]
        results = await asyncio.gather(*tasks)
        error_list = []
        success_list = []
        for index, result in enumerate(results):
            chart_path = chart_file_path[index]
            if "error" in result and "chart_path" not in result:
                error_list.append(f"Error in {chart_path}: {result['error']}")
            else:
                success_list.append(chart_path)
        success_template = (
            f"# Charts Update with Insights\n{','.join(success_list)}"
            if len(success_list) > 0
            else ""
        )
        if len(error_list) > 0:
            return {
                "observation": f"# Error in chart insights:{'\n'.join(error_list)}\n{success_template}",
                "success": False,
            }
        else:
            return {"observation": f"{success_template}"}

    async def execute(
        self,
        json_path: str,
        output_type: str | None = "html",
        tool_type: str | None = "visualization",
        language: str | None = "en",
    ) -> str:
        if not vmind_ready():
            logger.warning("📈 VMind не установлен — отвечаем инструкцией")
            return {
                "observation": NO_VMIND.format(directory=self.output_dir),
                "success": False,
            }
        try:
            logger.info(f"📈 data_visualization with {json_path} in: {tool_type} ")
            with open(json_path, "r", encoding="utf-8") as file:
                json_info = json.load(file)
            if tool_type == "visualization":
                return await self.data_visualization(json_info, output_type, language)
            else:
                return await self.add_insighs(json_info, output_type)
        except Exception as e:
            return {
                "observation": f"Error: {e}",
                "success": False,
            }

    async def invoke_vmind(
        self,
        file_name: str,
        output_type: str,
        task_type: str,
        insights_id: list[str] = None,
        dict_data: list[dict[Hashable, Any]] = None,
        chart_description: str = None,
        language: str = "en",
    ):
        llm_config = {
            "base_url": self.llm.base_url,
            "model": self.llm.model,
            "api_key": self.llm.api_key,
        }
        vmind_params = {
            "llm_config": llm_config,
            "user_prompt": chart_description,
            "dataset": dict_data,
            "file_name": file_name,
            "output_type": output_type,
            "insights_id": insights_id,
            "task_type": task_type,
            "directory": self.output_dir,
            "language": language,
        }
        # build async sub process
        process = await asyncio.create_subprocess_exec(
            "npx",
            "ts-node",
            "src/chartVisualize.ts",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(__file__),
        )
        input_json = json.dumps(vmind_params, ensure_ascii=False).encode("utf-8")
        try:
            stdout, stderr = await process.communicate(input_json)
            stdout_str = stdout.decode("utf-8")
            stderr_str = stderr.decode("utf-8")
            if process.returncode == 0:
                return json.loads(stdout_str)
            else:
                return {"error": _node_error(stderr_str, self.output_dir)}
        except Exception as e:
            return {
                "error": f"Не удалось запустить рисовалку: {e}\n"
                + DRAW_INSTEAD.format(directory=self.output_dir)
            }
