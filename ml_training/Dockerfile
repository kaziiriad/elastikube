# ML Training Container Image
# Generic container for ML training pipeline - orchestrated by Kubernetes CronJob.
#
# Slim production image — copies only the files the CronJob pipeline needs
# (scripts/, utils/, requirements.txt). Notebooks/, docs/, data/, models/
# are dev-only and intentionally excluded.
#
# Pinned to the prod dependency list in requirements.txt. Dev deps
# (jupyter, matplotlib, plotly, pytest, etc.) are in requirements-dev.txt
# and NOT installed in this image.
#
# Multi-stage build:
#   builder  — installs build-essential + pip-installs deps into a venv.
#              Heavy C-extension toolchain (needed for prophet/cmdstan) and
#              pip cache stay here.
#   runtime  — `python:3.11-slim` base. Only the venv + scripts + utils
#              are copied. No build toolchain, no pip, no compiler.
#
# The CronJob pipeline uses boto3 for S3 + CloudWatch, so the AWS CLI
# bundle is NOT installed — that alone saves ~150MB compressed.
# matplotlib / pillow / plotly are also NOT installed (prophet is
# pulled in with --no-deps), saving another ~140MB on the runtime
# image. The pipeline is headless — no charts, no notebooks.

# --- Stage 1: builder ------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# build-essential lets pip compile any C-extension wheels prophet needs
# (cmdstan pulls in C++). gcc, g++, make, libc-dev, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a venv so the runtime stage can copy it as a
# single directory without dragging pip / setuptools / build tooling along.
#
# `requirements-deps.txt` is the prophet-free subset. We install it with
# normal dependency resolution first, then install `prophet` standalone
# with --no-deps so matplotlib / pillow / kiwisolver / fontTools /
# contourpy / mpl_toolkits are NOT pulled in transitively (saves ~140MB).
# Prophet's plotting code paths degrade to a runtime warning; the
# forecasting pipeline (fit/predict/serialize/cross_validation) does not
# need matplotlib.
COPY requirements.txt /app/requirements.txt
COPY requirements-deps.txt /app/requirements-deps.txt
COPY scripts /app/scripts/
COPY utils /app/utils/
COPY setup.py pyproject.toml /app/

RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements-deps.txt \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps prophet \
    && /app/.venv/bin/pip install --no-cache-dir .

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app

# Pull the venv from the builder. The venv includes the python interpreter
# symlinks, so we don't need to copy the system one.
COPY --from=builder /app/.venv /app/.venv

# Pull the application source.
COPY scripts /app/scripts/
COPY utils /app/utils/
COPY requirements.txt /app/requirements.txt

# Put the venv first on PATH so `python` resolves to the venv interpreter
# (which has boto3, prophet, pandas, etc. installed). PYTHONUNBUFFERED keeps
# stdout/stderr streaming so CronJob logs show progress in real time.
ENV PATH="/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/sbin:/bin" \
    PYTHONUNBUFFERED=1

# Sanity check: imports load — confirms the venv is intact and prophet's
# compiled extensions are present. prophet emits a benign "Importing
# matplotlib failed" warning at import time; the model still works.
RUN python -c "import boto3, pandas, numpy, prophet, joblib; print('deps OK')"

# No ENTRYPOINT - container is generic and orchestrated by CronJob.
# Default command shows help if run without arguments.
CMD ["python", "-c", "print('ML Training Container - Run via CronJob pipeline')"]
