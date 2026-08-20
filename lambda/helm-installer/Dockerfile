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
ARG DNF_SECURITY_EPOCH=2026-07-30
# hadolint ignore=DL3041
RUN echo "dnf security epoch ${DNF_SECURITY_EPOCH}" \
    && dnf upgrade -y --releasever=latest \
    && dnf clean all

# Install Helm from the pinned release after authenticating the archive.
# Keep this non-overridable trust anchor synchronized with the exact amd64
# asset named below.
# hadolint ignore=DL3041
RUN dnf install -y tar gzip \
    && curl --fail --show-error --location --retry 3 \
        --output /tmp/helm.tar.gz \
        https://get.helm.sh/helm-v4.2.4-linux-amd64.tar.gz \
    && echo "c306b46f719b0a4da32d0f78ee21bf90ce8d602f15b22ab753f0674d1670a7f3  /tmp/helm.tar.gz" \
        | sha256sum -c - \
    && mkdir -p /tmp/helm-install \
    && tar -xzf /tmp/helm.tar.gz -C /tmp/helm-install \
        --strip-components=1 --no-same-owner linux-amd64/helm \
    && install -m 0755 /tmp/helm-install/helm /usr/local/bin/helm \
    && helm version --short \
    && rm -f /tmp/helm.tar.gz /tmp/helm-install/helm \
    && rmdir /tmp/helm-install \
    && dnf clean all

# Install kubectl for cluster connectivity verification after authenticating
# the exact release binary against a non-overridable trust anchor.
RUN curl --fail --show-error --location --retry 3 \
        --output /tmp/kubectl \
        "https://dl.k8s.io/release/v1.36.3/bin/linux/amd64/kubectl" \
    && echo "ebbd080e7c2e275093b55915722043257eb24004363e20acb3c4d71919f88336  /tmp/kubectl" \
        | sha256sum -c - \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl \
    && kubectl version --client=true

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

# Precompile the handler and its site-packages for cold-start latency: the
# Lambda task root is read-only at invoke time, so any module that reaches
# the runtime uncompiled is re-compiled from source on every cold start.
# PYTHONDONTWRITEBYTECODE documents that runtime bytecode writes are neither
# needed nor possible.
RUN python -m compileall -q "${LAMBDA_TASK_ROOT}"
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["handler.lambda_handler"]
