FROM python:3.12-slim

WORKDIR /app/OpenManus

# nodejs/npm are here for MCP servers published on npm, which are run with npx
# chromium: плагин «Браузер» подключается к нему по протоколу отладки.
# Ставим пакет Debian, а не playwright install: он тянет за собой ровно те
# системные библиотеки, которые нужны этой сборке.
# xvfb + x11-utils: виртуальный экран, чтобы браузер шёл обычным режимом, а не
# headless — headless узнаётся защитой сайтов с первого запроса
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl nodejs npm chromium xvfb x11-utils fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)

# The dependencies come before the source: this layer is rebuilt only when
# requirements.txt itself changes, so editing the code no longer reinstalls
# forty packages on every build.
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY . .
# гарантируем право на запуск: бит мог потеряться при выгрузке кода
RUN chmod +x scripts/*.command scripts/*.sh 2>/dev/null || true

# browser-harness ищет браузер здесь, если ему придётся запускать его самому
ENV BH_CHROME_PATH=/usr/bin/chromium

ENTRYPOINT ["/app/OpenManus/scripts/docker-entrypoint.sh"]
CMD ["bash"]
