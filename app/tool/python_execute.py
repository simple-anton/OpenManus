import multiprocessing
import sys
from io import StringIO
from typing import Dict

from app.logger import logger
from app.tool.base import BaseTool

# Сколько секунд даётся коду агента. Пять — столько стояло здесь изначально —
# хватало на арифметику и ни на что больше: только импорт pandas и matplotlib
# занимает около 1,4 с, чтение таблицы на 20 000 строк — ещё секунду, и
# обычный «прочитать файл, посчитать, нарисовать график» упирался в предел и
# возвращал «Execution timeout» вместо результата. Тридцать секунд оставляют
# запас на настоящую работу и всё ещё не дают зациклившемуся коду висеть.
TIMEOUT = 30

# Потолок памяти на один запуск кода. Не изоляция, а предохранитель от аварии:
# зациклившийся или ошибочный код (создать массив на десятки гигабайт, утечь
# память) упрётся в MemoryError в своём процессе, а не выест всю память и не
# уронит контейнер с браузером и другими задачами. Восемь гигабайт — щедро:
# выше любой нормальной работы с таблицами и графиками (замерено: pandas +
# matplotlib укладываются и в два), но ниже «съесть все 32 ГБ машины».
MEM_LIMIT = 8 * 1024**3

# Потолок размера ОДНОГО создаваемого файла — от «пишу в файл, пока не кончится
# диск». Два гигабайта заведомо больше любых таблиц/картинок задачи.
FILE_LIMIT = 2 * 1024**3


def _apply_limits() -> None:
    """Ставит предохранители на процесс с кодом. Только Linux; где нельзя —
    молча без лимитов (например, на машине разработчика не под Linux)."""
    try:
        import resource
        import signal

        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))
        # Превышение размера файла шлёт SIGXFSZ, который по умолчанию убивает
        # процесс. Игнорируем сигнал — тогда запись просто падает ошибкой EFBIG,
        # её ловит try/except ниже, и агент получает внятное сообщение, а не
        # молчаливо убитый процесс.
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    except Exception:  # pragma: no cover - зависит от платформы и прав
        pass


class PythonExecute(BaseTool):
    """A tool for executing Python code with timeout and safety restrictions."""

    name: str = "python_execute"
    description: str = "Executes Python code string. Note: Only print outputs are visible, function return values are not captured. Use print statements to see results."
    parameters: dict = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python code to execute.",
            },
        },
        "required": ["code"],
    }

    def _run_code(self, code: str, result_dict: dict, safe_globals: dict) -> None:
        _apply_limits()

        # Маячок сети: аудит-хук ядра ловит реальные исходящие соединения. Сеть
        # у python_execute намеренно оставлена (запасной путь и способ выявлять
        # пробелы в fetch/browser_exec), но каждый прямой выход в сеть мы
        # помечаем — это сигнал, что специализированного инструмента не хватило.
        # Unix-сокет менеджера multiprocessing идёт мимо: его адрес — строка, а
        # не пара (host, port), поэтому ложных срабатываний нет.
        contacted: list = []

        def _audit(event: str, args) -> None:
            if event == "socket.connect" and len(args) >= 2:
                addr = args[1]
                if isinstance(addr, tuple) and len(addr) >= 2:
                    contacted.append(f"{addr[0]}:{addr[1]}")

        sys.addaudithook(_audit)

        original_stdout = sys.stdout
        try:
            output_buffer = StringIO()
            sys.stdout = output_buffer
            exec(code, safe_globals, safe_globals)
            result_dict["observation"] = output_buffer.getvalue()
            result_dict["success"] = True
        except Exception as e:
            result_dict["observation"] = str(e)
            result_dict["success"] = False
        finally:
            sys.stdout = original_stdout
            if contacted:
                # список кладём один раз, по уже открытому соединению менеджера
                result_dict["network"] = sorted(set(contacted))[:10]

    async def execute(
        self,
        code: str,
        timeout: int = TIMEOUT,
    ) -> Dict:
        """
        Executes the provided Python code with a timeout.

        Args:
            code (str): The Python code to execute.
            timeout (int): Execution timeout in seconds.

        Returns:
            Dict: Contains 'output' with execution output or error message and 'success' status.
        """

        with multiprocessing.Manager() as manager:
            result = manager.dict({"observation": "", "success": False})
            if isinstance(__builtins__, dict):
                safe_globals = {"__builtins__": __builtins__}
            else:
                safe_globals = {"__builtins__": __builtins__.__dict__.copy()}
            proc = multiprocessing.Process(
                target=self._run_code, args=(code, result, safe_globals)
            )
            proc.start()
            proc.join(timeout)

            # timeout process
            if proc.is_alive():
                proc.terminate()
                proc.join(1)
                return {
                    "observation": f"Execution timeout after {timeout} seconds",
                    "success": False,
                }
            return self._with_network_note(dict(result))

    @staticmethod
    def _with_network_note(result: Dict) -> Dict:
        """Если код выходил в сеть — помечаем это и в логах, и в ответе агенту."""
        hosts = result.pop("network", None)
        if hosts:
            joined = ", ".join(hosts)
            logger.warning(f"python_execute вышел в сеть напрямую: {joined}")
            result["observation"] = (result.get("observation") or "") + (
                f"\n\n[⚠ этот код выходил в СЕТЬ напрямую ({joined}). Обычно сеть — "
                "задача fetch и browser_exec; прямой выход отсюда чаще всего значит, "
                "что их не хватило. Если так — лучше доработать fetch/browser, а не "
                "ходить в сеть кодом.]"
            )
        return result
