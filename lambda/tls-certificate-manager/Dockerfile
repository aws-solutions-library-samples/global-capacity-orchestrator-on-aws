# GCO deployment-local PKI manager. Runs only during bootstrap and scheduled rotation.
FROM public.ecr.aws/lambda/python:3.14

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR ${LAMBDA_TASK_ROOT}

# Pull current Amazon Linux 2023 security fixes. Bump the epoch when a newly
# published base-layer patch must invalidate the container build cache.
ARG DNF_SECURITY_EPOCH=2026-07-28
# hadolint ignore=DL3041
RUN echo "dnf security epoch ${DNF_SECURITY_EPOCH}" \
    && dnf upgrade -y --releasever=latest \
    && dnf clean all

COPY requirements.txt .
RUN for attempt in 1 2 3; do \
        if pip install --no-cache-dir \
            --retries 10 --timeout 120 --resume-retries 5 \
            --requirement requirements.txt \
            --target "${LAMBDA_TASK_ROOT}"; then \
            break; \
        fi; \
        if [ "${attempt}" = "3" ]; then exit 1; fi; \
        sleep "$((attempt * 5))"; \
    done

COPY handler.py .

# Lambda normally supplies a least-privileged runtime user; declare one
# explicitly so the image remains non-root in generic container runtimes too.
USER 1000:1000

CMD ["handler.lambda_handler"]
