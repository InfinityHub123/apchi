# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /src
# Dependencies resolve from the lockfile alone, so a source change does not
# re-resolve them. --no-install-project keeps app/ out of this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev


FROM python:3.13-slim-bookworm

# Apchi reaches Kubernetes and Trino over HTTP and holds no state of its own, so
# nothing here needs root.
RUN useradd --create-home --uid 10001 apchi
COPY --from=build --chown=apchi:apchi /src /src
ENV PATH="/src/.venv/bin:$PATH"
USER apchi
WORKDIR /src
EXPOSE 8000

# One worker. An Apply is in-process state -- the runner holds the task and the
# Candidate lock -- so a second worker would run a second pipeline against the
# same Cluster. §22.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
