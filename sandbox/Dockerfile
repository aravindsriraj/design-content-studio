# The agent's computer for stills, charts, PDFs, GIFs and HyperFrames video: the ADK sandbox shim (FastAPI on :8080, from
# google/adk-samples long-horizon-harness) + the stills render toolchain from LangChain's
# design-agent sandbox recipe (Chrome Headless Shell, sharp, puppeteer-core, OFL fonts).
# Agent Runtime Sandboxes need linux/amd64; arm64 builds are for local Docker on Apple Silicon.
#   docker build -t studio-sandbox sandbox/
FROM node:22.23.3-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive DO_NOT_TRACK=1 PUPPETEER_SKIP_DOWNLOAD=1

# Chrome Headless Shell's shared libraries, fontconfig, and what the shim and gates shell out to.
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
      ca-certificates curl unzip procps python3 python3-venv xz-utils ripgrep jq woff2 ffmpeg \
      fontconfig fonts-liberation fonts-noto-color-emoji \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
      libcairo2 libdbus-1-3 libexpat1 libx11-6 libxcb1 libxext6 \
 && rm -rf /var/lib/apt/lists/*

# Chrome Headless Shell, pinned + checksum-verified (same builds as design-agent's recipe).
# amd64: Chrome for Testing 152.0.7977.30. arm64: Playwright chromium headless-shell r1228 (Chrome 149).
RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) url=https://storage.googleapis.com/chrome-for-testing-public/152.0.7977.30/linux64/chrome-headless-shell-linux64.zip; \
             sha=1b150320178ecabe39726bcf3198ebb896ba2b7a07d870db1710914c42d55221; bin=chrome-headless-shell-linux64/chrome-headless-shell; \
             d2sha=5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9 ;; \
      arm64) url=https://cdn.playwright.dev/dbazure/download/playwright/builds/chromium/1228/chromium-headless-shell-linux-arm64.zip; \
             sha=1652929a70f4afb17aca36fce073fb7ed22262d16825be761b0801972f43ac4f; bin=chrome-linux/headless_shell; \
             d2sha=ac2c028697199479acb321db1e3d68caee9f2ba492ed73caa3cd13f3829bf913 ;; \
      *) echo "unsupported arch: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 "$url" -o /tmp/chs.zip; \
    echo "$sha  /tmp/chs.zip" | sha256sum -c -; \
    mkdir -p /opt/design-agent/chrome-headless-shell; \
    unzip -q /tmp/chs.zip -d /opt/design-agent/chrome-headless-shell; rm /tmp/chs.zip; \
    ln -sfn "/opt/design-agent/chrome-headless-shell/$bin" /usr/local/bin/chrome-headless-shell; \
    ! ldd /usr/local/bin/chrome-headless-shell | grep 'not found'; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    curl -fsSL --retry 3 "https://github.com/d2lang/d2/releases/download/v0.9.0/d2-v0.9.0-linux-${arch}.tar.gz" -o /tmp/d2.tgz; \
    echo "$d2sha  /tmp/d2.tgz" | sha256sum -c -; \
    tar -xzf /tmp/d2.tgz -C /tmp; install -m 0755 /tmp/d2-v0.9.0/bin/d2 /usr/local/bin/d2; rm -rf /tmp/d2*; \
    d2 --version

# Open fonts (OFL, see fonts/LICENSES.md). Brand fonts are staged into /workspace/fonts at run time.
COPY fonts/ /usr/local/share/fonts/studio/
RUN printf '%s\n' '<?xml version="1.0"?>' '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">' \
      '<fontconfig><dir>/usr/local/share/fonts/studio</dir><dir>/workspace/fonts</dir></fontconfig>' \
      > /etc/fonts/conf.d/99-studio.conf \
 && fc-cache -f

# Render toolchain at the path the gates expect (toolchain.mjs RENDER_DIR default).
# design-agent's pinned toolchain (package.json + package-lock.json verbatim): sharp, puppeteer-core,
# echarts, gsap, hyperframes. npm ci checks every package against the lock's integrity hashes.
COPY render/package.json render/package-lock.json /opt/design-agent/render/
RUN cd /opt/design-agent/render && npm ci --ignore-scripts --no-audit --no-fund --omit=dev \
 && printf '%s\n' '#!/usr/bin/env bash' \
      'export HYPERFRAMES_BROWSER_PATH=/usr/local/bin/chrome-headless-shell' \
      'export HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1 HYPERFRAMES_NO_UPDATE_CHECK=1' \
      'export HYPERFRAMES_NO_AUTO_INSTALL=1 HYPERFRAMES_SKIP_SKILLS=1 PUPPETEER_SKIP_DOWNLOAD=1' \
      'exec /opt/design-agent/render/node_modules/.bin/hyperframes "$@"' > /usr/local/bin/hyperframes \
 && chmod 0755 /usr/local/bin/hyperframes

# design-agent's probe: fonts resolve, the browser screenshots, HyperFrames lints and renders, ffprobe agrees.
COPY probe/ /opt/design-agent/probe/
RUN /opt/design-agent/probe/probe.sh

# Website capture for brand onboarding; only the capture sandbox (internet on, no credentials) runs it.
COPY capture/ /opt/studio/

# Sandbox shim (Apache-2.0, google/adk-samples).
COPY runtime/ /opt/runtime/
RUN python3 -m venv /opt/runtime/venv \
 && /opt/runtime/venv/bin/pip install --no-cache-dir "fastapi>=0.136.1,<1" "pydantic>=2.12.5,<3" "uvicorn[standard]>=0.47.0" \
 && sed -i 's#exec python #exec /opt/runtime/venv/bin/python #' /opt/runtime/entrypoint.sh \
 && chmod +x /opt/runtime/entrypoint.sh \
 && useradd --create-home --uid 1001 --shell /bin/bash sandbox \
 && mkdir -p /workspace/attachments /workspace/projects /workspace/skills /workspace/tools /workspace/brand /workspace/fonts \
 && chown -R sandbox:sandbox /workspace /opt/runtime

USER sandbox
WORKDIR /workspace
EXPOSE 8080
CMD ["/opt/runtime/entrypoint.sh"]
