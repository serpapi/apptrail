FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-install-project \
    && uv build --wheel \
    && uv pip install --python /opt/venv/bin/python --no-deps dist/*.whl

FROM python:3.14-slim
RUN groupadd --gid 10001 apptrail && useradd --uid 10001 --gid apptrail --create-home apptrail \
    && mkdir /data && chown apptrail:apptrail /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" APPTRAIL_DATA_DIR=/data PYTHONUNBUFFERED=1
USER apptrail
VOLUME ["/data"]
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:80/healthz', timeout=4)" || exit 1
ENTRYPOINT ["apptrail"]
CMD ["--host", "0.0.0.0", "--port", "80", "--no-browser"]
