# SPDX-License-Identifier: MIT
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/workspace/.venv

# Node LTS is required by wrangler / pywrangler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /bin/

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

EXPOSE 8787

CMD ["uv", "run", "pywrangler", "dev", "--ip", "0.0.0.0", "--port", "8787"]
