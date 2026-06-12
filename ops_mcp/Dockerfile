# Ops MCP-сервер: python:3.12-slim + статический docker CLI (только клиент, без демона)
FROM python:3.12-slim

# Статический бинарь docker — надёжнее для slim, чем docker.io из apt
# (apt-пакет тянет демон/containerd, которые тут не нужны)
ARG DOCKER_VERSION=27.5.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/ops_mcp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8765

CMD ["python", "server.py"]
