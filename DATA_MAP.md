# Where the data lives

Four different things get called "replay data" and they are not interchangeable. Only the
last one is scarce.

| Layer | What it is | Where it comes from | Coverage |
| --- | --- | --- | --- |
| Match list | which ranked matches exist — ids, dates, maps, scores | the official ranked servers' replay listing | complete for the whole ranked era |
| Match records | players, scores, event timelines, per-match stats | tagpro.eu | 98.1% of known matches |
| Ranked metadata | skill ratings and win probabilities for a ranked game | embedded in the recording's first line | only where the recording was captured |
| Replay recordings | the full frame-by-frame game file everything else derives from | the official servers, for ~48 hours after a match | 6.2% |

The first two are broadly available and always have been. The recordings expire: reachable for
roughly two days after a match, then effectively gone unless somebody saved a copy.

## Published here

| File | Contents |
| --- | --- |
| `data/weeks/YYYY-MM-DD.csv.gz` | one file per week — every known match that week, with flags for whether the recording and the match record are held |
| `data/weeks/index.json` | per-week totals used to build the coverage page |
| `data/missing_replays.csv.gz` | every known ranked match whose recording is **not** held — 112,765 rows |

Columns in the weekly files: `uuid`, `game_id`, `started_utc`, `map`, `duration_frames`,
`have_replay`, `have_metadata`, `tagpro_eu_match_id`, `donated_pending_import`.

`uuid` is the replay's own identifier. `game_id` is the id the servers use for the recording
itself. `tagpro_eu_match_id` links to the public match record where one exists.

## Upstream sources

**tagpro.eu** — the long-standing public mirror of match records. Has a per-match data endpoint
and sitemaps listing every match id. This is where the 98% match-record coverage comes from, and
it is public, bulk-friendly, and has been for years.

**The official ranked servers** — the only source of the recordings themselves, and of the ranked
skill and win-probability metadata attached to them. The replay listing endpoint and its filters
are documented on the coverage page. Recordings older than roughly two days return
`403 you must be logged in to view that replay` to anonymous requests.

## What is not published

The analysis built on top of this data — models, ratings, derived research tables — is not part
of this repository. This repo is the archive inventory and its coverage, nothing else.

## Provenance

This archive began by crawling the ranked servers without asking. The operators noticed, it cost
them bandwidth and time, and they asked for it to stop. It stopped. They allowed the archive to
keep what had already been collected, and permitted continued collection of recent matches inside
a narrow window. That permission was extended to one person and is not transferable.
