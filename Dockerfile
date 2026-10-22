# syntax=docker/dockerfile:1
# bdfr serve for an always-on machine: downloads requested by the userscript, plus the weekly Arctic Shift baseline
# for the users in /config/bdfr/watchlist.yaml. See docker-compose.yml.
# Alpine (musl) and flattened into one layer, so files removed from the base image (pip, IDLE, ...) really go: ~107 MB
# unpacked, ~35 MB to download.

# ffmpeg, built from the official source release with only what yt-dlp uses here: it never encodes, only copies
# streams into a new container (merging the best video and audio of v.redd.it and other yt-dlp sites, and fixing HLS
# AAC audio), and sometimes downloads HLS itself over HTTPS. The decoders are only there because reading MPEG-TS
# needs them to find stream parameters: an AAC stream's sample rate (v.redd.it's TS segments fail without), and
# through the HLS demuxer an H.264/HEVC stream's size. No encoders, filters beyond the CLI's built-ins, devices or
# external libraries except OpenSSL and zlib, which the Python image already has. LGPL 2.1 build.
# musl gives new threads a 128 KiB stack by default (glibc 8 MiB), which ffmpeg's threaded muxing overflows (it
# segfaulted merging v.redd.it video and audio); -z stack-size sets the default for its threads to 2 MiB.
# To update: take the new release's .tar.xz from https://ffmpeg.org/releases/, check it against its .asc with the
# FFmpeg release signing key (https://ffmpeg.org/ffmpeg-devel.asc, fingerprint
# FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8), then change the version and the SHA-256 below.
FROM python:3.15-alpine AS ffmpeg
RUN apk add --no-cache gcc musl-dev make pkgconf openssl-dev zlib-dev xz
ADD --checksum=sha256:8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e \
    https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz /tmp/ffmpeg.tar.xz
WORKDIR /tmp/ffmpeg
RUN tar -xJf /tmp/ffmpeg.tar.xz --strip-components=1 \
    && ./configure --prefix=/opt/ffmpeg \
        --disable-everything --disable-autodetect --disable-doc --disable-debug --disable-ffplay --disable-avdevice \
        --disable-x86asm --enable-small --enable-pthreads --enable-zlib --enable-openssl \
        --extra-ldflags="-Wl,-z,stack-size=2097152" \
        --enable-protocol=file,pipe,http,https,tcp,tls,crypto,data \
        --enable-demuxer=mov,matroska,mpegts,hls,aac,mp3,ogg,flv,concat \
        --enable-muxer=mp4,ipod,mov,matroska,webm,mpegts,flv,mp3,adts,ogg,opus \
        --enable-parser=h264,hevc,av1,vp8,vp9,mpeg4video,aac,aac_latm,ac3,mpegaudio,opus,vorbis \
        --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,vp9_superframe,extract_extradata,setts \
        --enable-decoder=aac,aac_latm,ac3,eac3,mp3,mp3float,opus,vorbis,h264,hevc \
    && make -j"$(nproc)" \
    && make install \
    && cp COPYING.LGPLv2.1 /opt/ffmpeg/LICENSE

FROM python:3.15-alpine AS app

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Dependencies first, in their own layer, so a change to bdfr's code doesn't reinstall them
WORKDIR /src
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > requirements.txt \
    && pip install -r requirements.txt
COPY LICENSE ./
COPY bdfr ./bdfr
# Then what serve doesn't need goes (pip, IDLE, pydoc's data), and the standard library is compiled to bytecode, which
# the Alpine image ships without. The container's user can't write bytecode, so without it every start of serve would
# spend ~3 s compiling what it imports (yt-dlp's extractors most of all).
RUN pip install --no-deps . \
    && pip uninstall -y pip \
    && rm -rf /src /usr/local/bin/pip* /usr/local/bin/idle* /usr/local/bin/pydoc* \
        /usr/local/lib/python3.15/ensurepip /usr/local/lib/python3.15/idlelib /usr/local/lib/python3.15/pydoc_data \
    && cd / \
    && python -m compileall -q -j0 /usr/local/lib/python3.15
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg /opt/ffmpeg/LICENSE /usr/local/share/doc/ffmpeg/LICENSE

# One layer: removing files in a later layer wouldn't make the image smaller. The base image's settings don't carry
# over, so PATH is set again.
FROM scratch
COPY --from=app / /

# BDFR's configuration (default_config.cfg, serve_token.txt, serve_log.txt, watchlist.yaml, watchlist_state.json,
# failed_posts.json) goes to /config/bdfr; caches (yt-dlp) to /config/cache. Both must be writable by the container's
# user.
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    XDG_CONFIG_HOME=/config \
    XDG_CACHE_HOME=/config/cache \
    PYTHONUNBUFFERED=1
VOLUME ["/data", "/config"]
WORKDIR /data
EXPOSE 7635

HEALTHCHECK --interval=1m --timeout=10s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 7635), 5)"

# Extra serve options (e.g. --rename-existing, -v) can be added after the image name or in compose's "command"
ENTRYPOINT ["bdfr", "serve", "/data", "--host", "0.0.0.0", "--port", "7635"]
