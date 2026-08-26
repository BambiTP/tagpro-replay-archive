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
import urllib.parse
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB = Path(os.environ.get("ARCHIVE_DB", "/home/metjr/nte/data/tagpro.db"))
REPLAYS = Path(os.environ.get("ARCHIVE_REPLAYS", "/home/metjr/nte/data/replays"))
BULK = Path(os.environ.get("ARCHIVE_BULK", "/home/metjr/nte/data/ranked_matches_bulk.json.gz"))
SITE = "https://bambitp.github.io/tagpro-replay-archive/"

# A home uplink, not a CDN. Past this many transfers in flight the rest are
# turned away with a Retry-After rather than everyone getting a trickle.
MAX_TRANSFERS = 8
# ...and no single caller may hold more than this many of those slots, so one
# script with fifty parallel connections cannot take the whole server.
MAX_PER_IP = 2
# Requests per IP per minute. Generous for a person browsing or a sane script,
# low enough that a loop hammering /custom.tar gets shut out.
RATE_LIMIT = 90
RATE_WINDOW_S = 60
CHUNK = 256 * 1024

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

log = logging.getLogger("archive")
_local = threading.local()
_transfers = threading.BoundedSemaphore(MAX_TRANSFERS)
_ip_lock = threading.Lock()
_ip_active = {}                  # ip -> transfers in flight
_ip_hits = {}                    # ip -> [timestamps within the window]


class Busy(Exception):
    """Too many requests, or too many at once, from one caller."""


def check_rate(ip):
    now = time.time()
    with _ip_lock:
        hits = [t for t in _ip_hits.get(ip, ()) if now - t < RATE_WINDOW_S]
        if len(hits) >= RATE_LIMIT:
            _ip_hits[ip] = hits
            raise Busy(f"more than {RATE_LIMIT} requests a minute")
        hits.append(now)
        _ip_hits[ip] = hits
        # Keep the table from growing without bound on a long uptime.
        if len(_ip_hits) > 4096:
            for k in [k for k, v in _ip_hits.items() if not v or now - v[-1] > RATE_WINDOW_S]:
                _ip_hits.pop(k, None)


class transfer_slot:
    """One of MAX_TRANSFERS globally, at most MAX_PER_IP of them per caller."""

    def __init__(self, ip):
        self.ip = ip

    def __enter__(self):
        with _ip_lock:
            if _ip_active.get(self.ip, 0) >= MAX_PER_IP:
                raise Busy(f"more than {MAX_PER_IP} downloads at once")
            _ip_active[self.ip] = _ip_active.get(self.ip, 0) + 1
        if not _transfers.acquire(timeout=30):
            with _ip_lock:
                _ip_active[self.ip] -= 1
            raise Busy("server busy")
        return self

    def __exit__(self, *exc):
        _transfers.release()
        with _ip_lock:
            n = _ip_active.get(self.ip, 1) - 1
            if n <= 0:
                _ip_active.pop(self.ip, None)
            else:
                _ip_active[self.ip] = n


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


# --------------------------------------------------------------- eu bulk shape

# tagpro.eu's own bulk endpoint is {match_id: match_doc}. Genuine records are
# served as the exact document the mirror gave us, plus the three fields the
# pipeline derives (outcome, void_reason, disconnected_players).
#
# Rebuilt records have no such document - they were reconstructed from a
# recording and there is nothing to copy. They are emitted in the same shape
# with the fields a recording cannot supply set to null, marked
# "source": "replay", and left OUT unless rebuilt=1 is asked for.

