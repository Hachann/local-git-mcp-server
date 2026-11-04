FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# The launcher passes the host user's UID:GID via `docker run --user`, so
# files created in the bind mount stay host-owned. Git's ownership check
# has no exemption for any uid, so we whitelist the workspace to avoid
# "dubious ownership" on Linux for any cross-uid mount.
RUN git config --system --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .
# --root-user-action=ignore: pip warns that a root install may break the
# system package manager — irrelevant in a dedicated container, so silence it.
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY server.py .

LABEL maintainer="hachann" \
      org.opencontainers.image.title="local-git-mcp-server"

EXPOSE 8000

# Unbuffered so `docker logs` shows [init] lines immediately
ENV PYTHONUNBUFFERED=1

CMD ["python", "server.py"]
