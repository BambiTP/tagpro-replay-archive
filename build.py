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
               CASE WHEN m.match_id IS NOT NULL THEN 1 ELSE 0 END, m.match_id, m.source
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
    for uuid, gid, started, mp, dur, has_rep, has_rec, eu_id, eu_src in rows:
        t = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        w = monday(t.date())
        c = weeks.setdefault(w, {"ids": 0, "replay": 0, "missing": 0,
                                 "record": 0, "rebuilt": 0})
        c["ids"] += 1
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
                          "record": 0, "rebuilt": 0})["missing"] += 1

    today = dt.datetime.now(dt.timezone.utc).date()
    curwk = monday(today)
    cov = []
    for w in sorted(weeks):
        c = weeks[w]
        cov.append({"week": w, "ids": c["ids"], "replay": c["replay"],
                    "record": c["record"], "rebuilt": c["rebuilt"],
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
    dump("all.json", [{"uuid": r[0], "game_id": r[1], "started": r[2], "map": r[3],
                       "duration": r[4], "have_replay": bool(r[5]),
                       "have_record": bool(r[6]), "eu_match_id": r[7]} for r in rows])

    render(cov, missing)
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
td.n2{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;width:96px}
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
"""

HEAD = ('<tr><th>Week</th><th></th><th style="text-align:right">Count</th>'
        '<th style="text-align:right">%</th><th></th></tr>')

HEAD_ID = ('<tr><th>Week</th><th></th><th style="text-align:right">Replay ids</th>'
           '<th style="text-align:right">tagpro.eu ids</th>'
           '<th style="text-align:right">tagpro.eu rebuilt</th><th></th></tr>')


def render(cov, missing=()):
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
                    f'<td class="n2">{f(reb) if reb else "&mdash;"}</td>'
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
        f'<td class="n2">{f(reb_all)}</td><td></td></tr></tfoot>')
    exceptions = "\n".join(
        f'<tr><td><a href="https://tagpro.eu/?match={mid}">{mid}</a></td>'
        f'<td class="wk">{t:%Y-%m-%d %H:%M:%S}</td></tr>'
        for mid, t in sorted(missing, key=lambda x: x[1]))
    span = (dt.date.fromisoformat(cov[0]["week"]).strftime("%b %Y"),
            dt.date.fromisoformat(cov[-1]["week"]).strftime("%b %Y"))
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ranked Replay Coverage</title>
<meta name="description" content="Per-week coverage of collected TagPro ranked match ids and replay recordings.">
<style>{CSS}</style></head><body>
<header><div class="wrap">
<h1>Ranked replay coverage</h1>
<p class="sub">Match ids and replay recordings collected per week, {span[0]} &ndash; {span[1]}.</p>
<div class="stats">
<div class="stat"><b>{f(ids)}</b><span>replay ids</span></div>
<div class="stat"><b>100%</b><span>replay id coverage</span></div>
<div class="stat"><b>{f(rep)}</b><span>recordings held</span></div>
<div class="stat"><b>{pc(rep,ids):.2f}%</b><span>recordings downloaded</span></div>
</div>
</div></header>

<div class="wrap">
<section>
<h2>Replay ids &mdash; complete</h2>
<p class="lead">Every ranked match the replay listing returns has its id here: <strong>{f(ids)} of
{f(ids)}, 100%</strong>. Nothing is outstanding.</p>
<p class="note">Replay ids collected per week, with the tagpro.eu layer beneath: how many of those
matches have a tagpro.eu id, and how many of those were rebuilt here from an archived recording
rather than carried by the mirror.</p>
<div class="key">
<span><i style="background:var(--id)"></i>replay ids collected</span>
<span><i style="background:var(--rec)"></i>tagpro.eu id carried by the mirror</span>
<span><i style="background:var(--reb)"></i>tagpro.eu id rebuilt here from a recording</span>
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
</section>

<footer><div class="wrap">
Each week offers four downloads. <code>rep</code> is every replay id (uuid plus the game id a
recording is requested by), <code>missing</code> is the subset with no recording held here,
<code>eu</code> is the tagpro.eu ids, and <code>all</code> is everything with flags for what is held.
<br><br>
Whole archive: <a href="data/all.replay.json" download>all.replay.json</a> &middot;
<a href="data/missing_replays.json" download>missing_replays.json</a> &middot;
<a href="data/all.eu.json" download>all.eu.json</a> &middot;
<a href="data/all.json" download>all.json</a> &middot;
<a href="data/coverage.json">coverage.json</a>. Field reference: <a href="DATA_MAP.md">DATA_MAP.md</a>.
Generated {dt.datetime.now(dt.timezone.utc):%d %b %Y}.
</div></footer>
</div></body></html>'''
    open(OUT / "index.html", "w").write(html)


if __name__ == "__main__":
    cov = build()
    t = lambda k: sum(r[k] for r in cov)
    print(f"weeks      : {len(cov)}")
    print(f"ids        : {t('ids'):,}  (100% - the ranked listing is ground truth)")
    print(f"records    : {t('record'):,}  ({100*t('record')/t('ids'):.2f}% have a tagpro.eu record)")
    print(f"rebuilt    : {t('rebuilt'):,}  (records reconstructed from a recording)")
    print(f"replays    : {t('replay'):,} / {t('ids'):,}  ({100*t('replay')/t('ids'):.2f}%)")
    print(f"exceptions : {t('missing_ids')}  (on tagpro.eu, absent from the listing)")
