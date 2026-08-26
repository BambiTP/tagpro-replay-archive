# tagpro-archive-tunnel

Keeps the current address of the archive host so the static site can link to
something that does not move.

The host is exposed by a free cloudflared quick tunnel, whose hostname changes
on every restart. `host/supervise.sh` posts the new hostname here; the site
links to `/go`, which redirects to it.

| Route | |
| --- | --- |
| `GET /` `GET /status` | `{online, url, updated, age}`, CORS open, never cached |
| `GET /go` `GET /go/<path>` | 302 to the host, path and query carried through; 503 when offline |
| `POST /register` | `Authorization: Bearer $TUNNEL_SECRET`, `{"url": "https://x.trycloudflare.com"}` |
| `DELETE /register` | same auth, marks offline on clean shutdown |

Only an `https://*.trycloudflare.com` origin can be registered, so `/go` cannot
be turned into an open redirect.

Deploy: `npx wrangler deploy`. The secret is set with
`npx wrangler secret put TUNNEL_SECRET` and lives on the host in
`~/.config/tagpro-archive/tunnel.env`.
