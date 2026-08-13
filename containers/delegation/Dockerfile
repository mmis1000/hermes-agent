# syntax=docker/dockerfile:1
FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

RUN groupadd --gid 10001 hermes \
    && useradd --uid 10001 --gid hermes --create-home --shell /bin/bash hermes \
    && install -d -o hermes -g hermes /workspace

USER hermes
WORKDIR /workspace
ENV HOME=/home/hermes \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/bin/bash"]
