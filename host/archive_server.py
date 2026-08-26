"""
Serves the parts of the archive that are too big for GitHub Pages: the replay
recordings themselves and the tagpro.eu records behind them.

Read-only, and bound to loopback - the only thing that reaches it is the
cloudflared quick tunnel started by supervise.sh, which is what makes it
reachable from outside. Because that tunnel's hostname changes on every
restart, nothing should link to this server directly; links go to the Worker's
stable /go prefix, which redirects here (see worker/src/index.js).

Run with: python3 archive_server.py [port]

Weeks are keyed by the Monday (UTC) of a match's koala start time, the same
way build.py keys the published coverage tables, so a week here holds exactly
the matches that week's row on the site counts.
"""
import datetime as dt
import gzip
import html
import io
import json
import logging
import os
import re
import socketserver
import sqlite3
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB = Path(os.environ.get("ARCHIVE_DB", "/home/metjr/nte/data/tagpro.db"))
REPLAYS = Path(os.environ.get("ARCHIVE_REPLAYS", "/home/metjr/nte/data/replays"))
BULK = Path(os.environ.get("ARCHIVE_BULK", "/home/metjr/nte/data/ranked_matches_bulk.json.gz"))
SITE = "https://bambitp.github.io/tagpro-replay-archive/"

# A home uplink, not a CDN. Past this many transfers in flight the rest are
# turned away with a Retry-After rather than everyone getting a trickle.
MAX_TRANSFERS = 8
CHUNK = 256 * 1024

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger("archive")
_local = threading.local()
_transfers = threading.BoundedSemaphore(MAX_TRANSFERS)


