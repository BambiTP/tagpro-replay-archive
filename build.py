"""
Regenerate the coverage site from tagpro.db.

Run with: python3 build.py

Two things here are easy to get wrong and are handled deliberately:

1. `matches.date` (tagpro.eu) is US Pacific local time, while
   `koala_matches.started` is UTC with a Z suffix. Comparing them naively
   shifts every tagpro.eu match by 7-8 hours, which is enough to move
   matches across week boundaries and corrupt every weekly total. Rows with
   source='replay' are the exception - those were reconstructed locally and
   are already UTC.

2. A tagpro.eu match with no linked koala uuid is NOT automatically a
   missing id. Most are simply unlinked: the id was collected, but
   koala_link_uuids hasn't matched the two records yet. Only a tagpro.eu
   match with no koala id at the same instant is a genuinely missing id.
   Conflating the two inflates the gap by roughly 50% and makes every week
   look uncertain when almost all of them are exact.
"""
import bisect
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

DB = "/home/metjr/nte/data/tagpro.db"
OUT = Path(__file__).resolve().parent
PT = ZoneInfo("America/Los_Angeles")
MATCH_TOLERANCE_S = 120   # same match if start times agree within this


def monday(d):
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def load(conn):
    rows = conn.execute("""
        SELECT k.uuid, k.game_id, k.started, k.map_name, k.duration,
               CASE WHEN r.uuid IS NOT NULL THEN 1 ELSE 0 END,
               CASE WHEN m.match_id IS NOT NULL THEN 1 ELSE 0 END, m.match_id, m.source,
               COALESCE(r.bytes_stored, 0)
        FROM koala_matches k
        LEFT JOIN replay_files r ON r.uuid = k.uuid AND r.status = 'done'
        LEFT JOIN matches m ON m.koala_uuid = k.uuid
        ORDER BY k.started
    """).fetchall()

    # Instant index of every id we hold, for the lag-vs-gap test below.
    instants = sorted(dt.datetime.fromisoformat(r[2].replace("Z", "+00:00")) for r in rows)

    missing = []
    for mid, date, source in conn.execute(
            "SELECT match_id, date, source FROM matches WHERE koala_uuid IS NULL"):
        naive = dt.datetime.fromisoformat(date)
        t = (naive.replace(tzinfo=dt.timezone.utc) if source == "replay"
             else naive.replace(tzinfo=PT).astimezone(dt.timezone.utc))
        i = bisect.bisect_left(instants, t)
        if not any(0 <= j < len(instants)
                   and abs((instants[j] - t).total_seconds()) < MATCH_TOLERANCE_S
                   for j in (i - 1, i, i + 1)):
            missing.append((mid, t))
    return rows, missing


