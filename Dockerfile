FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE NOTICE MANIFEST.in requirements.txt ./
COPY app/ ./app/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOD_HOST=0.0.0.0 \
    VOD_PORT=8099 \
    VOD_BASE_DIR=/data
RUN groupadd --system --gid 10001 vod \
    && useradd --system --uid 10001 --gid vod --home-dir /data vod \
    && mkdir -p /data /app \
    && chown 10001:10001 /data /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels quark-vod \
    && rm -rf /wheels
WORKDIR /app
USER 10001:10001
EXPOSE 8099
ENTRYPOINT ["quark-vod"]
