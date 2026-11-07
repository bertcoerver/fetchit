# syntax=docker/dockerfile:1
#
# Built by .github/workflows/addon.yml, with the *repository root* as the build
# context — not addon/ — so the source in src/ can be copied in directly. Home
# Assistant never builds this: config.yaml names the published image, and an
# update is a pull. To build it by hand, from the repository root:
#
#   docker build -f addon/Dockerfile .
ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.4.0
FROM ${BUILD_FROM}

# The same three the Home Assistant builder passes. BUILD_VERSION is the
# add-on version from config.yaml; it becomes fetchit's own version too.
ARG BUILD_ARCH=aarch64
ARG BUILD_VERSION=0.0.0.dev0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Prepend, never replace: the base image's own s6 services run with this PATH
# too, and one of them needs /usr/sbin.
ENV PATH="/opt/venv/bin:${PATH}" \
    # Python in a container has no one to buffer for, and an add-on log that
    # only appears once a run is over is no use while the run is going.
    PYTHONUNBUFFERED=1

# ffmpeg is for ARTE (an HLS remux, stream-copy only) and for yt-dlp's muxing.
# python3-venv because Debian's Python is externally managed and fetchit has no
# business in the system site-packages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# fetchit and yt-dlp in their own venv, from the source in this checkout — the
# commit the release workflow is building, and nothing fetched from elsewhere.
# There is no .git in the context (see .dockerignore), so setuptools-scm cannot
# read a version from the tags and is told it instead.
COPY pyproject.toml README.md LICENSE /tmp/fetchit/
COPY src /tmp/fetchit/src
RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip; \
    SETUPTOOLS_SCM_PRETEND_VERSION="${BUILD_VERSION}" \
        /opt/venv/bin/pip install --no-cache-dir "/tmp/fetchit[video]"; \
    rm -rf /tmp/fetchit; \
    ln -sf /opt/venv/bin/fetchit /usr/local/bin/fetchit; \
    fetchit --help > /dev/null

COPY addon/rootfs /

# s6 runs these; the build should not depend on the file mode surviving a copy
# through git, Samba and a Windows checkout.
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/fetchit/run \
    /etc/s6-overlay/s6-rc.d/fetchit/finish \
    /usr/local/bin/fetchit-run

LABEL \
    io.hass.name="fetchit" \
    io.hass.description="Download feeds into a media library" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/bertcoerver/fetchit"
