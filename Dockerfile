# Leanarr - seed-aware library shrinker (by H0m3Lab)
# Two helpers for the 4K mode are built from their official sources at pinned tags:
#   vmaf      (Netflix) - picture-quality meter: a 4K result is kept only if it scores high enough
#   dovi_tool (quietvoid) - keeps Dolby Vision metadata across a re-encode
FROM debian:trixie-slim AS vmaf
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git build-essential meson ninja-build nasm xxd \
 && git clone --depth 1 --branch v3.2.1 https://github.com/Netflix/vmaf /src \
 && cd /src/libvmaf \
 && meson setup build --buildtype release -Ddefault_library=static -Dbuilt_in_models=true \
      -Denable_tests=false -Denable_docs=false \
 && ninja -C build \
 && install -D build/tools/vmaf /out/vmaf \
 && install -D -m 644 /src/model/vmaf_4k_v0.6.1.json /out/vmaf_4k_v0.6.1.json

FROM rust:1-slim-trixie AS dovi
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git pkg-config libfontconfig-dev \
 && git clone --depth 1 --branch 2.3.4 https://github.com/quietvoid/dovi_tool /src \
 && cd /src && cargo build --release --locked \
 && install -D target/release/dovi_tool /out/dovi_tool

FROM python:3.12-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix libfontconfig1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=vmaf /out/vmaf /usr/local/bin/vmaf
COPY --from=vmaf /out/vmaf_4k_v0.6.1.json /opt/vmaf/vmaf_4k_v0.6.1.json
COPY --from=dovi /out/dovi_tool /usr/local/bin/dovi_tool
WORKDIR /app
COPY leanarr.py leanarr_worker.py Setup-LeanarrWorker.ps1 /app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8282
HEALTHCHECK --interval=60s --timeout=10s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8282/health', timeout=5)"
ENTRYPOINT ["python3", "/app/leanarr.py"]
