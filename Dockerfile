# syntax=docker/dockerfile:1

# The UI is built first and copied in, so the image serves it from the same origin as the
# API. That is not packaging convenience: the session is an httponly, samesite=lax cookie,
# and a lax cookie is not sent cross-origin, so a separately hosted UI could not log in at
# all. See app/api/web.py.
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
# The client types are generated from the committed contract, so the contract is copied in
# rather than the types being trusted: a stale schema.d.ts fails the build here.
COPY openapi.json /openapi.json
COPY web ./
RUN npx openapi-typescript /openapi.json -o src/api/schema.d.ts \
    && npm run typecheck \
    && npx vite build


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
COPY --from=web --chown=10001:0 /web/dist /src/web/dist
ENV PATH="/src/.venv/bin:$PATH"
USER 10001
WORKDIR /src
EXPOSE 8000

# One worker. An Apply is in-process state -- the runner holds the task and the
# Candidate lock -- so a second worker would run a second pipeline against the
# same Cluster. §22.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
