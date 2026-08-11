# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/playwright:v1.55.0-noble@sha256:ffc33305f7b4b04057ae4a0caa70aad4fde87454fb403a1a22e7f931707dfcf9

USER root
RUN npm install --global --ignore-scripts playwright@1.55.0 \
    && install -d -o pwuser -g pwuser /workspace /home/pwuser/downloads /home/pwuser/traces

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NODE_PATH=/usr/lib/node_modules \
    HOME=/home/pwuser \
    LANG=C.UTF-8
USER pwuser
WORKDIR /workspace

CMD ["/bin/bash"]
