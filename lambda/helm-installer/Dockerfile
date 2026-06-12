# Helm Installer Lambda
# Installs Helm charts (KEDA, NVIDIA DRA Driver, etc.) to EKS clusters
# Note: This Lambda runs on x86_64 (AMD64) because, in testing, the ARM64
# Lambda base image had issues with the Helm/kubectl binary entrypoint.
# This Lambda only runs during stack deploys, so cost savings from ARM64
# would be negligible.
FROM public.ecr.aws/lambda/python:3.14

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR ${LAMBDA_TASK_ROOT}

# Install Helm
# hadolint ignore=DL3041
RUN dnf install -y tar gzip && \
    curl -fsSL https://get.helm.sh/helm-v4.2.1-linux-amd64.tar.gz | tar xz && \
    mv linux-amd64/helm /usr/local/bin/helm && \
    rm -rf linux-amd64 && \
    dnf clean all

# Install kubectl for cluster connectivity verification.
RUN curl -LO "https://dl.k8s.io/release/v1.36.2/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && \
    mv kubectl /usr/local/bin/kubectl

# Install Python dependencies.
#
# pip flags + bash retry loop guard against transient PyPI/CDN drops
# (see Dockerfile.dev for the rationale).
COPY requirements.txt .
RUN for attempt in 1 2 3; do \
        pip install --no-cache-dir \
            --retries 10 --timeout 120 --resume-retries 5 \
            -r requirements.txt && break; \
        echo "pip install attempt ${attempt} failed; retrying in 10s..."; \
        sleep 10; \
    done

# Copy handler code
COPY handler.py .
COPY charts.yaml .

CMD ["handler.lambda_handler"]
