FROM python:3.12-slim

WORKDIR /app/OpenManus

# nodejs/npm are here for MCP servers published on npm, which are run with npx
RUN apt-get update && apt-get install -y --no-install-recommends git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir uv)

# The dependencies come before the source: this layer is rebuilt only when
# requirements.txt itself changes, so editing the code no longer reinstalls
# forty packages on every build.
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY . .

CMD ["bash"]
