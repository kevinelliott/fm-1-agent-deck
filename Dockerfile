# Agent Deck's host daemon (host/agtdeck.py) as a container.
#
#   docker build -t agtdeck .
#   docker run --rm -it agtdeck --simulate --backend tmux     # no FM-1 needed
#
# With a real FM-1 the container needs three things from the computer: the
# FM-1's MIDI port, the multiplexer's socket and (for the INFO view) Claude
# Code's session logs. On a Linux host:
#
#   docker run --rm -it \
#     --device /dev/snd \
#     --user "$(id -u):$(id -g)" --group-add "$(getent group audio | cut -d: -f3)" \
#     -v "$HOME/.config/herdr:/home/deck/.config/herdr" \
#     -v "$HOME/.config/agtdeck:/home/deck/.config/agtdeck" \
#     -v "$HOME/.claude/projects:/home/deck/.claude/projects:ro" \
#     agtdeck --backend herdr
#
# For tmux instead of Herdr, mount tmux's socket directory at the same path
# (-v /tmp/tmux-$(id -u):/tmp/tmux-$(id -u)) and pass --backend tmux.
#
# Herdr itself is in the image too (the release the daemon is verified
# against). The container can run a Herdr server of its own, so the agents
# live in the container next to the daemon:
#
#   docker run --rm -it --name deck -e AGTDECK_HERDR_SERVER=1 agtdeck --simulate
#   docker exec -it deck herdr          # in another terminal: the Herdr TUI
#
# Docker Desktop on macOS and Windows does not pass USB MIDI devices into
# containers, so there the image is good for --simulate only; run the daemon
# directly on the computer to drive a real FM-1.
FROM python:3.12-slim

ARG HERDR_VERSION=0.9.5
ARG HERDR_SHA256_AMD64=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d
ARG HERDR_SHA256_ARM64=17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c
ARG TARGETARCH

# libasound2: ALSA, which python-rtmidi talks MIDI through. tmux: the tmux
# backend's client. git: the INFO and DIFF views. build tools only if pip
# has to compile python-rtmidi (no wheel for the platform).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libasound2 libjack-jackd2-0 tmux git \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir mido python-rtmidi

# Herdr (Apache-2.0): the release binary for this architecture, from its
# GitHub releases, checked against the SHA-256 recorded above. To move to
# another version, pass all three build args.
ADD https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-x86_64 /tmp/herdr-amd64
ADD https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-aarch64 /tmp/herdr-arm64
RUN case "${TARGETARCH}" in \
      amd64) sum="${HERDR_SHA256_AMD64}" ;; \
      arm64) sum="${HERDR_SHA256_ARM64}" ;; \
      *) echo "no Herdr binary for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && echo "${sum}  /tmp/herdr-${TARGETARCH}" | sha256sum -c - \
 && install -m 0755 "/tmp/herdr-${TARGETARCH}" /usr/local/bin/herdr \
 && rm -f /tmp/herdr-amd64 /tmp/herdr-arm64 \
 && herdr --version

# a home any --user can write to: the daemon keeps slots.json under ~/.config
ENV HOME=/home/deck PYTHONUNBUFFERED=1
RUN mkdir -p /home/deck/.config/agtdeck /home/deck/.config/herdr && chmod -R 0777 /home/deck \
 && git config --system --add safe.directory '*'

WORKDIR /opt/agtdeck
COPY host/agtdeck.py host/config.example.json ./
COPY LICENSE PROTOCOL.md ./
COPY host/docker/entrypoint.sh /usr/local/bin/agtdeck-entrypoint

ENTRYPOINT ["agtdeck-entrypoint"]
CMD ["--backend", "auto"]
