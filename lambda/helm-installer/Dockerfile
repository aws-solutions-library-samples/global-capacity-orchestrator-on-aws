# Helm Installer Lambda
# Installs Helm charts (KEDA, NVIDIA DRA Driver, etc.) to EKS clusters
# Note: This Lambda runs on x86_64 (AMD64) because, in testing, the ARM64
# Lambda base image had issues with the Helm/kubectl binary entrypoint.
# This Lambda only runs during stack deploys, so cost savings from ARM64
# would be negligible.
FROM public.ecr.aws/lambda/python:3.14

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR ${LAMBDA_TASK_ROOT}

# Apply Amazon Linux 2023 security patches at build time. The AL2023 Lambda
# base pins a dated releasever, so security-fixed packages (util-linux,
# sqlite-libs, libsolv, openssl, ...) land only in a newer releasever and are
# invisible to a plain `dnf upgrade`. Float to `--releasever=latest` to pull
# them — the AL2023 analogue of the `apt-get upgrade` the Debian service images
# run. DNF_SECURITY_EPOCH busts the CI layer cache so a freshly-published patch
# isn't masked by a stale cached upgrade layer; bump the date when a new fix
# needs pulling.
ARG DNF_SECURITY_EPOCH=2026-07-08
# hadolint ignore=DL3041
RUN echo "dnf security epoch ${DNF_SECURITY_EPOCH}" \
    && dnf upgrade -y --releasever=latest \
    && dnf clean all

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
        if pip install --no-cache-dir \
            --retries 10 --timeout 120 --resume-retries 5 \
            -r requirements.txt; then \
            break; \
        fi; \
        if [ "${attempt}" -eq 3 ]; then \
            echo "pip install failed after ${attempt} attempts" >&2; \
            exit 1; \
        fi; \
        echo "pip install attempt ${attempt} failed; retrying in 10s..."; \
        sleep 10; \
    done

# Copy handler code
COPY handler.py .
COPY charts.yaml .

CMD ["handler.lambda_handler"]
