# Two targets: `runtime` is what the Compose service runs, and `dev` adds the locked dev group
# for the dev container (selected in .devcontainer/docker-compose.devcontainer.yaml). Every
# PyPI package, dev tools included, is installed here through PYPI_PROXY when it is set, so
# creating the dev container needs no PyPI access. This follows youtube-whisperer's build
# design; the collector needs no CUDA, so it starts from plain Ubuntu.
FROM ubuntu:24.04 AS runtime

ARG APT_PROXY
ARG PYPI_PROXY

# The application runs from a uv-managed venv built from the distro's python3; uv never
# downloads an interpreter. The venv lives outside the project directory because the dev
# container bind-mounts the workspace over it, which would hide an in-project .venv.
# UV_PROJECT_ENVIRONMENT points the dev container's `uv sync` (update-dependencies.sh) at the
# same venv.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Install OS packages and pinned Python dependencies first, so editing source does not
# invalidate these layers and force a full dependency reinstall on every rebuild.
#
# The apt/uv steps use the shared build helpers, supplied as the named build context
# `build_common` and pinned in docker-compose.yaml. They are bind-mounted rather than copied:
# they are build-time only, so nothing is left behind in the image. A build that bypasses
# compose must supply the context itself, otherwise BuildKit looks for an image by that name
# on Docker Hub:
#   docker buildx build --target runtime --build-context build_common=<git-url-or-local-clone> .
#
# ffmpeg lets yt-dlp merge separate video and audio streams; git fetches the
# pathlib-extensions git dependency; curl and unzip run the Deno installer. Plain Ubuntu has no
# CA certificates, and the helper skips recommended packages, so ca-certificates is listed
# explicitly: without it every HTTPS download below fails verification.
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/apt_install.sh ca-certificates curl ffmpeg git python3 unzip

# Deno is yt-dlp's JavaScript runtime for YouTube. The pipeline's status is the shell's, so a
# failed download would go unnoticed; checking the installed binary fails the build instead.
RUN export DENO_INSTALL=/usr/local && curl -fsSL https://deno.land/install.sh | sh && deno --version

# uv stays in the image because the dev container runs it (`uv sync`, `uv lock`). The tag
# names the version; the digest makes the pin immutable.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

# The venv and the project directory belong to the runtime user from the start, so the dev
# container's `uv sync` (update-dependencies.sh) can update the venv without a recursive
# chown, which would copy the whole venv into another layer. The database directory is the
# mount point of a named volume; Docker initializes a new volume from it, ownership included,
# so the service can write its SQLite file there.
RUN install -d -o ubuntu -g ubuntu /opt/venv /workspaces/youtube-collector /var/lib/youtube-collector
USER ubuntu
WORKDIR /workspaces/youtube-collector

COPY --chown=ubuntu pyproject.toml uv.lock .python-version ./
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv --no-emit-project

# Copy the rest of the source and install the project itself in editable mode.
COPY --chown=ubuntu . .
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv

FROM runtime AS dev

# Build args are scoped to the stage that declares them.
ARG PYPI_PROXY

# uv pip sync makes the venv match the export exactly, so this keeps the runtime packages and
# the editable project and only adds the dev group.
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv --group dev
