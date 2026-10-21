# bdfr serve for an always-on machine: downloads requested by the userscript, plus the weekly Arctic Shift baseline
# for the users in /config/bdfr/watchlist.yaml. See docker-compose.yml.
FROM python:3.12-slim

# ffmpeg merges the best video and audio streams (v.redd.it, yt-dlp sites)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY bdfr ./bdfr
RUN pip install --no-cache-dir . && rm -rf /src

# BDFR's configuration (default_config.cfg, serve_token.txt, serve_log.txt, watchlist.yaml, watchlist_state.json) goes
# to /config/bdfr; caches (yt-dlp) to /config/cache. Both must be writable by the container's user.
ENV XDG_CONFIG_HOME=/config \
    XDG_CACHE_HOME=/config/cache \
    PYTHONUNBUFFERED=1
VOLUME ["/data", "/config"]
WORKDIR /data
EXPOSE 7635

HEALTHCHECK --interval=1m --timeout=10s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 7635), 5)"

# Extra serve options (e.g. --rename-existing, -v) can be added after the image name or in compose's "command"
ENTRYPOINT ["bdfr", "serve", "/data", "--host", "0.0.0.0", "--port", "7635"]
