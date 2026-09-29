# EDGEVERDICT_SANDBOX_DOCKERFILE_V3_BUILDDEPS
# EDGEVERDICT_SANDBOX_DOCKERFILE_V4_JQ: jq added for promises probes (shell repos lean on it)
# EDGEVERDICT_SANDBOX_DOCKERFILE_V5_PROMISE_TOOLS: zip/unzip, shellcheck, Mike Farah yq v4
# (checksum-pinned; Debian's "yq" is a different tool with different syntax), uv
# Python version is a build arg so repos that pin requires-python (e.g.
# PostHog's ==3.13.13) get a matching sandbox without editing this file:
#   docker build -f docker/Dockerfile.sandbox \
#     --build-arg PYTHON_VERSION=3.13.13 \
#     -t edgeverdict-sandbox:py3.13 .
# Then run with EDGEVERDICT_SANDBOX_IMAGE=edgeverdict-sandbox:py3.13.
# The default build is the image the demo and the promises probes run on.
ARG PYTHON_VERSION=3.12.11
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG NODE_MAJOR=22
# Compile-from-source deps for python repos whose lock pins packages with no
# wheel for this platform (zstd, polyleven, pywatchman on arm64) or that must
# link against system libraries (xmlsec against libxml2, psycopg2 against
# libpq). The list is PostHog's own build stage, taken verbatim from their
# Dockerfile; it is the common denominator for Django monorepos, and a
# generic image without it fails installs one package at a time.
ARG BUILD_DEPS="build-essential libpq-dev libxmlsec1 libxmlsec1-dev libffi-dev zlib1g-dev pkg-config"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg jq shellcheck tini unzip zip ${BUILD_DEPS} \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && python -m pip install --no-cache-dir pytest uv \
    && corepack enable \
    && rm -rf /var/lib/apt/lists/*

# Mike Farah yq v4, pinned by version AND sha256 (from the release's own
# checksums file). Repos write yq v4 expressions (strenv, tag); Debian's
# python "yq" would run them wrong instead of failing, which is worse.
ARG YQ_VERSION=v4.44.3
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) sum=a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7 ;; \
         arm64) sum=0e7e1524f68d91b3ff9b089872d185940ab0fa020a5a9052046ef10547023156 ;; \
         *) echo "yq: no pinned checksum for $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/yq \
       "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${arch}" \
    && echo "$sum  /usr/local/bin/yq" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/yq \
    && yq --version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "--version"]
