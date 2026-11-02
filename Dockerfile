ARG PYTHON_IMAGE=python:3.14-slim-trixie
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.13.6

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    MCP_IMAGE_LOG_DIR=/tmp/mcp-image-logs \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra svg --no-install-project
COPY mcp_image.py ./
RUN uv sync --locked --no-dev --extra svg

USER nobody
ENTRYPOINT ["mcp-image"]
