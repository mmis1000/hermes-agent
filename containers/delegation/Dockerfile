# syntax=docker/dockerfile:1
FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

# Baseline tools for a source-tree worker.  The protected runtime is commonly
# network-disabled, so anything the worker needs to inspect or test a revealed
# repository must already be present in the image.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
        pytest==9.0.2 \
        PyYAML==6.0.3 \
    && git config --system --add safe.directory '*' \
    && groupadd --gid 10001 hermes \
    && useradd --uid 10001 --gid hermes --create-home --shell /bin/bash hermes \
    && install -d -o hermes -g hermes /workspace

USER hermes
WORKDIR /workspace
ENV HOME=/home/hermes \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/bin/bash"]
