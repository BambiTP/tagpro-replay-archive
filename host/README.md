# Archive host

Serves the parts of the archive that are too big for GitHub Pages: the replay
recordings and the tagpro.eu records behind them.

- `archive_server.py` — read-only HTTP server, loopback only. Routes are listed
  on its own landing page and in `/manifest.json`.
- `supervise.sh` — runs that server, runs a free cloudflared quick tunnel in
  front of it, and registers the hostname the tunnel was assigned with the
  Worker in `../worker`, re-posting once a minute as a heartbeat.

A quick tunnel gets a new `*.trycloudflare.com` hostname on every restart, so
nothing links to it directly. The site links to `<worker>/go/…`, which
redirects to wherever the host currently is, and answers 503 when it is down.

## Running it

Started from cron under `flock`, the same pattern as `publish.sh`:

```
@reboot     flock -n .tunnel.lock host/supervise.sh >> .tunnel.log 2>&1
*/5 * * * * flock -n .tunnel.lock host/supervise.sh >> .tunnel.log 2>&1
```

The `*/5` line is the self-heal — `flock` makes it a no-op while the supervisor
is alive, and restarts it within five minutes if it is not. By hand:

```
flock -n .tunnel.lock host/supervise.sh          # start (blocks)
pkill -f supervise.sh                            # stop, deregisters on the way out
tail -f .tunnel.log                              # what it is doing
curl -s $WORKER_URL/status                       # what the site sees
```

## Configuration

`~/.config/tagpro-archive/tunnel.env`, mode 600, deliberately outside this repo
because `publish.sh` runs `git add -A` and pushes to a public remote:

```
WORKER_URL=https://tagpro-archive-tunnel.<subdomain>.workers.dev
TUNNEL_SECRET=<same value as the Worker's TUNNEL_SECRET secret>
```

Paths default to the pipeline's own (`ARCHIVE_DB`, `ARCHIVE_REPLAYS`,
`ARCHIVE_BULK` override them). The server holds a read-only SQLite connection
and never writes anything.

## Limits

Eight transfers run at once; the rest get a 503 with `Retry-After`. It is one
home uplink, and a week of recordings is a few hundred megabytes.
