# GCO deployment-local PKI manager. Runs only during bootstrap and scheduled rotation.
# checkov:skip=CKV_DOCKER_2:AWS Lambda invokes the handler and decides liveness
# itself; this image is not a long-running service, so a Docker HEALTHCHECK
# would be dead metadata.
FROM public.ecr.aws/lambda/python:3.14

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR ${LAMBDA_TASK_ROOT}

# Pull current Amazon Linux 2023 security fixes. Bump the epoch when a newly
# published base-layer patch must invalidate the container build cache.
ARG DNF_SECURITY_EPOCH=2026-10-25
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
    done \
    && python -m pip uninstall -y pip
# Nothing installs packages at runtime, so pip is uninstalled above in the
# same layer that finishes using it, as the service images do: installer
# tooling stays out of the deployed attack surface, and pip's vendored
# copies of msgpack, setuptools and friends (pip/_vendor/vendor.txt, which
# the base image ships and no pin of ours controls) stop reaching the
# image's vulnerability scan.

COPY handler.py .

# Lambda normally supplies a least-privileged runtime user; declare one
# explicitly so the image remains non-root in generic container runtimes too.
USER 1000:1000

CMD ["handler.lambda_handler"]
