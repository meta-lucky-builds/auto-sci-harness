# LAB box image (GOAL §17 "Lab and Court"): full-capability exploration for coding agents. NOT the Court sandbox:
# agent-written code that is to be CERTIFIED runs in docker/sandbox-candidate.Dockerfile (no network), never here.
#
# Build (needs network; nothing in the harness builds or pulls this at run time):
#      docker build -f docker/lab.Dockerfile -t custom-harness-lab:py312 docker
# The base image and the agent CLI versions below are pinned here. They were set on 2026-11-02 to the newest release at
# least 14 days old on that day. To move them, change these lines (reviewed versions only; no "latest", no auto-update)
# or pass --build-arg; the build fails if a version is empty.
#
# Static rules checked by custom_harness/lab/walls.py `recipe_problems` (`chh lab check`):
#   no COPY/ADD (the image holds public packages only: no host file, no truth, no secret can ride along),
#   no VOLUME (the only volume is the session workspace, bound at run time), no secret-like ENV/ARG,
#   no `curl | sh` installers, every *_VERSION pinned, ends as a non-root USER.
# No dataset is bundled (no scikit-learn here; an agent may install packages at run time: the Lab has network).
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ARG CLAUDE_CODE_VERSION=2.1.302
ARG CODEX_VERSION=0.163.0
ARG OPENCODE_VERSION=1.19.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential nodejs npm less procps ripgrep jq unzip \
    && rm -rf /var/lib/apt/lists/*

# agent CLIs at the exact versions the owner passed (refuses to build without them)
RUN test -n "$CLAUDE_CODE_VERSION" && test -n "$CODEX_VERSION" && test -n "$OPENCODE_VERSION" \
    && npm install -g --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

RUN useradd -u 1000 -m -d /home/lab -s /bin/bash lab \
    && mkdir -p /workspace && chown lab:lab /workspace

# no telemetry, no error reports, no update checks (each CLI's documented switch; the egress test is the real check)
ENV HOME=/home/lab \
    PATH=/home/lab/.local/bin:/home/lab/.npm-global/bin:$PATH \
    NPM_CONFIG_PREFIX=/home/lab/.npm-global \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NO_UPDATE_NOTIFIER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_BUG_COMMAND=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1

LABEL org.custom-harness.lab="EXPLORATORY" \
      org.custom-harness.lab.note="Lab box: never mounts truth, store or owner secrets; outputs are not findings"

USER 1000:1000
WORKDIR /workspace
# PID 1 is Docker's --init (custom_harness/lab/box.py passes it); the box idles until agents are exec'd in
CMD ["sleep", "infinity"]
