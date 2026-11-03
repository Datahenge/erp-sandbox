# check=skip=InvalidDefaultArgInFrom
#
# The line above is a BuildKit parser directive and must stay first. BASE_IMAGE
# has no default on purpose -- it comes from config.toml through .env -- and
# BuildKit warns about exactly that on every build (decision #58).
#
# The bench container: python, node, wkhtmltopdf, and the bench CLI.
#
# Every version here arrives as a build arg from .env, which render.py wrote
# from config.toml. Nothing is pinned in this file -- a v13 or v14 environment
# is a different config.toml, not a different Dockerfile.
#
#   podman build \
#     --build-arg BASE_IMAGE=... --build-arg PYTHON_VERSION=... (etc, from .env) \
#     -t <project>-bench images/bench
#
# No credential is ever passed to this build. Apps are cloned host-side by
# erp-sandbox fetch and arrive through a bind mount, so no PAT can be
# baked into a layer where `image history` would keep it forever
# (technical.md section 5).

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG PYTHON_VERSION
ARG NODE_VERSION
ARG YARN_VERSION
ARG WKHTMLTOPDF_RELEASE
ARG BENCH_VERSION
ARG BENCH_CLI_PINS
ARG EXTRA_PACKAGES=""
ARG HOST_UID
ARG HOST_GID

# Where the bind-mounted bench tree appears. An architecture constant, like the
# compose service names: the generated nginx config already serves assets from
# /workspace/bench/sites, and both must agree.
ENV BENCH_DIR=/workspace/bench

# The bench CLI lives in its own venv, separate from bench/env -- which is
# bind-mounted, built at Phase 5, and holds the *apps'* dependencies. Mixing
# the two would put the tool that manages the environment inside the
# environment it manages.
ENV BENCH_CLI=/opt/bench-cli