def db():
    """One read-only connection per thread; ThreadingHTTPServer gives us one thread per request."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def monday(iso_utc):
    d = dt.datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).date()
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def week_bounds(week):
    """[start, end) as ISO-Z strings, for comparing against koala_matches.started."""
    start = dt.datetime.fromisoformat(week).replace(tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=7)
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return fmt(start), fmt(end)


def replay_path(row):
    """Recording on disk, or None. path is stored relative to the replays root."""
    if not row["path"]:
        return None
    p = (REPLAYS / row["path"]).resolve()
    # The path comes out of our own database, but resolve-and-check costs
    # nothing and means a bad row can't reach outside the replay tree.
    if REPLAYS.resolve() not in p.parents or not p.is_file():
        return None
    return p


# ---------------------------------------------------------------- eu records

def eu_record(conn, match_id):
    m = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if not m:
        return None
    rec = {k: m[k] for k in m.keys() if k != "raw_json"}
    rec["players"] = [dict(r) for r in conn.execute(
        "SELECT * FROM match_players WHERE match_id = ? ORDER BY team, player_name", (match_id,))]
    rec["events"] = [dict(r) for r in conn.execute(
        "SELECT time, player_name, team, kind, detail, x, y FROM match_events "
        "WHERE match_id = ? ORDER BY time, id", (match_id,))]
    rec["splats"] = [dict(r) for r in conn.execute(
        "SELECT time, x, y, player_name, team, kind FROM match_splats "
        "WHERE match_id = ? ORDER BY time, id", (match_id,))]
    if m["map_id"] is not None:
        mp = conn.execute("SELECT map_id, name, author, type, width, height FROM maps "
                          "WHERE map_id = ?", (m["map_id"],)).fetchone()
        rec["map"] = dict(mp) if mp else None
    # Ranked skill/tier movement, keyed by the koala uuid rather than the eu id.
    if m["koala_uuid"]:
        rd = conn.execute("SELECT * FROM match_ranked_data WHERE uuid = ?",
                          (m["koala_uuid"],)).fetchone()
        rec["ranked"] = dict(rd) if rd else None
        rec["ranked_players"] = [dict(r) for r in conn.execute(
            "SELECT * FROM match_ranked_players WHERE uuid = ?", (m["koala_uuid"],))]
    return rec


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tagpro-archive/1.0"

    def log_message(self, fmt, *args):
        ip = self.headers.get("CF-Connecting-IP", self.client_address[0])
        log.info("%s %s", ip, fmt % args)

    # -- small helpers ----------------------------------------------------

    def _head(self, status, ctype, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if length is None:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self._head(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, "application/json", json.dumps(obj, separators=(",", ":")))

    def _error(self, status, msg):
        self._send(status, "text/plain; charset=utf-8", msg + "\n")

    def _chunks(self):
        """Chunked-encoding writer: everything streamed has an unknown length up front."""
        out = self.wfile

        class W:
            def write(self, b):
                if not b:
                    return 0
                out.write(b"%X\r\n" % len(b))
                out.write(b)
                out.write(b"\r\n")
                return len(b)

            def flush(self):
                out.flush()

            def close(self):
                out.write(b"0\r\n\r\n")
                out.flush()

        return W()

    def _sendfile(self, path, ctype, filename):
        """Static file with single-range support, so a dropped transfer resumes."""
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range", "")
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
        if m:
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                if b:
                    end = min(int(b), size - 1)
            elif b:                      # suffix range: last N bytes
                start = max(0, size - int(b))
            if start > end or start >= size:
                self._send(416, "text/plain", "range not satisfiable\n",
                           {"Content-Range": f"bytes */{size}"})
                return
            status = 206
        length = end - start + 1
        extra = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._head(status, ctype, length, extra)
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    # -- routes -----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                return self.landing()
            if path == "/manifest.json":
                return self.manifest()
            if path == "/weeks.json":
                return self.weeks()

            m = re.match(r"^/replay/([0-9a-f-]{36})$", path)
            if m:
                return self.replay(m.group(1))

            m = re.match(r"^/week/(\d{4}-\d{2}-\d{2})/replays\.tar$", path)
            if m:
                return self.week_tar(m.group(1))

            m = re.match(r"^/week/(\d{4}-\d{2}-\d{2})/replays\.json$", path)
            if m:
                return self.week_list(m.group(1))

            m = re.match(r"^/eu/(\d+)\.json$", path)
            if m:
                return self.eu_one(int(m.group(1)))

            m = re.match(r"^/eu/week/(\d{4}-\d{2}-\d{2})\.json\.gz$", path)
            if m:
                return self.eu_week(m.group(1))

            if path == "/bulk/ranked_matches_bulk.json.gz":
                return self.bulk()

            self._error(404, "no such route - see / for what is here")
        except BrokenPipeError:
            pass                                  # client hung up mid-download
        except Exception:
            log.exception("request failed: %s", path)
            try:
                self._error(500, "server error")
            except Exception:
                pass

    def replay(self, uuid):
        if not UUID_RE.match(uuid):
            return self._error(400, "not a uuid")
        row = db().execute("SELECT path FROM replay_files WHERE uuid = ? AND status = 'done'",
                           (uuid,)).fetchone()
        p = replay_path(row) if row else None
        if not p:
            return self._error(404, "no recording held for that uuid")
        with _transfers:
            self._sendfile(p, "application/gzip", f"{uuid}.ndjson.gz")

    def bulk(self):
        if not BULK.is_file():
            return self._error(404, "bulk export not present")
        with _transfers:
            self._sendfile(BULK, "application/gzip", BULK.name)

    def week_rows(self, week):
        lo, hi = week_bounds(week)
        return db().execute(
            "SELECT k.uuid, k.game_id, k.started, k.map_name, r.path, r.bytes_stored "
            "FROM koala_matches k JOIN replay_files r ON r.uuid = k.uuid AND r.status = 'done' "
            "WHERE k.started >= ? AND k.started < ? ORDER BY k.started", (lo, hi)).fetchall()

    def week_list(self, week):
        rows = self.week_rows(week)
        self._json([{"uuid": r["uuid"], "game_id": r["game_id"], "started": r["started"],
                     "map": r["map_name"], "bytes": r["bytes_stored"]} for r in rows])

    def week_tar(self, week):
        rows = self.week_rows(week)
        if not rows:
            return self._error(404, "no recordings held for that week")
        with _transfers:
            self._head(200, "application/x-tar", None,
                       {"Content-Disposition": f'attachment; filename="replays-{week}.tar"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            # Stream mode: nothing is buffered and nothing seeks, so this costs
            # no disk and no memory however large the week is.
            tar = tarfile.open(fileobj=w, mode="w|")
            try:
                for r in rows:
                    p = replay_path(r)
                    if not p:
                        continue
                    info = tar.gettarinfo(str(p), arcname=f"{r['uuid']}.ndjson.gz")
                    with open(p, "rb") as f:
                        tar.addfile(info, f)
            finally:
                tar.close()
                w.close()

    def eu_one(self, match_id):
        rec = eu_record(db(), match_id)
        if rec is None:
            return self._error(404, "no tagpro.eu record held for that match id")
        self._json(rec)

    def eu_week(self, week):
        lo, hi = week_bounds(week)
        ids = [r[0] for r in db().execute(
            "SELECT m.match_id FROM koala_matches k JOIN matches m ON m.koala_uuid = k.uuid "
            "WHERE k.started >= ? AND k.started < ? ORDER BY k.started", (lo, hi))]
        if not ids:
            return self._error(404, "no tagpro.eu records held for that week")
        with _transfers:
            self._head(200, "application/gzip", None,
                       {"Content-Disposition": f'attachment; filename="eu-{week}.json.gz"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            gz = gzip.GzipFile(fileobj=w, mode="wb")
            try:
                gz.write(b"[")
                conn = db()
                for i, mid in enumerate(ids):
                    if i:
                        gz.write(b",")
                    gz.write(json.dumps(eu_record(conn, mid), separators=(",", ":")).encode())
                gz.write(b"]")
            finally:
                gz.close()
                w.close()

    def weeks(self):
        rows = db().execute(
            "SELECT substr(k.started,1,10) AS d, COUNT(*) AS ids, "
            "SUM(CASE WHEN r.uuid IS NOT NULL THEN 1 ELSE 0 END) AS held, "
            "SUM(COALESCE(r.bytes_stored,0)) AS bytes "
            "FROM koala_matches k LEFT JOIN replay_files r "
            "  ON r.uuid = k.uuid AND r.status = 'done' GROUP BY d").fetchall()
        weeks = {}
        for r in rows:
            w = weeks.setdefault(monday(r["d"]), {"week": monday(r["d"]), "ids": 0,
                                                  "recordings": 0, "bytes": 0})
            w["ids"] += r["ids"]
            w["recordings"] += r["held"]
            w["bytes"] += r["bytes"]
        return self._json([weeks[k] for k in sorted(weeks)])

    def manifest(self):
        conn = db()
        one = lambda q: conn.execute(q).fetchone()[0]
        self._json({
            "site": SITE,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "match_ids": one("SELECT COUNT(*) FROM koala_matches"),
            "recordings": one("SELECT COUNT(*) FROM replay_files WHERE status='done'"),
            "recordings_bytes": one("SELECT COALESCE(SUM(bytes_stored),0) FROM replay_files WHERE status='done'"),
            "eu_records": one("SELECT COUNT(*) FROM matches"),
            "eu_events": one("SELECT COUNT(*) FROM match_events"),
            "first_match": one("SELECT MIN(started) FROM koala_matches"),
            "last_match": one("SELECT MAX(started) FROM koala_matches"),
            "bulk_export_bytes": BULK.stat().st_size if BULK.is_file() else None,
            "routes": [
                "/manifest.json", "/weeks.json",
                "/replay/<uuid>",
                "/week/<YYYY-MM-DD>/replays.json", "/week/<YYYY-MM-DD>/replays.tar",
                "/eu/<match_id>.json", "/eu/week/<YYYY-MM-DD>.json.gz",
                "/bulk/ranked_matches_bulk.json.gz",
            ],
        })

    def landing(self):
        conn = db()
        one = lambda q: conn.execute(q).fetchone()[0]
        held = one("SELECT COUNT(*) FROM replay_files WHERE status='done'")
        gb = one("SELECT COALESCE(SUM(bytes_stored),0) FROM replay_files WHERE status='done'") / 1e9
        ids = one("SELECT COUNT(*) FROM koala_matches")
        eu = one("SELECT COUNT(*) FROM matches")
        sample = conn.execute("SELECT uuid FROM replay_files WHERE status='done' LIMIT 1").fetchone()
        uuid = sample["uuid"] if sample else "<uuid>"
        body = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>TagPro replay archive &mdash; host</title>
<style>
body{{margin:0;background:#101215;color:#e8eaee;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:44px 22px 80px}}
h1{{font-size:22px;font-weight:640;margin:0 0 6px;letter-spacing:-.01em}}
h2{{font-size:15px;font-weight:640;margin:30px 0 8px}}
p{{color:#a2aab8;margin:0 0 12px}}
a{{color:#5b9bdd}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;margin:0 0 12px}}
td{{padding:6px 10px;border-bottom:1px solid #282c34;vertical-align:top}}
td:first-child{{white-space:nowrap;font-family:ui-monospace,Menlo,Consolas,monospace;color:#e8eaee}}
td:last-child{{color:#a2aab8}}
pre{{background:#1e222a;padding:12px 14px;border-radius:7px;overflow-x:auto;font-size:12.5px;
font-family:ui-monospace,Menlo,Consolas,monospace}}
.n{{color:#78808e;font-size:13px}}
</style></head><body><div class=wrap>
<h1>TagPro replay archive &mdash; host</h1>
<p>The recordings and tagpro.eu records behind
<a href="{SITE}">the coverage site</a>. {held:,} recordings ({gb:.1f} GB),
{ids:,} match ids, {eu:,} tagpro.eu records.</p>
<p class=n>This is a home machine on a rotating tunnel hostname. Do not bookmark this address
&mdash; it changes. Use the archive site's stable links, which redirect here.</p>
<h2>Routes</h2>
<table>
<tr><td>/manifest.json</td><td>counts, sizes, what is here</td></tr>
<tr><td>/weeks.json</td><td>per-week ids, recordings held, bytes</td></tr>
<tr><td>/replay/&lt;uuid&gt;</td><td>one recording, <code>.ndjson.gz</code>, resumable</td></tr>
<tr><td>/week/&lt;YYYY-MM-DD&gt;/replays.json</td><td>what that week holds</td></tr>
<tr><td>/week/&lt;YYYY-MM-DD&gt;/replays.tar</td><td>every recording that week, streamed as one tar</td></tr>
<tr><td>/eu/&lt;match_id&gt;.json</td><td>one tagpro.eu record: match, players, events, splats, ranked</td></tr>
<tr><td>/eu/week/&lt;YYYY-MM-DD&gt;.json.gz</td><td>every tagpro.eu record for that week</td></tr>
<tr><td>/bulk/ranked_matches_bulk.json.gz</td><td>the whole tagpro.eu export in one file</td></tr>
</table>
<p class=n>Weeks are keyed by the Monday, UTC, matching the tables on the coverage site.</p>
<h2>Pulling it down</h2>
<pre>curl -L -O https://tagpro-archive-tunnel.bambitagpro.workers.dev/go/replay/{uuid}
curl -L https://tagpro-archive-tunnel.bambitagpro.workers.dev/go/week/2026-08-17/replays.tar | tar x
curl -L -C - -O https://tagpro-archive-tunnel.bambitagpro.workers.dev/go/bulk/ranked_matches_bulk.json.gz</pre>
<p class=n>Go through <code>/go/</code> rather than this hostname and your script keeps working
after the tunnel rotates. <code>-C -</code> resumes a part-finished file.</p>
</div></body></html>"""
        self._send(200, "text/html; charset=utf-8", body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8431
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s archive %(levelname)s %(message)s")
    if not DB.is_file():
        sys.exit(f"database not found: {DB}")
    if not REPLAYS.is_dir():
        sys.exit(f"replay tree not found: {REPLAYS}")
    srv = Server(("127.0.0.1", port), Handler)
    log.info("serving %s and %s on 127.0.0.1:%d", DB, REPLAYS, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