def eu_bulk_doc(conn, row):
    """One entry of a tagpro.eu-shaped bulk file, genuine or rebuilt."""
    if row["raw_json"]:
        doc = json.loads(row["raw_json"])
        doc["outcome"] = row["outcome"]
        doc["void_reason"] = row["void_reason"]
        doc["koala_uuid"] = row["koala_uuid"]
        doc["disconnected_players"] = [
            r[0] for r in conn.execute("SELECT player_name FROM match_players "
                                       "WHERE match_id = ? AND disconnected = 1", (row["match_id"],))]
        return doc

    # Rebuilt: same keys, nulls where a recording carries nothing, and the
    # per-player counters and event list the mirror packs into strings.
    started = dt.datetime.fromisoformat(row["date"]).replace(tzinfo=dt.timezone.utc)
    players = []
    for pr in conn.execute("SELECT * FROM match_players WHERE match_id = ? ORDER BY team, player_name",
                           (row["match_id"],)):
        players.append({
            "name": pr["player_name"], "team": pr["team"],
            "auth": pr["auth"], "flair": pr["flair"], "degree": pr["degree"],
            "score": pr["score"], "points": pr["points"],
            "events": None,          # the mirror's packed string cannot be reproduced
            "stats": {k: pr[k] for k in
                      ("grabs", "captures", "drops", "hold", "tags", "returns", "pops",
                       "prevent", "pups_total", "button", "block", "time_played")},
        })
    events = [{"time": e["time"], "player": e["player_name"], "team": e["team"],
               "kind": e["kind"], "detail": e["detail"], "x": e["x"], "y": e["y"]}
              for e in conn.execute(
                  "SELECT time, player_name, team, kind, detail, x, y FROM match_events "
                  "WHERE match_id = ? ORDER BY time, id", (row["match_id"],))]
    return {
        "server": row["server"], "port": row["port"], "official": True,
        "uuid": "", "group": row["group_id"] or "",
        "date": int(started.timestamp()), "timeLimit": None,
        "duration": row["duration"], "finished": bool(row["finished"]),
        "mapId": row["map_id"],
        "teams": [{"name": "Red", "score": row["red_score"], "splats": None},
                  {"name": "Blue", "score": row["blue_score"], "splats": None}],
        "players": players,
        "outcome": row["outcome"], "void_reason": row["void_reason"],
        "koala_uuid": row["koala_uuid"],
        "disconnected_players": [r[0] for r in conn.execute(
            "SELECT player_name FROM match_players WHERE match_id = ? AND disconnected = 1",
            (row["match_id"],))],
        # Provenance is a column in the database and stays one here: a
        # replay-derived row is not a tagpro.eu row and does not pretend to be.
        "source": "replay",
        "events_decoded": events,
    }

# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tagpro-archive/1.0"
    # Without this a connection that opens and then says nothing holds a thread
    # for ever - the cheapest denial of service there is against a threaded
    # server. Applies to reading the request as well as writing the response.
    timeout = 60

    def ip(self):
        """Everything arrives from the tunnel on loopback, so the caller's real
        address is the header Cloudflare sets, not the socket."""
        return self.headers.get("CF-Connecting-IP", self.client_address[0])

    def log_message(self, fmt, *args):
        log.info("%s %s", self.ip(), fmt % args)

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
            check_rate(self.ip())
            if path == "/":
                return self.landing()
            if path == "/manifest.json":
                return self.manifest()
            m = re.match(r"^/replay/([0-9a-f-]{36})$", path)
            if m:
                return self.replay(m.group(1))

            m = re.match(r"^/week/(\d{4}-\d{2}-\d{2})/replays\.tar$", path)
            if m:
                return self.week_tar(m.group(1))

            if path == "/all/replays.tar":
                return self.all_tar()

            if path == "/custom.tar":
                return self.custom_tar()

            if path == "/custom/estimate":
                return self.custom_estimate()

            m = re.match(r"^/week/(\d{4}-\d{2}-\d{2})/results\.json\.gz$", path)
            if m:
                return self.week_results(m.group(1), self.want("map", True),
                                         self.want("rebuilt", True))

            m = re.match(r"^/eu/week/(\d{4}-\d{2}-\d{2})\.json\.gz$", path)
            if m:
                return self.eu_week(m.group(1), self.want_rebuilt())

            if path == "/bulk/ranked_matches_bulk.json.gz":
                return self.bulk(self.want_rebuilt())

            self._error(404, "no such route - see / for what is here")
        except Busy as e:
            self._send(429, "text/plain; charset=utf-8", f"slow down: {e}\n",
                       {"Retry-After": "60"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass                                  # client hung up mid-download
        except Exception:
            log.exception("request failed: %s", path)
            try:
                self._error(500, "server error")
            except Exception:
                pass

    def want(self, name, default):
        """Boolean query flag: ?name=0/1/true/false/yes/no/on/off."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if name not in q:
            return default
        return q[name][0].strip().lower() in ("1", "true", "yes", "on")

    def want_rebuilt(self):
        """?rebuilt=1 mixes the reconstructed records into a tagpro.eu download.
        Off by default there: a file labelled tagpro.eu should be tagpro.eu
        unless asked otherwise."""
        return self.want("rebuilt", False)

    def replay(self, uuid):
        if not UUID_RE.match(uuid):
            return self._error(400, "not a uuid")
        row = db().execute("SELECT path FROM replay_files WHERE uuid = ? AND status = 'done'",
                           (uuid,)).fetchone()
        p = replay_path(row) if row else None
        if not p:
            return self._error(404, "no recording held for that uuid")
        with transfer_slot(self.ip()):
            self._sendfile(p, "application/gzip", f"{uuid}.ndjson.gz")

    def range_rows(self, lo, hi, who=""):
        """Recordings held for matches started in [lo, hi)."""
        return db().execute(
            "SELECT k.uuid, k.game_id, k.started, k.map_name, r.path, r.bytes_stored "
            "FROM koala_matches k JOIN replay_files r ON r.uuid = k.uuid AND r.status = 'done' "
            "WHERE k.started >= ? AND k.started < ?" + who + " ORDER BY k.started",
            (lo, hi)).fetchall()

    def week_rows(self, week):
        return self.range_rows(*week_bounds(week))

    def stream_tar(self, rows, filename, prefix=""):
        """Stream mode: nothing buffered and nothing seeks, so this costs no disk
        and no memory whether it is one week or the whole archive."""
        with transfer_slot(self.ip()):
            self._head(200, "application/x-tar", None,
                       {"Content-Disposition": f'attachment; filename="{filename}"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            tar = tarfile.open(fileobj=w, mode="w|")
            sent = 0
            try:
                for r in rows:
                    p = replay_path(r)
                    if not p:
                        continue
                    info = tar.gettarinfo(str(p), arcname=f"{prefix}{r['uuid']}.ndjson.gz")
                    with open(p, "rb") as f:
                        tar.addfile(info, f)
                    sent += 1
            finally:
                tar.close()
                w.close()
                log.info("streamed %d recordings as %s", sent, filename)

    def week_tar(self, week):
        rows = self.week_rows(week)
        if not rows:
            return self._error(404, "no recordings held for that week")
        self.stream_tar(rows, f"replays-{week}.tar")

    def all_tar(self):
        """Every recording held, ~4 GB. Foldered by week so an interrupted
        extraction is obvious and a partial copy is still organised."""
        rows = db().execute(
            "SELECT k.uuid, k.started, r.path FROM koala_matches k "
            "JOIN replay_files r ON r.uuid = k.uuid AND r.status = 'done' "
            "ORDER BY k.started").fetchall()
        if not rows:
            return self._error(404, "no recordings held")
        with transfer_slot(self.ip()):
            self._head(200, "application/x-tar", None,
                       {"Content-Disposition": 'attachment; filename="tagpro-replays-all.tar"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            tar = tarfile.open(fileobj=w, mode="w|")
            sent = 0
            try:
                for r in rows:
                    p = replay_path(r)
                    if not p:
                        continue
                    arc = f"{monday(r['started'][:10])}/{r['uuid']}.ndjson.gz"
                    info = tar.gettarinfo(str(p), arcname=arc)
                    with open(p, "rb") as f:
                        tar.addfile(info, f)
                    sent += 1
            finally:
                tar.close()
                w.close()
                log.info("streamed %d recordings as the whole archive", sent)

    # ------------------------------------------------------------------ picking

    # Every field a results record can carry. uuid is not listed because it is
    # identity and always present. An absent ?fields= means all of them.
    MATCH_FIELDS = ["game_id", "started", "eu_match_id", "record_source", "duration_ms",
                    "duration_frames", "mode", "season", "finished", "outcome",
                    "void_reason", "overtime", "mercy", "score", "winner", "server",
                    "map", "ranked", "ranked_players", "players"]
    # Per-player, inside "players". name is always present, same reason.
    PLAYER_FIELDS = ["team", "auth", "score", "points", "grabs", "captures", "drops",
                     "hold", "tags", "returns", "pops", "prevent", "button", "block",
                     "pups_total", "time_played", "caps_for", "caps_against", "disconnected"]

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def picked(self, name, allowed):
        """?name=a,b,c - absent or empty means everything allowed."""
        q = self.query()
        raw = (q.get(name) or [""])[0].strip()
        if not raw:
            return list(allowed)
        want = {x.strip() for x in raw.split(",") if x.strip()}
        return [x for x in allowed if x in want]

    # Cap the list so one request cannot turn into a hundred index lookups.
    MAX_PLAYERS = 10

    def player_filter(self):
        """
        ?player=Name or ?player=A,B,C - matches that any of them played in.

        Both sources are consulted: tagpro.eu's player rows, and the names in
        the recordings themselves, which is the only place a name appears for
        a match the mirror never carried. The uuids land in a temp table
        because a player with a thousand games would otherwise blow past
        SQLite's parameter limit.

        Returns "" when no filter was asked for, or a SQL fragment to append
        to a WHERE clause that already has koala_matches aliased as k.
        """
        raw = (self.query().get("player") or [""])[0]
        names = []
        for n in raw.split(","):
            n = n.strip()
            if n and n not in names:
                names.append(n)
        if not names:
            return ""
        names = names[:self.MAX_PLAYERS]
        marks = ",".join("?" * len(names))
        conn = db()
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS pick (uuid TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM pick")
        conn.execute(
            f"INSERT OR IGNORE INTO pick (uuid) "
            f"SELECT m.koala_uuid FROM matches m JOIN match_players mp ON mp.match_id = m.match_id "
            f"WHERE m.koala_uuid IS NOT NULL AND mp.player_name COLLATE NOCASE IN ({marks})", names)
        conn.execute(
            f"INSERT OR IGNORE INTO pick (uuid) SELECT uuid FROM replay_players "
            f"WHERE display_name COLLATE NOCASE IN ({marks})", names)
        return " AND k.uuid IN (SELECT uuid FROM pick)"

    def bounds(self):
        """
        The slice of the archive being asked for, as [lo, hi) start times.

        ?start= and ?end= are match numbers: 1 is the oldest match in the
        archive, and the last is however many there are now. ?from= and ?to=
        take ISO dates instead. Neither given means everything. Returns
        (lo, hi, first_n, last_n) or None if the query is malformed.
        """
        q = self.query()
        get = lambda k: (q.get(k) or [""])[0].strip()
        conn = db()
        total = conn.execute("SELECT COUNT(*) FROM koala_matches").fetchone()[0]
        if not total:
            return None

        a, b = get("start"), get("end")
        if a or b:
            try:
                first = max(1, int(a or 1))
                last = min(total, int(b or total))
            except ValueError:
                return None
            if first > last:
                first, last = last, first
            nth = lambda n: conn.execute(
                "SELECT started FROM koala_matches ORDER BY started LIMIT 1 OFFSET ?",
                (n - 1,)).fetchone()[0]
            lo = nth(first)
            hi_started = nth(last)
            # hi is exclusive, so step past the last match's own instant
            hi = hi_started[:-1] + "1Z" if hi_started.endswith("Z") else hi_started + "1"
            return lo, hi, first, last

        f, t = get("from"), get("to")
        for v in (f, t):
            if v and not WEEK_RE.match(v):
                return None
        lo = f"{f}T00:00:00.000Z" if f else "0000"
        hi = ((dt.date.fromisoformat(t) + dt.timedelta(days=1)).isoformat() + "T00:00:00.000Z"
              if t else "9999")
        row = conn.execute(
            "SELECT COUNT(*) FROM koala_matches WHERE started < ?", (lo,)).fetchone()
        first = row[0] + 1
        last = conn.execute(
            "SELECT COUNT(*) FROM koala_matches WHERE started < ?", (hi,)).fetchone()[0]
        return lo, hi, first, max(first, last)

    def weeks_in(self, lo, hi, who=""):
        """Weeks the range touches, with each week's own clamped bounds."""
        rows = db().execute(
            "SELECT DISTINCT substr(k.started,1,10) FROM koala_matches k "
            "WHERE k.started >= ? AND k.started < ?" + who, (lo, hi)).fetchall()
        out = {}
        for (d,) in rows:
            w = monday(d)
            wlo, whi = week_bounds(w)
            out[w] = (max(wlo, lo), min(whi, hi))
        return [(w, out[w]) for w in sorted(out)]

    # ------------------------------------------------------------------ results

    def results_records(self, lo, hi, with_map, with_rebuilt, fields, stats, who=""):
        """
        How every match in the range ended, and what each player did in it.

        Provenance is explicit: record_source says whether the stats came from
        tagpro.eu or were rebuilt from the recording. Rebuilt ones are included
        by default here - this is a results file, not a mirror of tagpro.eu.
        """
        conn = db()
        rows = conn.execute(
            "SELECT k.uuid, k.game_id, k.started, k.map_name, k.map_type, k.server, "
            "       k.duration AS koala_duration_ms, k.winner, k.red_score AS k_red, "
            "       k.blue_score AS k_blue, m.match_id, m.duration, m.finished, "
            "       m.mode, m.season, m.red_score, m.blue_score, m.outcome, m.overtime, "
            "       m.mercy, m.void_reason, m.map_id, m.source "
            "FROM koala_matches k LEFT JOIN matches m ON m.koala_uuid = k.uuid "
            "WHERE k.started >= ? AND k.started < ?" + who + " ORDER BY k.started",
            (lo, hi)).fetchall()

        want = set(fields)
        maps = {}
        if with_map and "map" in want:
            for mp in conn.execute("SELECT map_id, name, author, type, width, height, marsballs FROM maps"):
                maps[mp["map_id"]] = dict(mp)

        out = []
        for r in rows:
            rebuilt = r["source"] == "replay"
            if rebuilt and not with_rebuilt:
                continue
            full = {
                "game_id": r["game_id"], "started": r["started"],
                "eu_match_id": r["match_id"],
                "record_source": None if r["match_id"] is None else
                                 ("replay" if rebuilt else "tagpro.eu"),
                "duration_ms": r["koala_duration_ms"], "duration_frames": r["duration"],
                "mode": r["mode"], "season": r["season"],
                "finished": None if r["finished"] is None else bool(r["finished"]),
                "outcome": r["outcome"], "void_reason": r["void_reason"],
                "overtime": r["overtime"], "mercy": r["mercy"],
                "score": {"red": r["red_score"] if r["red_score"] is not None else r["k_red"],
                          "blue": r["blue_score"] if r["blue_score"] is not None else r["k_blue"]},
                "winner": r["winner"], "server": r["server"],
            }
            rec = {"uuid": r["uuid"]}
            for k in self.MATCH_FIELDS:
                if k in want and k in full:
                    rec[k] = full[k]

            if with_map and "map" in want:
                rec["map"] = {"name": r["map_name"], "type": r["map_type"],
                              "eu_map_id": r["map_id"], "eu_map": maps.get(r["map_id"])}

            if "players" in want:
                if r["match_id"] is None:
                    rec["players"] = None
                else:
                    rec["players"] = [
                        dict({"name": pr["player_name"]},
                             **{k: pr[k] for k in stats})
                        for pr in conn.execute(
                            "SELECT * FROM match_players WHERE match_id = ? "
                            "ORDER BY team, player_name", (r["match_id"],))]

            # Ranked skill movement is keyed by koala uuid and by user id, which
            # does not map onto the player names above, so it stays its own block.
            if "ranked" in want or "ranked_players" in want:
                rd = conn.execute("SELECT game_mode, region, season, void_occurred_at, "
                                  "red_avg_skill, red_win_prob, blue_avg_skill, blue_win_prob "
                                  "FROM match_ranked_data WHERE uuid = ?", (r["uuid"],)).fetchone()
                if rd and "ranked" in want:
                    rec["ranked"] = dict(rd)
                if rd and "ranked_players" in want:
                    rec["ranked_players"] = [dict(x) for x in conn.execute(
                        "SELECT user_id, pre_skill, pre_tier, pre_sub_tier, post_skill, "
                        "post_tier, post_sub_tier, disconnected FROM match_ranked_players "
                        "WHERE uuid = ?", (r["uuid"],))]
            out.append(rec)
        return out

    def results_blob(self, lo, hi, with_map, with_rebuilt, fields, stats, who=""):
        recs = self.results_records(lo, hi, with_map, with_rebuilt, fields, stats, who)
        if not recs:
            return None
        return gzip.compress(json.dumps(recs, separators=(",", ":")).encode(), 6)

    def week_results(self, week, with_map, with_rebuilt):
        lo, hi = week_bounds(week)
        blob = self.results_blob(lo, hi, with_map, with_rebuilt,
                                 self.picked("fields", self.MATCH_FIELDS),
                                 self.picked("stats", self.PLAYER_FIELDS))
        if blob is None:
            return self._error(404, "no matches that week")
        self._send(200, "application/gzip", blob,
                   {"Content-Disposition": f'attachment; filename="results-{week}.json.gz"'})

    # ----------------------------------------------------------- tagpro.eu bulk

    def eu_rows(self, lo, hi, rebuilt, who=""):
        return db().execute(
            "SELECT m.* FROM koala_matches k JOIN matches m ON m.koala_uuid = k.uuid "
            "WHERE k.started >= ? AND k.started < ? " + who + " "
            + ("" if rebuilt else "AND m.raw_json IS NOT NULL ")
            + "ORDER BY k.started", (lo, hi)).fetchall()

    def eu_blob(self, lo, hi, rebuilt, who=""):
        rows = self.eu_rows(lo, hi, rebuilt, who)
        if not rows:
            return None
        conn = db()
        docs = {str(r["match_id"]): eu_bulk_doc(conn, r) for r in rows}
        return gzip.compress(json.dumps(docs, separators=(",", ":")).encode(), 6)

    def eu_week(self, week, rebuilt):
        """That week's tagpro.eu records, in tagpro.eu's own {match_id: doc} bulk shape."""
        lo, hi = week_bounds(week)
        blob = self.eu_blob(lo, hi, rebuilt)
        if blob is None:
            return self._error(404, "no tagpro.eu records held for that week")
        name = f"eu-{week}{'-with-rebuilt' if rebuilt else ''}.json.gz"
        self._send(200, "application/gzip", blob,
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    def bulk(self, rebuilt):
        """The whole tagpro.eu export. Without rebuilt it is a static file on
        disk; with it, the same shape generated live from the database."""
        if not rebuilt:
            if not BULK.is_file():
                return self._error(404, "bulk export not present")
            with transfer_slot(self.ip()):
                self._sendfile(BULK, "application/gzip", BULK.name)
            return
        rows = db().execute("SELECT * FROM matches ORDER BY match_id").fetchall()
        self.stream_bulk(rows, "ranked_matches_bulk-with-rebuilt.json.gz")

    def stream_bulk(self, rows, filename):
        with transfer_slot(self.ip()):
            self._head(200, "application/gzip", None,
                       {"Content-Disposition": f'attachment; filename="{filename}"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            gz = gzip.GzipFile(fileobj=w, mode="wb")
            conn = db()
            try:
                gz.write(b"{")
                for i, row in enumerate(rows):
                    if i:
                        gz.write(b",")
                    gz.write(json.dumps(str(row["match_id"])).encode())
                    gz.write(b":")
                    gz.write(json.dumps(eu_bulk_doc(conn, row), separators=(",", ":")).encode())
                gz.write(b"}")
            finally:
                gz.close()
                w.close()

    # ------------------------------------------------------------------- custom

    # Rough gzipped bytes per match, measured over a full week of each. Only
    # used to size a download before it starts; the estimate says it is one.
    RESULTS_BYTES_PER_MATCH = 530
    EU_BYTES_PER_MATCH = 3100

    def custom_selection(self):
        return {
            "replays": self.want("replays", False),
            "results": self.want("results", True),
            "eu": self.want("eu", False),
            "map": self.want("map", True),
            "rebuilt": self.want("rebuilt", True),
        }

    def custom_estimate(self):
        b = self.bounds()
        if b is None:
            return self._error(400, "bad range")
        lo, hi, first_n, last_n = b
        sel = self.custom_selection()
        fields = self.picked("fields", self.MATCH_FIELDS)
        stats = self.picked("stats", self.PLAYER_FIELDS)
        who = self.player_filter()
        row = db().execute(
            "SELECT COUNT(*) AS ids, "
            "  SUM(CASE WHEN r.uuid IS NOT NULL THEN 1 ELSE 0 END) AS held, "
            "  COALESCE(SUM(r.bytes_stored), 0) AS bytes, "
            "  MIN(k.started) AS first, MAX(k.started) AS last "
            "FROM koala_matches k LEFT JOIN replay_files r "
            "  ON r.uuid = k.uuid AND r.status = 'done' "
            "WHERE k.started >= ? AND k.started < ?" + who, (lo, hi)).fetchone()
        ids, held, rbytes = row["ids"], row["held"] or 0, row["bytes"] or 0
        # Fewer fields, smaller file - scale the per-match average by how much
        # of the record was actually asked for.
        share = (len(fields) / len(self.MATCH_FIELDS)) if fields else 0.0
        if "players" in fields and stats:
            share *= 0.5 + 0.5 * (len(stats) / len(self.PLAYER_FIELDS))
        total = 0
        if sel["replays"]:
            total += rbytes
        if sel["results"]:
            total += ids * self.RESULTS_BYTES_PER_MATCH * share
        if sel["eu"]:
            total += ids * self.EU_BYTES_PER_MATCH
        self._json({
            "matches": ids, "first_match": first_n, "last_match": last_n,
            "first_started": row["first"], "last_started": row["last"],
            "weeks": len(self.weeks_in(lo, hi, who)),
            "recordings": held, "recordings_bytes": rbytes, "bytes": int(total),
            # Recording bytes come off disk; the JSON parts are per-match
            # averages, so the total is only exact for recordings alone.
            "exact": sel["replays"] and not (sel["results"] or sel["eu"]),
            "total_matches": db().execute("SELECT COUNT(*) FROM koala_matches").fetchone()[0],
            "selection": sel, "fields": fields, "stats": stats,
            "players": [n.strip() for n in
                        (self.query().get("player") or [""])[0].split(",") if n.strip()],
        })

    def custom_tar(self):
        b = self.bounds()
        if b is None:
            return self._error(400, "bad range")
        lo, hi, first_n, last_n = b
        sel = self.custom_selection()
        if not (sel["replays"] or sel["results"] or sel["eu"]):
            return self._error(400, "pick at least one of replays, results, eu")
        fields = self.picked("fields", self.MATCH_FIELDS)
        stats = self.picked("stats", self.PLAYER_FIELDS)
        who = self.player_filter()
        players = [n.strip() for n in (self.query().get("player") or [""])[0].split(",") if n.strip()]
        weeks = self.weeks_in(lo, hi, who)
        if not weeks:
            return self._error(404, "nothing in that range")

        name = f"tagpro-archive-{first_n}-to-{last_n}.tar"
        with transfer_slot(self.ip()):
            self._head(200, "application/x-tar", None,
                       {"Content-Disposition": f'attachment; filename="{name}"'})
            if self.command == "HEAD":
                return
            w = self._chunks()
            tar = tarfile.open(fileobj=w, mode="w|")

            def add_bytes(arcname, blob):
                info = tarfile.TarInfo(arcname)
                info.size = len(blob)
                info.mtime = int(dt.datetime.now(dt.timezone.utc).timestamp())
                tar.addfile(info, io.BytesIO(blob))

            try:
                add_bytes("manifest.json", json.dumps({
                    "site": SITE,
                    "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "matches": [first_n, last_n], "weeks": [w for w, _ in weeks],
                    "selection": sel, "fields": fields, "stats": stats,
                    "players": players,
                }, indent=1).encode())

                for week, (wlo, whi) in weeks:
                    if sel["results"]:
                        blob = self.results_blob(wlo, whi, sel["map"], sel["rebuilt"], fields, stats, who)
                        if blob:
                            add_bytes(f"results/{week}.json.gz", blob)
                    if sel["eu"]:
                        blob = self.eu_blob(wlo, whi, sel["rebuilt"], who)
                        if blob:
                            add_bytes(f"eu/{week}.json.gz", blob)
                    if sel["replays"]:
                        for r in self.range_rows(wlo, whi, who):
                            p = replay_path(r)
                            if not p:
                                continue
                            info = tar.gettarinfo(
                                str(p), arcname=f"replays/{week}/{r['uuid']}.ndjson.gz")
                            with open(p, "rb") as fh:
                                tar.addfile(info, fh)
            finally:
                tar.close()
                w.close()
                log.info("custom: matches %d-%d, %s", first_n, last_n,
                         ",".join(k for k, v in sel.items() if v))

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
                "/manifest.json",
                "/replay/<uuid>",
                "/week/<YYYY-MM-DD>/replays.tar",
                "/week/<YYYY-MM-DD>/results.json.gz[?map=0][?rebuilt=0]",
                "/all/replays.tar",
                "/custom.tar?from=&to=&replays=&results=&eu=&map=&rebuilt=",
                "/custom/estimate?<same query>",
                "/eu/week/<YYYY-MM-DD>.json.gz[?rebuilt=1]",
                "/bulk/ranked_matches_bulk.json.gz[?rebuilt=1]",
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
<tr><td>/replay/&lt;uuid&gt;</td><td>one recording, <code>.ndjson.gz</code>, resumable</td></tr>
<tr><td>/week/&lt;YYYY-MM-DD&gt;/results.json.gz</td><td>how every match that week ended, with per-player stats and the map; <code>?map=0</code>, <code>?rebuilt=0</code></td></tr>
<tr><td>/week/&lt;YYYY-MM-DD&gt;/replays.tar</td><td>every recording that week, streamed as one tar</td></tr>
<tr><td>/all/replays.tar</td><td>every recording held, {gb:.1f} GB, foldered by week</td></tr>
<tr><td>/custom.tar</td><td>pick a range and what to include: <code>?start=1&amp;end=5000&amp;replays=1&amp;results=1&amp;eu=1&amp;player=Name&amp;fields=&hellip;&amp;stats=&hellip;</code></td></tr>
<tr><td>/custom/estimate</td><td>same query, returns counts and a size estimate first</td></tr>
<tr><td>/eu/week/&lt;YYYY-MM-DD&gt;.json.gz</td><td>that week's tagpro.eu records, in tagpro.eu bulk shape</td></tr>
<tr><td>/bulk/ranked_matches_bulk.json.gz</td><td>the whole tagpro.eu export in one file</td></tr>
<tr><td colspan=2 class=n>Both take <code>?rebuilt=1</code> to mix in the records rebuilt from
recordings. Off by default - see the site's Rebuilt records page.</td></tr>
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
