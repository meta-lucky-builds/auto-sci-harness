# TeX compile image for outputs/latex.py (NOT BUILT; owner decision on size: docs/lab_compute.md "TeX image").
# Build (downloads at BUILD time only; compiles later run with --network none and tectonic --only-cached):
#   docker build -f docker/sandbox-tex.Dockerfile -t custom-harness-tex:tectonic docker
# The tectonic version is pinned; verify the release asset's SHA-256 against the project's published checksum and
# put it below before building (left as an owner step on purpose: no checksum is invented here).
FROM debian:bookworm-slim

ARG TECTONIC_VERSION=0.15.0
ARG TECTONIC_SHA256=REPLACE_WITH_PUBLISHED_SHA256
RUN set -eu; apt-get update; apt-get install -y --no-install-recommends ca-certificates curl fontconfig; \
    curl -fsSL -o /tmp/t.tar.gz \
      "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz"; \
    echo "${TECTONIC_SHA256}  /tmp/t.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/t.tar.gz -C /usr/local/bin tectonic; rm /tmp/t.tar.gz; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*; \
    useradd -u 1000 -m runner

# Warm the bundle cache with the packages our templates and the vendored TikZ preamble use, so a network-free
# compile with --only-cached finds them. The cache is part of the read-only image.
ENV XDG_CACHE_HOME=/opt/tex-cache
COPY tex-warm.tex /tmp/warm/warm.tex
RUN set -eu; mkdir -p /opt/tex-cache; cd /tmp/warm; tectonic warm.tex; rm -rf /tmp/warm; \
    chmod -R a+rX /opt/tex-cache

ENV HOME=/tmp openin_any=p openout_any=p shell_escape=f
USER 1000:1000
WORKDIR /work
