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
#
# Runs as any uid, because OpenShift's restricted-v2 assigns one and ignores USER. Its
# documented contract is that the process is in group 0, so the tree belongs to group 0 --
# and is world-readable besides. Apchi writes only to /tmp, through mkstemp (app.trust).
#
# USER is the number, not the name: a pod that sets runAsNonRoot without a runAsUser is
# refused by the kubelet when the image's USER is a name, since a name cannot be proved
# not to be root.
RUN useradd --create-home --uid 10001 --gid 0 apchi
COPY --from=build --chown=10001:0 /src /src
ENV PATH="/src/.venv/bin:$PATH"
USER 10001
WORKDIR /src
EXPOSE 8000

# One worker. An Apply is in-process state -- the runner holds the task and the
# Candidate lock -- so a second worker would run a second pipeline against the
# same Cluster. §22.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
