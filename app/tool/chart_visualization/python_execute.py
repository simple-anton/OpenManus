import copy
from typing import Dict, Optional

from app.config import config
from app.tool.python_execute import PythonExecute


class NormalPythonExecute(PythonExecute):
    """A tool for executing Python code with timeout and safety restrictions."""

    name: str = "python_execute"
    description: str = (
        """Execute Python code for in-depth data analysis / data report(task conclusion) / other normal task without direct visualization."""
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "code_type": {
                "description": "code type, data process / data report / others",
                "type": "string",
                "default": "process",
                "enum": ["process", "report", "others"],
            },
            "code": {
                "type": "string",
                "description": """Python code to execute.
# Note
1. The code should generate a comprehensive text-based report containing dataset overview, column details, basic statistics, derived metrics, timeseries comparisons, outliers, and key insights.
2. Use print() for all outputs so the analysis (including sections like 'Dataset Overview' or 'Preprocessing Results') is clearly visible and save it also
3. Save any report / processed files / each analysis result in worksapce directory: {directory}
4. Data reports need to be content-rich, including your overall analysis process and corresponding data visualization.
5. You can invode this tool step-by-step to do data analysis from summary to in-depth with data report saved also""",
            },
        },
        "required": ["code"],
    }

    # Where the generated code should read and write. None means the shared
    # workspace; set it to give one run a folder of its own.
    directory: Optional[str] = None

    @property
    def output_dir(self) -> str:
        return self.directory or str(config.workspace_root)

    def to_param(self) -> Dict:
        """Name the working folder at the moment the tool is described."""
        param = copy.deepcopy(super().to_param())
        properties = param["function"].get("parameters", {}).get("properties", {})
        for spec in properties.values():
            if isinstance(spec.get("description"), str):
                spec["description"] = spec["description"].replace(
                    "{directory}", self.output_dir
                )
        return param

    async def execute(self, code: str, code_type: str | None = None, timeout=5):
        return await super().execute(code, timeout)