# The sbin directories stay on PATH. Dropping them is an easy and confusing
# mistake: groupadd, useradd and locale-gen all live in /usr/sbin, and the
# build fails with "not found" for a binary that is plainly installed.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    PATH=/opt/bench-cli/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---------------------------------------------------------------------------
# System packages
#
# Grouped by why they are here, because "why is libtiff-dev in a Python image"
# is a question somebody will ask in two years. The list was derived from the
# apps' own requirements.txt, not copied from a general-purpose Frappe image:
#
#   line 3  mariadb-client for restores, redis-tools for debugging, and
#           libpq-dev -- see below
#   line 4  headers for the packages that build from source -- cryptography
#           (libssl, libffi), lxml (libxml2, libxslt), Pillow (the rest)
#   line 5  fonts, or wkhtmltopdf renders a PDF full of empty boxes
#
# libpq-dev is here unconditionally, for a PostgreSQL driver a MariaDB bench
# never loads. Every Frappe version pins psycopg2 something; whether that pin
# resolves to a wheel or to a source build depends on the exact version and the
# interpreter, and gets that wrong quietly. Vanilla v12 pins
# psycopg2-binary==2.8.4, which predates Python 3.9 and has no cp39 wheel, so it
# builds from source and needs pg_config. A fork that bumped the pin to 2.9.1
# gets a wheel and does not. 2.2 MB against an 869 MB image is not worth
# deciding per version -- the cost of guessing wrong is a build that fails
# twenty-five minutes in with an error about PostgreSQL.
#
# Deliberately absent here, and worth recording so nobody adds them back on a
# hunch: frappe v12 pins ldap3 (pure Python, so no libldap2-dev/libsasl2-dev)
# and PyMySQL (pure Python, so no MySQL client headers). v16 does need them
# (it pins mysqlclient) and Pango for WeasyPrint, which is why those arrive
# through EXTRA_PACKAGES from the series entry in known_versions.py rather than
# from this list. What a *series* needs goes there; what an *app* needs goes in
# [stack].extra_packages.
#
# Python itself is installed from Debian only when the base image does not
# already provide the series' version. The official python images (v16's
# python:3.14-slim-bookworm) carry it in /usr/local; Debian's python3 would add
# a second, older interpreter beside it.
# ---------------------------------------------------------------------------
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget git xz-utils procps \
        passwd adduser locales mime-support \
    ; \
    if command -v python3 >/dev/null \
        && [ "$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = "${PYTHON_VERSION}" ]; then \
        echo "base image provides python ${PYTHON_VERSION} at $(command -v python3); not installing Debian's"; \
    else \
        apt-get install -y --no-install-recommends \
            python3 python3-dev python3-venv python3-pip python3-setuptools; \
    fi; \
    apt-get install -y --no-install-recommends \
        build-essential pkg-config \
    ; \
    apt-get install -y --no-install-recommends \
        mariadb-client redis-tools libpq-dev \
    ; \
    apt-get install -y --no-install-recommends \
        libssl-dev libffi-dev \
        libxml2-dev libxslt1-dev \
        zlib1g-dev libjpeg62-turbo-dev libfreetype6-dev \
        liblcms2-dev libwebp-dev libtiff-dev libopenjp2-7-dev \
    ; \
    apt-get install -y --no-install-recommends \
        fonts-liberation xfonts-75dpi xfonts-base \
    ; \
    rm -rf /var/lib/apt/lists/*

# LANG above names a locale that has to exist. Frappe formats currency and dates
# through Python's locale machinery, and an unset locale degrades quietly.
RUN set -eux; \
    sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen; \
    locale-gen en_US.UTF-8

# The base image is a TOML value, so nothing guarantees its Python is the one
# the Frappe series needs. PYTHON_VERSION comes from the series entry in
# known_versions.py, not from config.toml (decision #57). Failing here is far
# cheaper than failing during a dependency install forty minutes later.
RUN set -eux; \
    actual="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"; \
    if [ "$actual" != "${PYTHON_VERSION}" ]; then \
        echo "FATAL: python3 is ${actual}, but this Frappe series needs ${PYTHON_VERSION}." >&2; \
        echo "[stack].base_image provides the wrong Python. Use an image that has" >&2; \
        echo "${PYTHON_VERSION} -- erp-sandbox init --frappe <series> writes one that does." >&2; \
        exit 1; \
    fi; \
    echo "python ${actual} is what the series needs"

# ---------------------------------------------------------------------------
# Node, from the official tarball
#
# Not from apt: Debian's Node is whatever its release ships, not the series'
# version, and the Procfile's socketio line names /usr/local/bin/node
# explicitly, so the binary has to land exactly there.
# ---------------------------------------------------------------------------
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "FATAL: no Node tarball for architecture ${arch}" >&2; exit 1 ;; \
    esac; \
    tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    wget -q "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}" -O /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude CHANGELOG.md --exclude LICENSE --exclude README.md; \
    rm /tmp/node.tar.xz; \
    test -x /usr/local/bin/node; \
    npm install -g "yarn@${YARN_VERSION}"; \
    npm cache clean --force

# ---------------------------------------------------------------------------
# wkhtmltopdf
#
# It MUST be a patched-qt build. The distro package is not: it needs an X
# server, and print formats fail at runtime in a way that looks like a template
# bug. Verified in Phase 0.1b -- installs on bullseye-slim, renders headlessly.
# ---------------------------------------------------------------------------
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"; \
    deb="wkhtmltox_${WKHTMLTOPDF_RELEASE}.${codename}_${arch}.deb"; \
    wget -q "https://github.com/wkhtmltopdf/packaging/releases/download/${WKHTMLTOPDF_RELEASE}/${deb}" \
        -O /tmp/wkhtmltox.deb; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb; \
    rm -rf /tmp/wkhtmltox.deb /var/lib/apt/lists/*; \
    wkhtmltopdf --version | grep -q 'with patched qt' \
        || { echo "FATAL: wkhtmltopdf is not a patched-qt build; PDFs will need an X server." >&2; exit 1; }

# ---------------------------------------------------------------------------
# The bench CLI, pinned, in its own venv
# ---------------------------------------------------------------------------
RUN set -eux; \
    python3 -m venv "${BENCH_CLI}"; \
    # BENCH_CLI_PINS is the series' bench_cli_installer: pip<21 for v12, which
    # cannot run on v16's Python 3.14. Unquoted on purpose: one word per pin.
    "${BENCH_CLI}/bin/pip" install --no-cache-dir --upgrade ${BENCH_CLI_PINS:?} wheel; \
    "${BENCH_CLI}/bin/pip" install --no-cache-dir "frappe-bench==${BENCH_VERSION}"; \
    "${BENCH_CLI}/bin/pip" install --no-cache-dir honcho; \
    ln -sf "${BENCH_CLI}/bin/bench" /usr/local/bin/bench; \
    ln -sf "${BENCH_CLI}/bin/honcho" /usr/local/bin/honcho; \
    bench --version

# ---------------------------------------------------------------------------
# A user matching the host
#
# Decision #25: the container runs as the detected host uid/gid. The passwd
# entry exists so that the uid resolves to a name with a home directory --
# git, pip and bench all misbehave against an unknown uid.
#
# Reuse rather than create when the id is already taken: base images differ,
# and a collision must not fail the build.
# ---------------------------------------------------------------------------
RUN set -eux; \
    if ! getent group "${HOST_GID}" >/dev/null; then \
        groupadd -g "${HOST_GID}" bench; \
    fi; \
    group_name="$(getent group "${HOST_GID}" | cut -d: -f1)"; \
    if ! getent passwd "${HOST_UID}" >/dev/null; then \
        useradd -u "${HOST_UID}" -g "${group_name}" -m -s /bin/bash bench; \
    fi; \
    user_name="$(getent passwd "${HOST_UID}" | cut -d: -f1)"; \
    user_home="$(getent passwd "${HOST_UID}" | cut -d: -f6)"; \
    mkdir -p "${BENCH_DIR}" "${user_home}"; \
    chown -R "${HOST_UID}:${HOST_GID}" "${BENCH_DIR}" "${user_home}"; \
    echo "container user: ${user_name} (${HOST_UID}:${HOST_GID}), home ${user_home}"

# System libraries a particular configuration's apps need in order to build.
# Empty by default and last of the apt layers, so a change here rebuilds only
# this step. The image cannot guess these -- a Frappe app that wraps systemd
# needs libsystemd-dev to build pystemd -- and hardcoding one app's need would
# make the image specific to that app (decision #17). It comes from
# config.toml's [stack].extra_packages.
RUN set -eux; \
    if [ -n "${EXTRA_PACKAGES}" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends ${EXTRA_PACKAGES}; \
        rm -rf /var/lib/apt/lists/*; \
        echo "installed extra packages: ${EXTRA_PACKAGES}"; \
    else \
        echo "no [stack].extra_packages requested"; \
    fi

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# Recorded so `erp-sandbox doctor` can compare a built image against the current
# config.toml and notice that somebody changed a version without rebuilding.
LABEL org.opencontainers.image.title="erp-sandbox bench" \
      erp-sandbox.python="${PYTHON_VERSION}" \
      erp-sandbox.node="${NODE_VERSION}" \
      erp-sandbox.yarn="${YARN_VERSION}" \
      erp-sandbox.wkhtmltopdf="${WKHTMLTOPDF_RELEASE}" \
      erp-sandbox.bench="${BENCH_VERSION}" \
      erp-sandbox.host_uid="${HOST_UID}" \
      erp-sandbox.host_gid="${HOST_GID}"

USER ${HOST_UID}:${HOST_GID}
WORKDIR ${BENCH_DIR}

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["honcho", "start"]
