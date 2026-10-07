# Sandbox image for the `lean` adapter (formal claims, tier T0): pinned Lean 4 + pinned Mathlib + the pinned checker.
#
# NOT BUILT in this repository: building it downloads elan, a Lean toolchain and Mathlib (several GB), which is the
# owner's decision (GOAL §12). See docs/adapters_lean.md for the install note. Never verified against a real build.
#
#   docker build -f docker/lean.Dockerfile -t custom-harness-lean:4.19.0 docker
#
# The pins MUST equal custom_harness/kernel/formal.py (LEAN_VERSION, MATHLIB_INPUT_REV, MATHLIB_COMMIT); the Kernel
# rejects every record whose `lean --version` or lake-manifest.json differs. After the first build, copy the
# manifest's mathlib "rev" into MATHLIB_COMMIT (and pass it here as MATHLIB_REV) so the tag can never move under you.
FROM debian:bookworm-slim

ARG LEAN_TOOLCHAIN=leanprover/lean4:v4.19.0
ARG MATHLIB_REV="v4.19.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m runner

# elan (the Lean toolchain manager) into a root-owned, world-readable location; the sandbox user cannot modify it
ENV ELAN_HOME=/opt/elan PATH=/opt/elan/bin:$PATH
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh \
    && sh /tmp/elan-init.sh -y --no-modify-path --default-toolchain "$LEAN_TOOLCHAIN" \
    && rm /tmp/elan-init.sh && chmod -R a+rX /opt/elan

# the project every proof is compiled against: Mathlib at the pinned revision, prebuilt (cache, else built)
WORKDIR /opt/chh-proj
RUN echo "$LEAN_TOOLCHAIN" > lean-toolchain \
    && printf 'name = "chhproj"\n\n[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4"\nrev = "%s"\n' "$MATHLIB_REV" > lakefile.toml \
    && lake update \
    && (lake exe cache get || true) \
    && lake build Mathlib \
    && chmod -R a+rX /opt/chh-proj

# the pinned comparator / axiom auditor (its sha256 is formal.CHECKER_SHA256)
COPY lean/ChhLeanCheck.lean /opt/chh-lean-check/ChhLeanCheck.lean
COPY lean/checker-lakefile.toml /opt/chh-lean-check/lakefile.toml
RUN cd /opt/chh-lean-check && echo "$LEAN_TOOLCHAIN" > lean-toolchain && lake build && chmod -R a+rX /opt/chh-lean-check

ENV HOME=/tmp
USER 1000:1000
WORKDIR /work
