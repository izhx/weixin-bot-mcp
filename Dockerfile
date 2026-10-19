FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:/opt/tunnel:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/data/home
WORKDIR /app
LABEL org.opencontainers.image.title="weixin-bot-mcp"
ARG HTTP_PROXY
ARG HTTPS_PROXY
RUN pip install --no-cache-dir uv==0.12.1
COPY pyproject.toml uv.lock README.md LICENSE NOTICE.txt ./
COPY licenses ./licenses
COPY wechat_dot_bridge ./wechat_dot_bridge
RUN uv sync --locked --no-dev --no-editable --extra setup
COPY .cache/tunnel/ /opt/tunnel/
COPY docker ./docker
COPY docker/weixin-mcp /usr/local/bin/weixin-mcp
COPY scripts ./scripts
COPY tests ./tests
RUN groupadd -g 1000 bridge && useradd -u 1000 -g bridge -M bridge \
    && chmod -R a+rX /app /opt/tunnel && chmod 755 /usr/local/bin/weixin-mcp
USER 1000:1000
ENTRYPOINT ["/app/.venv/bin/python", "/app/docker/runner.py"]
CMD []

FROM runtime AS test
USER root
ARG HTTP_PROXY
ARG HTTPS_PROXY
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; fi \
    && apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
CMD ["test"]

FROM runtime AS final
