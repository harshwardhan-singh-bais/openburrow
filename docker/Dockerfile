# syntax=docker/dockerfile:1.7
#
# OpenBurrow container image.
#
# Three targets, because the three things OpenBurrow runs have genuinely
# different shapes and one image that does all of them would be the largest of
# the three plus the other two's dependencies:
#
#   --target relay   the relay service (Postgres, HTTP, no repo, no harnesses)
#   --target daemon  the burrow daemon (needs git and the harness binaries)
#   --target web     the Next.js frontend
#
# A note on what a container is *for* here. The daemon's whole design assumes it
# owns a repo on a real filesystem and that its control socket's permissions are
# its access control. In a container that means the repo has to be mounted and
# the socket has to be on a volume the client can reach — running the daemon in a
# container without doing that gives you a daemon nobody can talk to. The relay
# is the one component that containerises cleanly, which is why `docker
# compose up` is documented as the *relay* development stack and not as how to
# run OpenBurrow.
#
# ---------------------------------------------------------------------------
# Stage: builder — resolve the workspace into a self-contained venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Pinned, and pinned *to the same resolver that wrote `uv.lock`*. A lockfile and
# the tool that reads it are a matched pair: `uv.lock` carries `revision = 3`,
# and a uv old enough not to know that revision refuses the file rather than
# misreading it. Bumping one without the other is how you get a build that
# starts failing on a Tuesday for no reason anyone can find.
ARG UV_VERSION=0.9.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /usr/local/bin/uv

WORKDIR /app

# Manifests first, sources second. This is the whole point of the split: editing
# a `.py` file must not invalidate the dependency layer, and without this the
# rebuild after every code change re-resolves the entire workspace.
COPY pyproject.toml uv.lock ./
COPY packages/openburrow-core/pyproject.toml packages/openburrow-core/
COPY packages/openburrow-a2a/pyproject.toml packages/openburrow-a2a/
COPY packages/openburrow-acp/pyproject.toml packages/openburrow-acp/
COPY packages/openburrow-adapters/pyproject.toml packages/openburrow-adapters/
COPY packages/openburrow-governance/pyproject.toml packages/openburrow-governance/
COPY packages/openburrow-brain/pyproject.toml packages/openburrow-brain/
COPY packages/openburrow-radar/pyproject.toml packages/openburrow-radar/
COPY packages/openburrow-reel/pyproject.toml packages/openburrow-reel/
COPY packages/openburrow-daemon/pyproject.toml packages/openburrow-daemon/
COPY packages/openburrow-cli/pyproject.toml packages/openburrow-cli/
COPY packages/openburrow-relay/pyproject.toml packages/openburrow-relay/

# `--no-install-workspace` resolves and installs third-party dependencies only.
# The workspace members themselves come in the next step, so this layer is cached
# across every source change.
#
# `--frozen` is the point of copying `uv.lock` at all. Without it uv re-resolves
# from the manifests, which means the image can ship a different dependency set
# than the one anyone tested — the exact failure a lockfile exists to prevent,
# reintroduced by the flag that reads it. The comment here used to say there was
# no `uv.lock` committed; there is, and it had been copied into the layer above
# the whole time, so the build was resolving against it and then ignoring it.
RUN uv sync --frozen --no-dev --no-install-workspace

# Now the sources, and the workspace members on top of the resolved tree.
COPY packages/ packages/
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage: relay — the relay service
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS relay

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    OPENBURROW_RELAY_HOST=0.0.0.0 \
    OPENBURROW_RELAY_PORT=8787

# `curl` is not decoration: the compose healthcheck needs a client, and the
# alternative — a `python -c` one-liner — pulls the whole interpreter up just to
# make one request. `tini` reaps the zombies a containerised server accumulates.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl tini \
    && rm -rf /var/lib/apt/lists/*

# A dedicated user. The relay holds no repo and needs no write access outside its
# own temp directory, so there is no reason for it to be able to write anywhere
# else — and a service that cannot write to its own filesystem is a service that
# is much harder to turn into a persistence mechanism.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 burrow

WORKDIR /app
COPY --from=builder --chown=burrow:burrow /app/.venv /app/.venv

USER burrow
EXPOSE 8787

# Uses `/healthz`, not `/readyz`, and the distinction matters here. `/healthz`
# touches nothing, so it answers "is this process alive". `/readyz` checks
# Postgres, so wiring it to a healthcheck turns a database blip into a container
# restart loop — which is a database blip made worse.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "openburrow-relay"]

# ---------------------------------------------------------------------------
# Stage: daemon — the burrow daemon
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS daemon

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# `git` is not optional — lanes get their own worktree, and a daemon without git
# cannot start a lane at all. `procps` is for `burrow daemon status` to be able
# to tell a live PID from a recycled one.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git procps tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 10002 burrow

WORKDIR /app
COPY --from=builder --chown=burrow:burrow /app/.venv /app/.venv

USER burrow

# The daemon's control socket lives in the repo's runtime directory. Declaring it
# as a volume is what makes it reachable from outside the container — without
# this the daemon starts, binds a socket nobody can see, and looks broken.
VOLUME ["/repo"]
WORKDIR /repo

ENTRYPOINT ["/usr/bin/tini", "--", "burrow"]

# ---------------------------------------------------------------------------
# Stage: web — the Next.js frontend
# ---------------------------------------------------------------------------
FROM node:22-alpine AS web-builder

WORKDIR /app

# Same manifest-first split as the Python side.
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY apps/web/ ./
# Server build, not a static export: the route handlers are what keep the
# daemon's socket path and the relay's origin out of the browser bundle.
RUN npm run build

FROM node:22-alpine AS web

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

RUN addgroup -g 10003 -S burrow && adduser -u 10003 -S burrow -G burrow

WORKDIR /app

COPY --from=web-builder --chown=burrow:burrow /app/.next ./.next
COPY --from=web-builder --chown=burrow:burrow /app/public ./public
COPY --from=web-builder --chown=burrow:burrow /app/node_modules ./node_modules
COPY --from=web-builder --chown=burrow:burrow /app/package.json ./package.json

USER burrow
EXPOSE 3000

# The daemon's socket has to be visible to this process, so it is a volume here
# too. Without it the board renders the "daemon is not answering" panel — which
# is the correct answer to a question nobody meant to ask.
VOLUME ["/repo"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npm", "start"]
