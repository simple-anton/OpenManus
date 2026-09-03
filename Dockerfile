FROM python:3.12-slim

WORKDIR /app/OpenManus

# nodejs/npm are here for MCP servers published on npm, which are run with npx
RUN apt-get update && apt-get install -y --no-install-recommends git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)

COPY . .

RUN uv pip install --system -r requirements.txt

CMD ["bash"]
