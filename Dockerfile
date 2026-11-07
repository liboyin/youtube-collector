# Stages: `base` holds what the service needs at run time; `build` adds git and uv and builds
# the locked venv; `runtime` is what the Compose service runs, `base` plus that venv; and `dev`
# continues from `build` with the locked dev group and an editable project for the dev
# container (selected in .devcontainer/docker-compose.devcontainer.yaml). Every PyPI package,
# dev tools included, is installed here through PYPI_PROXY when it is set, so creating the dev
# container needs no PyPI access. This follows youtube-whisperer's build design; the collector
# needs no CUDA, so it starts from plain Ubuntu. Ubuntu 26.04's distro python3 is Python 3.14
# (DESIGN decision C22). Base images are pinned by tag for readability and by digest so the pin
# is immutable.
FROM ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c AS base

ARG APT_PROXY

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

# The apt/uv steps use the shared build helpers, supplied as the named build context
# `build_common` and pinned in docker-compose.yaml. They are bind-mounted rather than copied:
# they are build-time only, so nothing is left behind in the image. A build that bypasses
# compose must supply the context itself, otherwise BuildKit looks for an image by that name
# on Docker Hub:
#   docker buildx build --target runtime --build-context build_common=<git-url-or-local-clone> .
#
# ffmpeg lets yt-dlp merge separate video and audio streams. Plain Ubuntu has no CA
# certificates, and the helper skips recommended packages, so ca-certificates is listed
# explicitly: without it every HTTPS request, at build and at run time, fails verification.
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/apt_install.sh ca-certificates ffmpeg python3

# Deno is yt-dlp's JavaScript runtime for YouTube. The official binary-only image pins its
# version and content, unlike the unversioned install script it replaces.
COPY --from=denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 /deno /usr/local/bin/deno
RUN deno --version

# The venv and the project directory belong to the runtime user from the start, so the dev
# container's `uv sync` (update-dependencies.sh) can update the venv without a recursive
# chown, which would copy the whole venv into another layer. The database and staging
# directories are mount points of named volumes (the dev container mounts both as volumes);
# Docker initializes a new volume from the image's directory, ownership included, so the
# runtime user can write there.
RUN install -d -o ubuntu -g ubuntu /opt/venv /workspaces/youtube-collector \
    /workspaces/youtube-collector/assets /var/lib/youtube-collector
WORKDIR /workspaces/youtube-collector

FROM base AS build

ARG APT_PROXY
ARG PYPI_PROXY

# git fetches the pathlib-extensions git dependency. It and uv are needed to build the venv
# and in the dev container, not by the service, so they stay out of `runtime`.
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/apt_install.sh git

# uv stays in `dev` because the dev container runs it (`uv sync`, `uv lock`). The tag names
# the version; the digest makes the pin immutable.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

USER ubuntu

# Install pinned Python dependencies before copying the source, so editing source does not
# invalidate this layer and force a full dependency reinstall on every rebuild. The helper
# exports the lock with --frozen, which never checks it against pyproject.toml, so check that
# first: a stale lock fails the build instead of installing outdated pins. --offline keeps the
# check off the network; an up-to-date lock is confirmed from the files alone, and a stale one
# fails with uv's message about being unable to fetch the git dependency offline.
COPY --chown=ubuntu pyproject.toml uv.lock .python-version ./
RUN UV_NO_CACHE=1 uv lock --check --offline
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv --no-emit-project

# Copy the rest of the source and install the project itself. `runtime` copies this venv, so
# the project is installed as a regular package rather than in editable mode: the service
# image then needs neither the source tree nor uv.
COPY --chown=ubuntu . .
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv --no-editable

FROM base AS runtime

# Same path and interpreter as in `build`, so the copied venv works unchanged. Root owns it in
# this stage: the service only reads it.
COPY --from=build /opt/venv /opt/venv
USER ubuntu

FROM build AS dev

ARG APT_PROXY
ARG PYPI_PROXY

# curl runs the native Claude Code and Codex installers and fetches GitHub's SSH host keys in
# .devcontainer/postCreateCommand.sh.
USER root
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/apt_install.sh curl
USER ubuntu

# uv pip sync makes the venv match the export exactly, so this keeps the runtime packages,
# adds the dev group, and replaces the regular project install with an editable one, which
# the dev container's workspace bind mount needs.
RUN --mount=type=bind,from=build_common,target=/build-common \
    /build-common/uv_install.sh /opt/venv --group dev
