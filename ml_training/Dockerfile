# ML Training Container Image
# Generic container for ML training pipeline - orchestrated by Kubernetes CronJob
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
# - curl: For AWS CLI installation and health checks
# - awscli: For S3 operations (uploading trained models)
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI for S3 operations
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip -q awscliv2.zip \
    && ./aws/install \
    && rm -rf aws awscliv2.zip

# Install uv package manager for fast Python dependency management
RUN pip install uv

# Copy ML training scripts and dependencies
COPY . /app/

# Install Python dependencies (uv creates .venv automatically)
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"

# Verify installation
RUN uv run python --version \
    && aws --version

# No ENTRYPOINT - container is generic and orchestrated by CronJob
# Default command shows help if run without arguments
CMD ["uv", "run", "python", "-c", "import sys; print('ML Training Container - Run via CronJob pipeline')"]
