#!/bin/bash
# Keeps the archive host reachable: runs the file server, runs a cloudflared
# quick tunnel in front of it, and tells the Worker where that tunnel currently
# is so the site's stable links keep resolving.
#
# A free quick tunnel is handed a new *.trycloudflare.com hostname every time
# it starts, and it does not stay up forever. So the hostname is re-registered
# on every restart, and re-posted once a minute as a heartbeat - the Worker
# calls the host offline after five minutes of silence rather than handing out
# an address that no longer answers.
#
# Started from cron under flock, the same pattern as publish.sh:
#   @reboot     flock -n .tunnel.lock host/supervise.sh
#   */5 * * * * flock -n .tunnel.lock host/supervise.sh
# The second line is the self-heal - flock makes it a no-op while this is
# alive, and restarts it within five minutes if it is not.
set -uo pipefail

REPO=/home/metjr/tagpro-archive
PORT=8431
METRICS_PORT=8432
ENV_FILE="$HOME/.config/tagpro-archive/tunnel.env"
TUNNEL_LOG=$REPO/.cloudflared.log
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

log() { echo "$(date -Is) $*"; }

[ -r "$ENV_FILE" ] || { log "no $ENV_FILE - cannot register"; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${WORKER_URL:?}" "${TUNNEL_SECRET:?}"

SERVER_PID=""
TUNNEL_PID=""

cleanup() {
    log "shutting down"
    # Tell the site we are gone, so it shows offline instead of a dead link.
    curl -sf -m 10 -X DELETE "$WORKER_URL/register" \
         -H "Authorization: Bearer $TUNNEL_SECRET" -o /dev/null
    [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    exit 0
}
trap cleanup INT TERM EXIT

server_up() { curl -sf -m 5 -o /dev/null "http://127.0.0.1:$PORT/manifest.json"; }

start_server() {
    if server_up; then
        log "file server already listening on $PORT"
        return
    fi
    python3 "$REPO/host/archive_server.py" "$PORT" &
    SERVER_PID=$!
    for _ in $(seq 30); do
        server_up && { log "file server up (pid $SERVER_PID)"; return; }
        sleep 1
    done
    log "file server did not come up"
    exit 1
}

# The hostname cloudflared was assigned. Its metrics endpoint is the reliable
# source; scraping the log is the fallback if that ever moves.
tunnel_host() {
    local h
    h=$(curl -sf -m 5 "http://127.0.0.1:$METRICS_PORT/quicktunnel" 2>/dev/null \
        | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')
    [ -n "$h" ] && { echo "https://$h"; return 0; }
    h=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1)
    [ -n "$h" ] && { echo "$h"; return 0; }
    return 1
}

register() {
    # Download totals ride along with the heartbeat, so the site can show them
    # from the same request it already makes - and can still show them while
    # the host is down, which is exactly when nobody could ask the host.
    local totals
    totals=$(curl -sf -m 5 "http://127.0.0.1:$PORT/stats.json" \
             | sed -n 's/.*"downloads":\([0-9]*\).*"bytes":\([0-9]*\).*/,"downloads":\1,"bytes":\2/p')
    curl -sf -m 15 -X POST "$WORKER_URL/register" \
         -H "Authorization: Bearer $TUNNEL_SECRET" \
         -H "Content-Type: application/json" \
         -d "{\"url\":\"$1\"$totals}" -o /dev/null
}

start_tunnel() {
    : > "$TUNNEL_LOG"
    cloudflared tunnel --no-autoupdate --metrics "127.0.0.1:$METRICS_PORT" \
        --url "http://127.0.0.1:$PORT" >> "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    for _ in $(seq 60); do
        CURRENT=$(tunnel_host) && break
        kill -0 "$TUNNEL_PID" 2>/dev/null || { log "cloudflared exited during startup"; return 1; }
        sleep 1
    done
    [ -n "${CURRENT:-}" ] || { log "no tunnel hostname after 60s"; return 1; }
    if register "$CURRENT"; then
        log "tunnel up: $CURRENT (pid $TUNNEL_PID)"
    else
        log "tunnel up at $CURRENT but registration failed - will retry"
    fi
    return 0
}

# Keep the log from growing without bound across reboots.
[ -f "$REPO/.tunnel.log" ] && [ "$(stat -c%s "$REPO/.tunnel.log")" -gt 5000000 ] \
    && tail -c 1000000 "$REPO/.tunnel.log" > "$REPO/.tunnel.log.tmp" \
    && mv "$REPO/.tunnel.log.tmp" "$REPO/.tunnel.log"

start_server
CURRENT=""
until start_tunnel; do
    log "retrying tunnel in 30s"
    sleep 30
done

while true; do
    sleep 60

    if ! server_up; then
        log "file server stopped answering - restarting it"
        [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
        SERVER_PID=""
        start_server
    fi

    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        log "cloudflared exited - restarting, hostname will change"
        CURRENT=""
        until start_tunnel; do
            log "retrying tunnel in 30s"
            sleep 30
        done
        continue
    fi

    # Same hostname almost always, but a reconnect can move it, and the
    # heartbeat has to go out either way to keep the host marked online.
    if host=$(tunnel_host); then
        [ "$host" != "$CURRENT" ] && log "hostname changed: $CURRENT -> $host"
        CURRENT=$host
        register "$CURRENT" || log "heartbeat failed"
    else
        log "cannot read tunnel hostname"
    fi
done
