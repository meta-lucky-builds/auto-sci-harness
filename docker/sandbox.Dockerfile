# Sandbox image for agent-written ML/DL experiment code.
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