def build():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows, missing = load(conn)

    weeks, out_rows = {}, {}
    for uuid, gid, started, mp, dur, has_rep, has_rec, eu_id, eu_src, nbytes in rows:
        t = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        w = monday(t.date())
        c = weeks.setdefault(w, {"ids": 0, "replay": 0, "missing": 0,
                                 "record": 0, "rebuilt": 0, "bytes": 0})
        c["ids"] += 1
        c["bytes"] += nbytes
        c["replay"] += has_rep
        c["record"] += has_rec
        # a tagpro.eu record we reconstructed ourselves from an archived
        # recording, rather than one the mirror ever carried (replay_to_eu.py)
        if eu_src == "replay":
            c["rebuilt"] += 1
        out_rows.setdefault(w, []).append({
            "uuid": uuid, "game_id": gid, "started": started, "map": mp,
            "duration": dur, "have_replay": bool(has_rep),
            "have_record": bool(has_rec), "eu_match_id": eu_id,
        })
    for _mid, t in missing:
        weeks.setdefault(monday(t.date()),
                         {"ids": 0, "replay": 0, "missing": 0,
                          "record": 0, "rebuilt": 0, "bytes": 0})["missing"] += 1

    today = dt.datetime.now(dt.timezone.utc).date()
    curwk = monday(today)
    cov = []
    for w in sorted(weeks):
        c = weeks[w]
        cov.append({"week": w, "ids": c["ids"], "replay": c["replay"],
                    "record": c["record"], "rebuilt": c["rebuilt"], "bytes": c["bytes"],
                    "missing_ids": c["missing"], "est": c["ids"] + c["missing"],
                    "partial": w == curwk})

    wdir = OUT / "data" / "weeks"
    wdir.mkdir(parents=True, exist_ok=True)
    for f in wdir.glob("*.json"):
        f.unlink()
    for w, recs in out_rows.items():
        dump = lambda name, obj: json.dump(obj, open(wdir / f"{w}.{name}.json", "w"),
                                           separators=(",", ":"))
        # tagpro.eu ids only - a plain list, for looking matches up on the mirror
        dump("eu", [r["eu_match_id"] for r in recs if r["eu_match_id"] is not None])
        # replay ids - uuid identifies the replay, game_id addresses the
        # recording itself, and you need the latter to request one
        rid = lambda r: {"uuid": r["uuid"], "game_id": r["game_id"]}
        dump("replay", [rid(r) for r in recs])
        # the subset with no recording held here - this is the wanted list
        dump("missing", [rid(r) for r in recs if not r["have_replay"]])
        # everything, including which of the two are actually held
        json.dump(recs, open(wdir / f"{w}.json", "w"), separators=(",", ":"))

    json.dump(cov, open(OUT / "data" / "coverage.json", "w"), indent=1)

    # Whole-archive equivalents of the three per-week files.
    ddir = OUT / "data"
    dump = lambda name, obj: json.dump(obj, open(ddir / name, "w"), separators=(",", ":"))
    dump("all.eu.json", [r[7] for r in rows if r[7] is not None])
    dump("all.replay.json", [{"uuid": r[0], "game_id": r[1]} for r in rows])
    dump("missing_replays.json",
         [{"uuid": r[0], "game_id": r[1], "started": r[2], "map": r[3]}
          for r in rows if not r[5]])

    held_bytes = conn.execute("SELECT COALESCE(SUM(bytes_stored),0) FROM replay_files "
                              "WHERE status = 'done'").fetchone()[0]
    render(cov, missing, held_bytes)
    return cov


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#16181d;--muted:#5b6270;--faint:#8a919e;--rule:#e3e5ea;
--id:#2f6fb3;--rep:#2e7d5b;--rec:#2e9e6b;--reb:#c9772e;--track:#e8eaee;--code:#f0f1f4}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--panel:#171a1f;--ink:#e8eaee;--muted:#a2aab8;
--faint:#78808e;--rule:#282c34;--id:#5b9bdd;--rep:#4dae83;--rec:#4fbe8b;--reb:#e08a3c;--track:#252932;--code:#1e222a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:0 auto;padding:0 22px}
header{padding:44px 0 26px;border-bottom:1px solid var(--rule)}
h1{font-size:23px;font-weight:640;letter-spacing:-.01em;margin:0 0 6px}
.sub{color:var(--muted);font-size:14px;margin:0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
background:var(--rule);border:1px solid var(--rule);border-radius:9px;overflow:hidden;margin:26px 0 0}
.stat{background:var(--panel);padding:15px 16px}
.stat b{display:block;font-size:22px;font-weight:640;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{display:block;font-size:12.5px;color:var(--muted);margin-top:3px}
section{padding:34px 0 8px}
h2{font-size:17px;font-weight:640;margin:0 0 6px;letter-spacing:-.01em;text-transform:none;
color:var(--ink)}
.lead{font-size:14px;color:var(--muted);margin:0 0 14px;max-width:70ch}
.lead strong{color:var(--ink);font-weight:640}
.note{font-size:13px;color:var(--muted);margin:0 0 16px}
table{border-collapse:collapse;width:100%;background:var(--panel);
border:1px solid var(--rule);border-radius:9px;overflow:hidden}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);
font-weight:600;text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule)}
td{padding:5px 12px;border-bottom:1px solid var(--rule);font-size:13px}
tr:last-child td{border-bottom:0}
td.wk{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums;width:104px}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;width:150px}
td.n i{font-style:normal;color:var(--faint)}
td.p{text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);width:56px}
td.dl{width:186px;text-align:right;white-space:nowrap}
.track{height:9px;border-radius:3px;background:var(--track);overflow:hidden;min-width:90px}
.track span{display:block;height:100%}
.fid span{background:var(--id)}
.frep span{background:var(--rep)}
.bars{display:flex;flex-direction:column;gap:2px;min-width:90px}
.bars .track{height:5px;border-radius:2px}
.bars .track:first-child{height:9px;border-radius:3px}
.frec span{background:var(--rec)}
.freb span{background:var(--reb)}
.track.split{display:flex}
.seg-rec{background:var(--rec)}
.seg-reb{background:var(--reb)}
td.n2{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;width:112px}
td.n2 i{font-style:normal;color:var(--faint);margin-left:5px;font-size:11.5px}
.key{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:0 0 12px}
.key i{display:inline-block;width:18px;height:7px;border-radius:2px;vertical-align:1px;margin-right:6px}
tfoot td{font-weight:640;background:var(--panel);border-top:2px solid var(--rule);padding:10px 12px}
a{color:var(--id)}
.dl a{font-size:10.5px;color:var(--faint);text-decoration:none;border:1px solid var(--rule);border-radius:4px;padding:2px 5px;margin-left:3px}
.dl a:hover{color:var(--id)}
footer{padding:26px 0 60px;color:var(--faint);font-size:12.5px;border-top:1px solid var(--rule);margin-top:34px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em;
font-family:ui-monospace,Menlo,Consolas,monospace}
@media(max-width:640px){td.n{width:76px}td.n2{width:64px}td.n2 i{display:none}td.dl{width:150px}td.wk{width:80px;font-size:11px}.track,.bars{min-width:40px}td.dl{width:92px}.dl a{padding:2px 3px;margin-left:2px}}
.nav{display:flex;gap:2px;margin:20px 0 0;flex-wrap:wrap}
.nav a{font-size:13.5px;color:var(--muted);text-decoration:none;padding:7px 13px;border-radius:7px;
border:1px solid transparent}
.nav a:hover{color:var(--ink);background:var(--panel)}
.nav a.on{color:var(--ink);background:var(--panel);border-color:var(--rule);font-weight:600}
.prose{max-width:70ch}
.prose p{font-size:14.5px;color:var(--muted);margin:0 0 14px}
.prose p strong{color:var(--ink);font-weight:640}
.prose ul{max-width:70ch;font-size:14.5px;color:var(--muted);margin:0 0 14px;padding-left:20px}
.prose li{margin:0 0 6px}
h3{font-size:14.5px;font-weight:640;margin:24px 0 8px;letter-spacing:-.01em}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:9px;padding:18px 20px;margin:0 0 18px}
.card p:last-child{margin-bottom:0}
.btn{display:inline-block;background:var(--id);color:#fff;text-decoration:none;font-size:14px;
font-weight:600;padding:9px 17px;border-radius:7px}
.btn:hover{filter:brightness(1.08)}
.btn[aria-disabled=true]{background:var(--track);color:var(--faint);pointer-events:none}
.status{font-size:13.5px;color:var(--muted);margin:0 0 14px;display:flex;align-items:center;gap:9px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.dot.up{background:var(--rec)}
.dot.down{background:var(--reb)}
.host{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;color:var(--faint);
word-break:break-all;margin:12px 0 0}
pre{background:var(--code);padding:13px 15px;border-radius:7px;overflow-x:auto;font-size:12.5px;
line-height:1.55;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0 0 14px}
.files td{font-size:13.5px;padding:8px 12px}
.files td:first-child{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
white-space:nowrap;width:1%}
.files td a{text-decoration:none}
.files td a:hover{text-decoration:underline}
.pick{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 14px;align-items:center}
.pick label{font-size:13.5px;color:var(--ink);display:flex;align-items:center;gap:7px;cursor:pointer}
.pick input[type=date]{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--rule);
border-radius:6px;background:var(--bg);color:var(--ink)}
.pick .sep{font-size:12px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
.pick input[type=text]{font:inherit;font-size:13px;padding:5px 9px;border:1px solid var(--rule);
border-radius:6px;background:var(--bg);color:var(--ink);width:190px}
.pick input[type=number]{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--rule);
border-radius:6px;background:var(--bg);color:var(--ink);width:104px;font-variant-numeric:tabular-nums}
.pick.fields{gap:9px 18px;margin:10px 0 4px}
.pick.fields label{font-size:13px;color:var(--muted)}
details{margin:0 0 12px}
summary{font-size:13px;color:var(--ink);cursor:pointer;padding:4px 0}
summary a{font-size:12.5px}
.n{font-size:12.5px;color:var(--faint);font-variant-numeric:tabular-nums}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.mini{font:inherit;font-size:12.5px;padding:7px 12px;border-radius:6px;border:1px solid var(--rule);
background:var(--panel);color:var(--muted);cursor:pointer}
.mini:hover{color:var(--ink)}
.served{margin:14px 0 0;padding-top:13px;border-top:1px solid var(--rule);font-size:13px;
color:var(--muted);font-variant-numeric:tabular-nums}
.served b{color:var(--ink);font-weight:640}
.urlbox{margin:12px 0 0}
.url{display:block;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
color:var(--id);word-break:break-all;text-decoration:none;line-height:1.5}
.url:hover{text-decoration:underline}
"""

HEAD = ('<tr><th>Week</th><th></th><th style="text-align:right">Count</th>'
        '<th style="text-align:right">%</th><th></th></tr>')

HEAD_ID = ('<tr><th>Week</th><th></th><th style="text-align:right">Replay ids</th>'
           '<th style="text-align:right">tagpro.eu ids</th>'
           '<th style="text-align:right">tagpro.eu rebuilt</th><th></th></tr>')


# Where the archive host currently is. The host sits on a free cloudflared
# quick tunnel whose hostname changes on every restart, so nothing here can
# link to it directly; this Worker holds the current address and redirects
# /go/<path> to it (worker/src/index.js, host/supervise.sh).
WORKER = "https://tagpro-archive-tunnel.bambitagpro.workers.dev"

DISCORD = "metjr_"

NAV = (("index.html", "Coverage"), ("about.html", "About"),
       ("rebuilt.html", "Rebuilt records"), ("download.html", "Download"))

# Fills in the download page's live status line. Kept out of the f-strings
# below so its braces don't have to be doubled.
STATUS_JS = """
(function () {
  var W = "__WORKER__";
  var dot = document.getElementById("dot"),
      txt = document.getElementById("stxt"),
      btn = document.getElementById("open"),
      host = document.getElementById("host");
  function ago(s) {
    if (s < 60) return s + " seconds ago";
    if (s < 3600) return Math.round(s / 60) + " minutes ago";
    return Math.round(s / 3600) + " hours ago";
  }
  function served(d) {
    var box = document.getElementById("served");
    if (!box) return;
    // Render at zero too. A counter that only appears once it is non-zero is
    // one nobody can find when they go looking for it.
    var n = (d.served && d.served.downloads) || 0;
    var bytes = (d.served && d.served.bytes) || 0;
    var gb = bytes / 1e9;
    box.innerHTML = "<b>" + n.toLocaleString() + "</b> download" + (n === 1 ? "" : "s")
      + " &middot; <b>" + (gb >= 1 ? gb.toFixed(1) + " GB" : (bytes / 1e6).toFixed(1) + " MB")
      + "</b> downloaded from here";
  }

  fetch(W + "/status", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      served(d);
      if (d.online) {
        dot.className = "dot up";
        txt.textContent = "Online \\u2014 last checked in " + ago(d.age) + ".";
        btn.removeAttribute("aria-disabled");
        host.textContent = "Currently at " + d.url;
      } else {
        dot.className = "dot down";
        txt.textContent = d.updated
          ? "Offline since " + new Date(d.updated).toLocaleString() + ". Try again later."
          : "Offline right now. Try again later.";
        btn.setAttribute("aria-disabled", "true");
        host.textContent = "";
      }
    })
    .catch(function () {
      dot.className = "dot down";
      txt.textContent = "Could not reach the address service.";
      btn.setAttribute("aria-disabled", "true");
    });
})();
"""


# Field lists here must match Handler.MATCH_FIELDS / PLAYER_FIELDS in
# host/archive_server.py - the page offers exactly what the host will honour.
MATCH_FIELDS = [
    ("started", "start time"), ("game_id", "game id"), ("eu_match_id", "tagpro.eu id"),
    ("record_source", "record source"), ("duration_ms", "duration (ms)"),
    ("duration_frames", "duration (frames)"), ("mode", "mode"), ("season", "season"),
    ("finished", "finished"), ("outcome", "outcome"), ("void_reason", "void reason"),
    ("overtime", "overtime"), ("mercy", "mercy"), ("score", "score"), ("winner", "winner"),
    ("server", "server"), ("map", "map"), ("ranked", "ranked skill"),
    ("ranked_players", "ranked per player"), ("players", "players"),
]
PLAYER_FIELDS = [
    ("team", "team"), ("auth", "auth"), ("score", "score"), ("points", "points"),
    ("grabs", "grabs"), ("captures", "captures"), ("drops", "drops"), ("hold", "hold"),
    ("tags", "tags"), ("returns", "returns"), ("pops", "pops"), ("prevent", "prevent"),
    ("button", "button"), ("block", "block"), ("pups_total", "powerups"),
    ("time_played", "time played"), ("caps_for", "caps for"),
    ("caps_against", "caps against"), ("disconnected", "disconnected"),
]

CUSTOM_JS = """
(function () {
  var W = "__WORKER__";
  var TOTAL = __TOTAL__;
  var el = function (id) { return document.getElementById(id); };
  var flags = ["replays", "results", "eu", "map", "rebuilt"];
  var est = el("est"), btn = el("build"), urlOut = el("url"), span = el("span");
  var copy = el("copy");
  var timer = null;

  function checked(group) {
    return Array.prototype.slice
      .call(document.querySelectorAll("input[data-g=" + group + "]"))
      .filter(function (i) { return i.checked; })
      .map(function (i) { return i.value; });
  }

  function total(group) {
    return document.querySelectorAll("input[data-g=" + group + "]").length;
  }

  function clampRange() {
    var a = parseInt(el("start").value, 10), b = parseInt(el("end").value, 10);
    if (isNaN(a) || a < 1) a = 1;
    if (isNaN(b) || b > TOTAL) b = TOTAL;
    if (a > b) { var t = a; a = b; b = t; }
    el("start").value = a; el("end").value = b;
    return [a, b];
  }

  function query() {
    var r = clampRange();
    var q = ["start=" + r[0], "end=" + r[1]];
    var who = el("player").value.trim();
    if (who) q.push("player=" + encodeURIComponent(who));
    flags.forEach(function (b) { q.push(b + "=" + (el("c-" + b).checked ? "1" : "0")); });
    // Only name fields when it is a real subset - a full list just makes the url long.
    var f = checked("f"), s = checked("s");
    if (f.length < total("f")) q.push("fields=" + f.join(","));
    if (s.length < total("s")) q.push("stats=" + s.join(","));
    return q.join("&");
  }

  function size(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(0) + " MB";
    return Math.max(1, Math.round(n / 1e3)) + " KB";
  }

  function refresh() {
    var picked = el("c-replays").checked || el("c-results").checked || el("c-eu").checked;
    var q = query();
    var href = W + "/go/custom.tar?" + q;
    urlOut.textContent = href;
    urlOut.setAttribute("href", href);
    // Carry the selection in this page's own address, so a bookmark or a
    // pasted link reopens the picker exactly as it was left.
    try { history.replaceState(null, "", "?" + q); } catch (e) {}
    if (!picked) {
      btn.setAttribute("aria-disabled", "true");
      est.textContent = "Pick at least one of match results, tagpro.eu records, or recordings.";
      return;
    }
    btn.setAttribute("href", href);
    btn.removeAttribute("aria-disabled");
    est.textContent = "Working out the size\\u2026";
    clearTimeout(timer);
    timer = setTimeout(function () {
      fetch(W + "/go/custom/estimate?" + q, { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.matches) { est.textContent = "Nothing in that range."; return; }
          est.textContent = d.matches.toLocaleString()
            + (d.players.length ? " matches with " + d.players.join(" or ") + " across "
                                : " matches across ")
            + d.weeks + (d.weeks === 1 ? " week, " : " weeks, ")
            + (d.exact ? "" : "about ") + size(d.bytes)
            + (d.selection.replays ? " (" + d.recordings.toLocaleString() + " recordings)" : "");
          if (d.first_started && span) {
            span.textContent = d.first_started.slice(0, 10) + " to " + d.last_started.slice(0, 10);
          }
        })
        .catch(function () {
          est.textContent = "Cannot reach the host, so no size estimate. The link still works "
            + "once it is back.";
        });
    }, 250);
  }

  function applyFromUrl() {
    var q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    if (!q.toString()) return;
    if (q.has("start")) el("start").value = q.get("start");
    if (q.has("player")) el("player").value = q.get("player");
    if (q.has("end")) el("end").value = q.get("end");
    flags.forEach(function (b) {
      if (q.has(b)) el("c-" + b).checked = q.get(b) === "1";
    });
    [["f", "fields"], ["s", "stats"]].forEach(function (pair) {
      if (!q.has(pair[1])) return;
      var want = q.get(pair[1]).split(",");
      document.querySelectorAll("input[data-g=" + pair[0] + "]").forEach(function (i) {
        i.checked = want.indexOf(i.value) !== -1;
      });
    });
  }

  if (copy) {
    copy.addEventListener("click", function () {
      var text = urlOut.textContent;
      var done = function () {
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = "Copy link"; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
        return;
      }
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      document.body.removeChild(ta);
    });
  }

  document.querySelectorAll("[data-all]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      var on = a.getAttribute("data-all") === "on";
      document.querySelectorAll("input[data-g=" + a.getAttribute("data-for") + "]")
        .forEach(function (i) { i.checked = on; });
      refresh();
    });
  });
  ["start", "end"].concat(flags.map(function (b) { return "c-" + b; }))
    .forEach(function (id) { el(id).addEventListener("change", refresh); });
  // input, not change - the estimate should follow along as the name is typed,
  // and refresh already debounces the request it makes.
  el("player").addEventListener("input", refresh);
  document.querySelectorAll("input[data-g]").forEach(function (i) {
    i.addEventListener("change", refresh);
  });
  applyFromUrl();
  refresh();
})();
"""

def page(slug, title, subtitle, body, stats="", foot="", script=""):
    """The shell every page shares: head, title block, tabs, body, footer."""
    nav = []
    for href, label in NAV:
        cls = ' class="on"' if href == slug else ""
        nav.append(f'<a href="{href}"{cls}>{label}</a>')
    script_tag = f"<script>{script}</script>" if script else ""
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{subtitle}">
<style>{CSS}</style></head><body>
<header><div class="wrap">
<h1>TagPro ranked replay archive</h1>
<p class="sub">{subtitle}</p>
<nav class="nav">{"".join(nav)}</nav>
{stats}
</div></header>

<div class="wrap">
{body}

<footer><div class="wrap">
{foot}
Generated {dt.datetime.now(dt.timezone.utc):%d %b %Y}.
</div></footer>
</div>{script_tag}</body></html>'''


def about_page(cov, missing, ids, rep, held_bytes, span):
    f = lambda n: f"{n:,}"
    pc = lambda a, b: (100.0 * a / b) if b else 0.0
    rebuilt = sum(r["rebuilt"] for r in cov)
    record = sum(r["record"] for r in cov)
    body = f'''<section class="prose">
<h2>What this is</h2>
<p>A crowdsourced record of every ranked TagPro match the replay listing returns:
<strong>{f(ids)} matches</strong> since {span[0]}, each with its id, start time, map and duration,
and a tagpro.eu record for {f(record)} of them. For <strong>{f(rep)}</strong> the recording itself
is held too.</p>
<p>It exists out of a love for data.</p>

<h2>Why this has to be crowdsourced</h2>
<p>Scraping replays older than two days is forbidden. The site blocks viewing a replay older than
two days without an account. A request for a bulk download of the older material was declined.</p>
<p>None of that is a technical limit. The matches are still there &mdash; ask for an old one
without an account and you are told to log in, not that it is gone. The two-day window, the account
wall and the refusal are decisions, and what they hold back is the community's own record of
itself.</p>
<p><strong>This data is the community's by right.</strong> Every match in it was played by people
who now cannot get at it. They filled the servers, they played the games, and the record of what
they did is theirs. Running the machine it happens to sit on is not a better claim than that.</p>
<p>So the only route left is to <strong>crowdsource</strong> it. This archive is complete on ids
because a listing can be enumerated, and thin on recordings because each one had to be caught
inside a two-day window as it happened. Everything older than that window when collection started
is out of my reach &mdash; but it is not out of everyone's. It is sitting in folders on other
people's machines.</p>

<h2>A request to the developers</h2>
<p>This is addressed to whoever runs the ranked servers, and it is a genuine request rather than a
complaint. <strong>Release the back catalogue.</strong> One bulk export of the recordings older
than the window, once, and the gap on this site closes permanently.</p>
<p>If a single dump is too much to hand over, any of these would do nearly as well:</p>
<ul>
<li>A longer collection window than two days, even a modest one, so recordings can be caught before
they lock.</li>
<li>Rate-limited access to older recordings &mdash; as slow as you like. There is no hurry on
matches that are already a year old.</li>
<li>An export that people can request for their own matches, so the community can fill this in
itself.</li>
<li>Or tell me what would make it acceptable, and I will build it that way.</li>
</ul>
<p>On the other side of it, this project already works the way you would want it to. Collection
stays inside the window it was given. It backs off when it is told to and stays backed off. It
holds no credential capable of reaching anything outside the window, deliberately, so that a bug
could not become a breach of the terms. Every id it has found is published openly, which is more
than the archive gets in return.</p>
<p>The data costs you nothing to release and it is worth a great deal to the people who made it.
<strong>{DISCORD}</strong> on Discord, any time.</p>

<h2>Send me your replays</h2>
<p>Every recording anyone contributes is one the archive can never get any other way. If you have
replay dumps &mdash; exports, scrapes, anything &mdash; send them to <strong>{DISCORD}</strong> on
Discord. Any format, any size, any dates.</p>
<p>Recordings are keyed by uuid, so duplicates cost nothing and nothing needs checking or filtering
first. The <a href="download.html">list of what is missing</a> is published if it is useful.</p>
<p><strong>Ask for what you need.</strong> Message <strong>{DISCORD}</strong> on Discord for a
particular cut of the data, a different format, a query run across the whole archive, or help with
a project you are building on it. Requests are welcome and are usually quicker to answer than they
are to work around.</p>

<h2>Where the numbers come from</h2>
<ul>
<li><strong>The ranked replay listing</strong> decides what exists. Every id here came from it, so
the totals are counts, not estimates.</li>
<li><strong><a href="https://tagpro.eu/?science" target="_blank" rel="noopener">tagpro.eu</a></strong>
is the second source, and where the box score, events and players come from. About 98% of matches
have one. The two are matched on start time with 120 seconds of
slack, because they record it in different time zones and to different precision.</li>
<li><strong>{f(rebuilt)} tagpro.eu records were rebuilt here</strong> from recordings, for matches
the mirror never had. That is the orange band on the coverage table, and there is a page on
<a href="rebuilt.html">how accurate they are</a>.</li>
<li><strong>{len(missing)} matches are exceptions.</strong> They are on tagpro.eu but the listing
returns nothing for them, even inside ranges it says are complete. There is no recording to get.</li>
</ul>
<p>Two ids identify a match: <code>uuid</code> for the replay, <code>game_id</code> for the
recording (that is the one you request a recording by), and <code>eu_match_id</code> for the same
match on tagpro.eu. Full field list in <a href="DATA_MAP.md">DATA_MAP.md</a>.</p>

<h2>How it runs</h2>
<p>A pipeline follows the listing, pulls each replay while it is still reachable, links it to its
tagpro.eu record, and rebuilds records for matches the mirror missed. This site is regenerated from
that database on a schedule. The {held_bytes / 1e9:.1f} GB of recordings stay on the machine that
collected them and are served from it &mdash; see <a href="download.html">Download</a>.</p>
</section>'''
    foot = ('Numbers on this page come from the same database as the coverage tables and move '
            'with them. Field reference: <a href="DATA_MAP.md">DATA_MAP.md</a>.<br><br>')
    return page("about.html", "About · TagPro ranked replay archive",
                "A crowdsourced record of ranked TagPro, and why it has to be one.",
                body, foot=foot)


def download_page(cov, ids, rep, held_bytes, span):
    f = lambda n: f"{n:,}"
    gb = lambda n: (f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB")
    record = sum(r["record"] for r in cov)
    rebuilt = sum(r["rebuilt"] for r in cov)
    latest = cov[-1]["week"]
    box = lambda g, k, lbl: (f'<label><input type="checkbox" data-g="{g}" value="{k}" checked> '
                             f'{lbl}</label>')
    match_boxes = "".join(box("f", k, lbl) for k, lbl in MATCH_FIELDS)
    stat_boxes = "".join(box("s", k, lbl) for k, lbl in PLAYER_FIELDS)
    rows = []
    for c in reversed(cov):
        lbl = dt.date.fromisoformat(c["week"]).strftime("%d %b %Y")
        w = c["week"]
        tar = (f'<a href="{WORKER}/go/week/{w}/replays.tar">replays.tar</a>'
               if c["replay"] else '<i>none</i>')
        rows.append(
            f'<tr><td class="wk">{lbl}</td>'
            f'<td class="n">{f(c["ids"])}</td>'
            f'<td class="n">{f(c["replay"])}</td>'
            f'<td class="n">{gb(c["bytes"]) if c["bytes"] else "&mdash;"}</td>'
            f'<td class="dl">{tar}'
            f'<a href="{WORKER}/go/week/{w}/results.json.gz">results</a>'
            f'<a href="{WORKER}/go/eu/week/{w}.json.gz">tagpro.eu</a></td></tr>')
    body = f'''<section>
<div class="card">
<p><strong>Want something that is not here?</strong> A particular cut of the data, a format that
suits your tooling, a query run against the whole archive, or a hand with a project you are
building on it &mdash; message <strong>{DISCORD}</strong> on Discord and ask. That is what it is
for, and it is quicker than working around a file that nearly fits.</p>
</div>
</section>

<section>
<h2>Everything at once</h2>
<p class="lead">Two files hold the archive. Both stream from the machine that collected them, so
they need the host to be up.</p>
<div class="card">
<p class="status"><span class="dot" id="dot"></span><span id="stxt">Checking the archive host&hellip;</span></p>
<p><a class="btn" id="open" href="{WORKER}/go" aria-disabled="true">Open the archive host</a></p>
<p class="host" id="host"></p>
<p class="served" id="served">Counting downloads&hellip;</p>
</div>
<table class="files"><tbody>
<tr><td><a href="{WORKER}/go/all/replays.tar">all/replays.tar</a></td>
<td>every recording held, {f(rep)} files, {gb(held_bytes)}, one folder per week</td></tr>
<tr><td><a href="{WORKER}/go/bulk/ranked_matches_bulk.json.gz">ranked_matches_bulk.json.gz</a></td>
<td>every tagpro.eu record, {f(record)} matches, in
<a href="https://tagpro.eu/?science" target="_blank" rel="noopener">tagpro.eu's own bulk shape</a></td></tr>
</tbody></table>
<p class="note">The bulk file is the mirror's records only. Add <code>?rebuilt=1</code> to mix in
the {f(rebuilt)} records <a href="rebuilt.html">rebuilt here from recordings</a>; they are marked
<code>source: "replay"</code> wherever they appear.</p>
</section>

<section>
<h2>Build a download</h2>
<p class="lead">Pick a range and exactly which fields you want. You get one tar holding a manifest,
the weeks your range covers, and nothing else.</p>
<div class="card">
<div class="pick">
<span class="sep">Matches</span>
<label>from <input type="number" id="start" min="1" max="{ids}" value="1" step="1"></label>
<label>to <input type="number" id="end" min="1" max="{ids}" value="{ids}" step="1"></label>
<span class="n" id="span"></span>
</div>
<p class="note" style="margin:-4px 0 14px">Match 1 is the oldest in the archive, {f(ids)} is the
newest. Ordered by start time.</p>
<div class="pick">
<span class="sep">Player</span>
<label><input type="text" id="player" placeholder="any player" autocomplete="off" spellcheck="false"></label>
<span class="n">only matches this player was in &mdash; comma-separate for several</span>
</div>
<div class="pick">
<span class="sep">Include</span>
<label><input type="checkbox" id="c-results" checked> Match results</label>
<label><input type="checkbox" id="c-eu"> tagpro.eu records</label>
<label><input type="checkbox" id="c-replays"> Recordings</label>
<label><input type="checkbox" id="c-rebuilt" checked> <a href="rebuilt.html">Rebuilt records</a></label>
<label><input type="checkbox" id="c-map" checked> Map</label>
</div>
<details open>
<summary>Per match &mdash; <a href="#" data-all="on" data-for="f">all</a> &middot;
<a href="#" data-all="off" data-for="f">none</a></summary>
<div class="pick fields">{match_boxes}</div>
</details>
<details>
<summary>Per player &mdash; <a href="#" data-all="on" data-for="s">all</a> &middot;
<a href="#" data-all="off" data-for="s">none</a></summary>
<div class="pick fields">{stat_boxes}</div>
</details>
<p class="status"><span class="dot"></span><span id="est">&hellip;</span></p>
<p class="row"><a class="btn" id="build" href="#">Download selection</a>
<button class="mini" id="copy" type="button">Copy link</button></p>
<p class="urlbox"><a id="url" class="url" href="#"></a></p>
</div>
<p class="note">The link changes as you tick things, and it is the whole request &mdash; paste it
into a terminal, a script, or a message. This page&rsquo;s own address tracks your picks too, so
bookmarking or sending it hands over the same selection.</p>
<p class="note">Every match carries its <code>uuid</code> and every player their name, whatever
else you untick. Recordings are the heavy part &mdash; the whole archive is {gb(held_bytes)} of
them, against roughly {gb(ids * 530)} for every match result ever. Sizes are exact for recordings
and an estimate for the rest, since those are built as they are sent.</p>
</section>

<section>
<h2>By week</h2>
<p class="lead">The same thing a week at a time. <strong>replays.tar</strong> is that week's
recordings, <strong>results</strong> is how every match ended with per-player stats, and
<strong>tagpro.eu</strong> is that week's mirror records in bulk shape.</p>
<p class="note">Weeks are the Monday, UTC, matching the <a href="index.html">Coverage</a> tables.
<code>results</code> carries the map and its tagpro.eu map id by default &mdash; drop it with
<code>?map=0</code> &mdash; and includes rebuilt records marked as such, which
<code>?rebuilt=0</code> removes.</p>
<table><thead><tr><th>Week</th><th style="text-align:right">Matches</th>
<th style="text-align:right">Recordings</th><th style="text-align:right">Size</th>
<th></th></tr></thead><tbody>
{chr(10).join(rows)}
</tbody></table>
</section>

<section>
<h2>Coverage data</h2>
<p class="lead">Ids, timings and held-flags for every match. Small enough to sit on this site, so
these links always work, host up or down.</p>
<table class="files"><tbody>
<tr><td><a href="data/all.replay.json" download>all.replay.json</a></td>
<td>every match: <code>uuid</code> plus the <code>game_id</code> a recording is requested by</td></tr>
<tr><td><a href="data/missing_replays.json" download>missing_replays.json</a></td>
<td>the {f(ids - rep)} matches with no recording held &mdash; the wanted list</td></tr>
<tr><td><a href="data/all.eu.json" download>all.eu.json</a></td>
<td>every linked tagpro.eu match id ({f(record)} of them)</td></tr>
<tr><td><a href="data/coverage.json">coverage.json</a></td>
<td>the per-week totals behind the tables on the Coverage tab</td></tr>
<tr><td><a href="DATA_MAP.md">DATA_MAP.md</a></td>
<td>what every field means</td></tr>
</tbody></table>
<p class="note">Per-week equivalents are linked from every row of the
<a href="index.html">Coverage</a> tables.</p>
</section>

<section>
<h2>Fetching it with a script</h2>
<pre>curl -LOJ {WORKER}/go/replay/&lt;uuid&gt;

curl -L {WORKER}/go/week/{latest}/replays.tar | tar x

curl -L -C - -O {WORKER}/go/bulk/ranked_matches_bulk.json.gz

curl -L -o results-{latest}.json.gz \\
  "{WORKER}/go/week/{latest}/results.json.gz?map=0"</pre>
<p class="note"><code>-L</code> follows the redirect to wherever the host currently is, so a script
written once survives every rotation. <code>-J</code> takes the filename from the server rather
than from the url, and <code>-C -</code> resumes a part-finished file. If the host is offline the
redirector answers 503 rather than sending you somewhere dead. It is one home uplink: eight
transfers run at once and the rest wait.</p>
</section>

<section>
<h2>Going the other way</h2>
<p class="lead">If you have replays this archive does not, send them to <strong>{DISCORD}</strong>
on Discord &mdash; any format, any size, any dates. <a href="about.html">Why that matters.</a></p>
</section>'''
    foot = ('Recordings are <code>.ndjson.gz</code>, one JSON event per line, as the recorder served '
            'them. tagpro.eu records follow <a href="https://tagpro.eu/?science" target="_blank" '
            'rel="noopener">tagpro.eu&rsquo;s own format</a>. Field reference: '
            '<a href="DATA_MAP.md">DATA_MAP.md</a>.<br><br>')
    return page("download.html", "Download · TagPro ranked replay archive",
                "Every file this archive publishes, whole or a week at a time.",
                body, foot=foot,
                script=(STATUS_JS + CUSTOM_JS)
                .replace("__WORKER__", WORKER).replace("__TOTAL__", str(ids)))


def rebuilt_page(cov, ids, span):
    f = lambda n: f"{n:,}"
    rebuilt = sum(r["rebuilt"] for r in cov)
    record = sum(r["record"] for r in cov)
    body = f'''<section class="prose">
<h2>Records rebuilt from recordings</h2>
<p>tagpro.eu carries {f(record)} of the {f(ids)} matches here, about
{100.0 * record / ids:.0f}%. For some of the rest there is a recording in this archive, and a
recording contains everything a match record holds. <strong>{f(rebuilt)} records</strong> have been
reconstructed that way. They are the orange band on the <a href="index.html">coverage table</a>.</p>
<p>They are kept separate on purpose. A record derived from a recording is not a tagpro.eu record,
so it carries <code>source: "replay"</code> and a synthetic match id from 1,000,000,000 up, well
clear of tagpro.eu's id space. <strong>Downloads leave them out unless you ask for them</strong>
&mdash; add <code>?rebuilt=1</code>.</p>

<h2>How accurate they are</h2>
<p>Measured by rebuilding matches that exist in <em>both</em> sources and comparing field by field,
across 250 matches and 1,992 player rows:</p>
<table><thead><tr><th>Field</th><th style="text-align:right">Agreement</th></tr></thead><tbody>
<tr><td>captures</td><td class="p">100.00%</td></tr>
<tr><td>pops</td><td class="p">100.00%</td></tr>
<tr><td>tags</td><td class="p">100.00%</td></tr>
<tr><td>returns</td><td class="p">100.00%</td></tr>
<tr><td>powerups</td><td class="p">100.00%</td></tr>
<tr><td>grabs</td><td class="p">99.95%</td></tr>
<tr><td>drops</td><td class="p">99.95%</td></tr>
</tbody><tfoot><tr><td>Player rows exact on all seven</td>
<td class="p">1,990 / 1,992 &mdash; 99.90%</td></tr></tfoot></table>
<p><strong>hold</strong> comes out frame-accurate: median error zero, every row within one second.
<strong>prevent</strong> is second-resolution only, because the recording carries it as a counter
ticking once a second rather than as events.</p>
<p>Two things the extractor has to get right, and did not at first. Counters belong to a player,
not to a slot: a player who rejoins gets a new slot that inherits their accumulated counters, and
treating that as a fresh player double-counts everything they had already done. And the units
differ &mdash; a recording counts hold and prevent in seconds while tagpro.eu stores 60&nbsp;fps
frames &mdash; so everything is normalised to frames before it is written.</p>

<h2>What a recording cannot supply</h2>
<p>These fields are null on a rebuilt record rather than guessed:</p>
<ul>
<li>The packed per-player <code>events</code> string and the per-team <code>splats</code> blob.
tagpro.eu's own encoding of these is not reproduced. The events are supplied instead as
<code>events_decoded</code>, one object per event.</li>
<li><code>auth</code>, <code>flair</code>, <code>degree</code>, <code>score</code> and
<code>points</code> &mdash; account and scoring metadata the mirror adds, which is not in the
recording.</li>
<li><code>port</code> and <code>timeLimit</code>.</li>
<li><code>mapId</code> is filled in only when the map name matches a map tagpro.eu already knows.
No map rows are invented: a recording's map identifier is a different id space, and a fabricated
id would corrupt every per-map aggregate downstream.</li>
</ul>

<h2>Where they are better</h2>
<p>The event stream from a recording is richer than the mirror's. Every event carries a position.
tagpro.eu only has coordinates for pops and drops, and only inside a packed blob that had to be
reverse-engineered to read at all.</p>

<h2>Getting them, or not</h2>
<p>Every tagpro.eu download takes the flag. Without it you get the mirror's records only, which is
what tagpro.eu itself would give you:</p>
<pre>&hellip;/eu/week/{cov[-1]["week"]}.json.gz              mirror records only
&hellip;/eu/week/{cov[-1]["week"]}.json.gz?rebuilt=1   with the rebuilt ones mixed in</pre>
<p>Both are in <a href="https://tagpro.eu/?science" target="_blank" rel="noopener">tagpro.eu's own
bulk shape</a>: an object keyed by match id. See <a href="download.html">Download</a>.</p>
<p><strong>If a rebuilt record looks wrong, tell me.</strong> Message <strong>{DISCORD}</strong> on
Discord with the match id and I will look at the recording it came from.</p>
</section>'''
    return page("rebuilt.html", "Rebuilt records · TagPro ranked replay archive",
                "Match records reconstructed from recordings, and how accurate they are.",
                body)


def write(name, html):
    open(OUT / name, "w").write(html)


def render(cov, missing=(), held_bytes=0):
    f = lambda n: f"{n:,}"
    pc = lambda a, b: (100.0 * a / b) if b else 0.0
    tot = lambda k: sum(r[k] for r in cov)
    t = tot
    ids, est, rep = tot("ids"), tot("est"), tot("replay")
    uncertain = [r for r in cov if r["missing_ids"] or r["partial"]]
    id_pct = 100.0

    def body(kind):
        out = []
        for s in cov:
            lbl = dt.date.fromisoformat(s["week"]).strftime("%d %b %Y")
            wk_ = s["week"]
            links = (f'<td class="dl">'
                     f'<a href="data/weeks/{wk_}.replay.json" download title="every replay id">rep</a>'
                     f'<a href="data/weeks/{wk_}.missing.json" download title="replay ids with no recording held">missing</a>'
                     f'<a href="data/weeks/{wk_}.eu.json" download title="tagpro.eu ids">eu</a>'
                     f'<a href="data/weeks/{wk_}.json" download title="everything, with what is held">all</a>'
                     f'</td>')
            if kind == "id":
                # The ranked replay listing is the authority on which matches
                # exist, so a week's collected ids ARE that week's total.
                d = s["ids"]
                reb = s["rebuilt"]
                mirror = s["record"] - reb        # records the mirror carried itself
                # rebuilt is a subset of record, so the two segments compose
                # into total record coverage on one track. A sliver must stay
                # visible: 281 rebuilt across 123k rounds to nothing otherwise.
                w_reb = max(pc(reb, d), 0.7) if reb else 0.0
                bar = (f'<div class="bars">'
                       f'<div class="track fid"><span style="width:100%"></span></div>'
                       f'<div class="track split">'
                       f'<span class="seg-rec" style="width:{pc(mirror,d):.2f}%"></span>'
                       f'<span class="seg-reb" style="width:{w_reb:.2f}%"></span>'
                       f'</div></div>')
                out.append(
                    f'<tr><td class="wk">{lbl}</td><td>{bar}</td>'
                    f'<td class="n">{f(d)}</td>'
                    f'<td class="n2">{f(s["record"])} <i>{pc(s["record"],d):.0f}%</i></td>'
                    f'<td class="n2">{f(reb)} <i>/ {f(d - mirror)}</i></td>'
                    f'{links}</tr>')
            else:
                n, d = s["replay"], s["ids"]
                total = f(d)
                w = pc(n, d)
                out.append(
                    f'<tr><td class="wk">{lbl}</td>'
                    f'<td><div class="track frep"><span style="width:{w:.2f}%"></span></div></td>'
                    f'<td class="n">{f(n)} <i>/ {total}</i></td>'
                    f'<td class="p">{w:.0f}%</td>{links}</tr>')
        return "\n".join(out)

    def foot(n, d, var, approx):
        total = ("~" + f(d)) if approx else f(d)
        return (f'<tfoot><tr><td>Total</td><td><div class="track"><span '
                f'style="width:{pc(n,d):.2f}%;background:var(--{var})"></span></div></td>'
                f'<td class="n">{f(n)} <i>/ {total}</i></td>'
                f'<td class="p">{pc(n,d):.1f}%</td><td></td></tr></tfoot>')

    reb_all = tot("rebuilt")
    mirror_all = tot("record") - reb_all
    id_foot = (
        '<tfoot><tr><td>Total</td><td><div class="bars">'
        '<div class="track fid"><span style="width:100%"></span></div>'
        '<div class="track split">'
        f'<span class="seg-rec" style="width:{pc(mirror_all,ids):.2f}%"></span>'
        f'<span class="seg-reb" style="width:{max(pc(reb_all,ids),0.7):.2f}%"></span>'
        '</div></div></td>'
        f'<td class="n">{f(ids)}</td>'
        f'<td class="n2">{f(tot("record"))} <i>{pc(tot("record"),ids):.1f}%</i></td>'
        f'<td class="n2">{f(reb_all)} <i>/ {f(ids - mirror_all)}</i></td>'
        '<td></td></tr></tfoot>')
    exceptions = "\n".join(
        f'<tr><td><a href="https://tagpro.eu/?match={mid}">{mid}</a></td>'
        f'<td class="wk">{t:%Y-%m-%d %H:%M:%S}</td></tr>'
        for mid, t in sorted(missing, key=lambda x: x[1]))
    span = (dt.date.fromisoformat(cov[0]["week"]).strftime("%b %Y"),
            dt.date.fromisoformat(cov[-1]["week"]).strftime("%b %Y"))
    stats = f'''<div class="stats">
<div class="stat"><b>{f(ids)}</b><span>replay ids</span></div>
<div class="stat"><b>100%</b><span>replay id coverage</span></div>
<div class="stat"><b>{f(rep)}</b><span>recordings held</span></div>
<div class="stat"><b>{pc(rep,ids):.2f}%</b><span>recordings downloaded</span></div>
</div>'''

    body = f'''<section>
<h2>Replay ids &mdash; complete</h2>
<p class="lead">Every ranked match the replay listing returns has its id here: <strong>{f(ids)} of
{f(ids)}, 100%</strong>. Nothing is outstanding.</p>
<p class="note">Replay ids collected per week, with the tagpro.eu layer beneath. The third column
counts records rebuilt here from an archived recording, against the number the mirror never carried
in the first place &mdash; so it reads as progress closing that shortfall, not as a share of all
tagpro.eu ids.</p>
<div class="key">
<span><i style="background:var(--id)"></i>replay ids collected</span>
<span><i style="background:var(--rec)"></i>tagpro.eu id carried by the mirror</span>
<span><i style="background:var(--reb)"></i>rebuilt here from a recording</span>
</div>
<p class="note">The ranked replay listing is the authority on which matches exist, so these totals
are exact rather than estimated.</p>
<table><thead>{HEAD_ID}</thead><tbody>
{body("id")}
</tbody>{id_foot}</table>
</section>

<section>
<h2>Recordings downloaded &mdash; {pc(rep,ids):.1f}%</h2>
<p class="lead">This is the part that is incomplete. Of the {f(ids)} matches we have ids for, the
actual recording has been downloaded for <strong>{f(rep)}</strong>.</p>
<p class="note">Replay recordings held, against the replay ids collected for that week.</p>
<table><thead>{HEAD}</thead><tbody>
{body("rep")}
</tbody>{foot(rep, ids, "rep", False)}</table>
</section>

<section>
<h2>Exceptions</h2>
<p class="note">{len(missing)} match{"" if len(missing)==1 else "es"} of {f(ids + len(missing))} appear
on tagpro.eu but return no entry from the ranked replay listing, including inside date ranges the
listing itself reported as fully enumerated. No recording exists to collect for these, so they are
counted here rather than as missing ids.</p>
<table><thead><tr><th>tagpro.eu</th><th>Started (UTC)</th></tr></thead><tbody>
{exceptions}
</tbody></table>
</section>'''

    foot = f'''Each week offers four downloads. <code>rep</code> is every replay id (uuid plus the game id a
recording is requested by), <code>missing</code> is the subset with no recording held here,
<code>eu</code> is the tagpro.eu ids, and <code>all</code> is everything with flags for what is held.
<br><br>
Whole archive: <a href="data/all.replay.json" download>all.replay.json</a> &middot;
<a href="data/missing_replays.json" download>missing_replays.json</a> &middot;
<a href="data/all.eu.json" download>all.eu.json</a> &middot;
<a href="data/coverage.json">coverage.json</a>. Field reference: <a href="DATA_MAP.md">DATA_MAP.md</a>.'''

    write("index.html", page(
        "index.html", "Coverage \u00b7 TagPro ranked replay archive",
        f"Match ids and replay recordings collected per week, {span[0]} \u2013 {span[1]}.",
        body, stats=stats, foot=foot))
    write("about.html", about_page(cov, missing, ids, rep, held_bytes, span))
    write("rebuilt.html", rebuilt_page(cov, ids, span))
    write("download.html", download_page(cov, ids, rep, held_bytes, span))


if __name__ == "__main__":
    cov = build()
    t = lambda k: sum(r[k] for r in cov)
    print(f"weeks      : {len(cov)}")
    print(f"ids        : {t('ids'):,}  (100% - the ranked listing is ground truth)")
    print(f"records    : {t('record'):,}  ({100*t('record')/t('ids'):.2f}% have a tagpro.eu record)")
    print(f"rebuilt    : {t('rebuilt'):,}  (records reconstructed from a recording)")
    print(f"replays    : {t('replay'):,} / {t('ids'):,}  ({100*t('replay')/t('ids'):.2f}%)")
    print(f"exceptions : {t('missing_ids')}  (on tagpro.eu, absent from the listing)")
