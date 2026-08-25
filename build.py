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
            missing.append(t)
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
    for t in missing:
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
        json.dump(recs, open(wdir / f"{w}.json", "w"), separators=(",", ":"))

    json.dump(cov, open(OUT / "data" / "coverage.json", "w"), indent=1)
    json.dump([{"uuid": r[0], "game_id": r[1], "started": r[2], "map": r[3]}
               for r in rows if not r[5]],
              open(OUT / "data" / "missing_replays.json", "w"), separators=(",", ":"))

    render(cov)
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
h2{font-size:15px;font-weight:620;margin:0 0 3px}
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
td.dl{width:44px;text-align:right}
.track{height:9px;border-radius:3px;background:var(--track);overflow:hidden;min-width:90px}
.track span{display:block;height:100%}
.fid span{background:var(--id)}
.frep span{background:var(--rep)}
.bars{display:flex;flex-direction:column;gap:2px;min-width:90px}
.bars .track{height:5px;border-radius:2px}
.bars .track:first-child{height:9px;border-radius:3px}
.frec span{background:var(--rec)}
.freb span{background:var(--reb)}
.key{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:0 0 12px}
.key i{display:inline-block;width:18px;height:7px;border-radius:2px;vertical-align:1px;margin-right:6px}
tfoot td{font-weight:640;background:var(--panel);border-top:2px solid var(--rule);padding:10px 12px}
a{color:var(--id)} .dl a{font-size:11px;color:var(--faint);text-decoration:none}
.dl a:hover{color:var(--id)}
footer{padding:26px 0 60px;color:var(--faint);font-size:12.5px;border-top:1px solid var(--rule);margin-top:34px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em;
font-family:ui-monospace,Menlo,Consolas,monospace}
@media(max-width:640px){td.n{width:118px}td.wk{width:88px;font-size:12px}.track{min-width:50px}}
"""

HEAD = ('<tr><th>Week</th><th></th><th style="text-align:right">Count</th>'
        '<th style="text-align:right">%</th><th></th></tr>')


def render(cov):
    f = lambda n: f"{n:,}"
    pc = lambda a, b: (100.0 * a / b) if b else 0.0
    tot = lambda k: sum(r[k] for r in cov)
    t = tot
    ids, est, rep = tot("ids"), tot("est"), tot("replay")
    uncertain = [r for r in cov if r["missing_ids"] or r["partial"]]

    def body(kind):
        out = []
        for s in cov:
            if kind == "id":
                n, d, cls = s["ids"], s["est"], "fid"
                approx = s["missing_ids"] > 0 or s["partial"]
            else:
                n, d, cls = s["replay"], s["ids"], "frep"
                approx = s["partial"]
            total = ("~" + f(d)) if approx else f(d)
            w = pc(n, d)
            lbl = dt.date.fromisoformat(s["week"]).strftime("%d %b %Y")
            if kind == "id":
                # A sliver must stay visible: 281 reconstructed matches across
                # 123k is 0.2%, which rounds to nothing without a floor.
                vis = lambda v: max(pc(v, d), 0.7) if v else 0.0
                bar = (f'<div class="bars">'
                       f'<div class="track fid"><span style="width:{w:.2f}%"></span></div>'
                       f'<div class="track frec"><span style="width:{pc(s["record"],d):.2f}%"></span></div>'
                       f'<div class="track freb"><span style="width:{vis(s["rebuilt"]):.2f}%"></span></div>'
                       f'</div>')
            else:
                bar = f'<div class="track {cls}"><span style="width:{w:.2f}%"></span></div>'
            out.append(
                f'<tr><td class="wk">{lbl}</td>'
                f'<td>{bar}</td>'
                f'<td class="n">{f(n)} <i>/ {total}</i></td>'
                f'<td class="p">{w:.0f}%</td>'
                f'<td class="dl"><a href="data/weeks/{s["week"]}.json" download>json</a></td></tr>')
        return "\n".join(out)

    def foot(n, d, var, approx):
        total = ("~" + f(d)) if approx else f(d)
        return (f'<tfoot><tr><td>Total</td><td><div class="track"><span '
                f'style="width:{pc(n,d):.2f}%;background:var(--{var})"></span></div></td>'
                f'<td class="n">{f(n)} <i>/ {total}</i></td>'
                f'<td class="p">{pc(n,d):.1f}%</td><td></td></tr></tfoot>')

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
<div class="stat"><b>{f(ids)}</b><span>match ids</span></div>
<div class="stat"><b>{pc(ids,est):.1f}%</b><span>id coverage</span></div>
<div class="stat"><b>{f(rep)}</b><span>replays</span></div>
<div class="stat"><b>{pc(rep,ids):.2f}%</b><span>replay coverage</span></div>
</div>
</div></header>

<div class="wrap">
<section>
<h2>ID coverage</h2>
<p class="note">Match ids collected per week, with the two derived layers beneath: how many of
those matches have a tagpro.eu record, and how many of those records were rebuilt here from an
archived recording rather than carried by the mirror.</p>
<div class="key">
<span><i style="background:var(--id)"></i>match ids collected</span>
<span><i style="background:var(--rec)"></i>has a tagpro.eu record</span>
<span><i style="background:var(--reb)"></i>record rebuilt from a recording</span>
</div>
<p class="note">{len(cov)-len(uncertain)} of {len(cov)} weeks are exact. A <code>~</code> marks the
{len(uncertain)} weeks where the total is an estimate rather than a known figure &mdash; see the note
below the totals.</p>
<table><thead>{HEAD}</thead><tbody>
{body("id")}
</tbody>{foot(ids, est, "id", bool(uncertain))}</table>
</section>

<section>
<h2>Replay coverage</h2>
<p class="note">Replay recordings held, against the match ids collected for that week.</p>
<table><thead>{HEAD}</thead><tbody>
{body("rep")}
</tbody>{foot(rep, ids, "rep", False)}</table>
</section>

<footer><div class="wrap">
The ID totals are marked <code>~</code> because no published figure exists for how many ranked
matches were played in a week. The denominator is estimated as the ids held plus any tagpro.eu match
with no id at the same instant. At {pc(ids,est):.2f}% coverage the estimate and the real total are
all but identical, and the {t('missing_ids')} ids still counted as absent are most likely matches whose
start times disagree between the two sources rather than genuinely missing records.
<br><br>
Per-week <code>json</code> links give every match id for that week with flags for what is held.
Full inventories: <a href="data/missing_replays.json" download>missing_replays.json</a>,
<a href="data/coverage.json">coverage.json</a>. Field reference: <a href="DATA_MAP.md">DATA_MAP.md</a>.
Generated {dt.datetime.now(dt.timezone.utc):%d %b %Y}.
</div></footer>
</div></body></html>'''
    open(OUT / "index.html", "w").write(html)


if __name__ == "__main__":
    cov = build()
    t = lambda k: sum(r[k] for r in cov)
    bad = [r for r in cov if r["missing_ids"]]
    print(f"weeks           : {len(cov)}")
    print(f"ids             : {t('ids'):,} / ~{t('est'):,}  ({100*t('ids')/t('est'):.2f}%)")
    print(f"replays         : {t('replay'):,} / {t('ids'):,}  ({100*t('replay')/t('ids'):.2f}%)")
    print(f"weeks with gaps : {len(bad)}  (missing {t('missing_ids'):,} ids)")
    for r in sorted(bad, key=lambda x: -x["missing_ids"])[:10]:
        print(f"   {r['week']}  {r['ids']:>5} / ~{r['est']:<5}  missing {r['missing_ids']}")
