# FULL sandbox image (trusted task building only: `chh build-suite --docker` exports the real datasets from the
# scikit-learn files bundled here). Agent-written code runs in the derived candidate image with those files removed:
# docker/sandbox-candidate.Dockerfile (STATUS ledger BUNDLED-DATA).
# Build:  docker build -f docker/sandbox.Dockerfile -t custom-harness-sandbox:py312 docker
# CPU-only torch keeps the image small; a CUDA variant comes with the cloud-GPU backend.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        numpy==2.* pandas==2.* scikit-learn==1.* scipy==1.* \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && useradd -u 1000 -m runner

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp HOME=/tmp
USER 1000:1000
WORKDIR /work
