#!/bin/sh
# Container entrypoint: the Agent Deck daemon, optionally with a Herdr server
# of the container's own started first.
#
#   AGTDECK_HERDR_SERVER=1   start a headless `herdr server` in the container
#                              and wait for its socket, then run the daemon.
#                              Agents then live in the container: attach with
#                              `docker exec -it <container> herdr`.
#   (unset)                    just the daemon; it uses whatever Herdr or tmux
#                              socket is mounted in.
set -e
if [ "${AGTDECK_HERDR_SERVER:-0}" = 1 ]; then
    sock="${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}"
    herdr server >"$HOME/herdr-server.log" 2>&1 &
    i=0
    while [ ! -S "$sock" ]; do
        i=$((i + 1))
        if [ "$i" -gt 50 ]; then
            echo "entrypoint: herdr server did not open $sock" >&2
            cat "$HOME/herdr-server.log" >&2
            exit 1
        fi
        sleep 0.2
    done
fi
exec python /opt/agtdeck/agtdeck.py "$@"
