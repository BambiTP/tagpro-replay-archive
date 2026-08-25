# Field reference

Two ids identify a match. `uuid` identifies the replay; `game_id` addresses the recording itself
and is what a recording is requested by. `eu_match_id` is the same match on tagpro.eu, where one
exists (about 98% of them).

## Per week

Files are keyed by the Monday of the week, UTC.

| File | Contents |
| --- | --- |
| `data/weeks/YYYY-MM-DD.eu.json` | tagpro.eu match ids — a plain list of integers |
| `data/weeks/YYYY-MM-DD.replay.json` | replay ids — objects with `uuid` and `game_id` |
| `data/weeks/YYYY-MM-DD.json` | both, plus what is actually held |

Fields in the combined file:

| Field | Type | Meaning |
| --- | --- | --- |
| `uuid` | string | the replay's identifier |
| `game_id` | string | the id a recording is requested by |
| `started` | string | match start, ISO-8601 UTC |
| `map` | string | map name |
| `duration` | int | match length in frames (60 = 1 second) |
| `have_replay` | bool | the recording is held here |
| `have_record` | bool | a tagpro.eu record is linked |
| `eu_match_id` | int / null | tagpro.eu match id where linked |

## Whole archive

| File | Contents |
| --- | --- |
| `data/all.eu.json` | every tagpro.eu match id |
| `data/all.replay.json` | every `uuid` + `game_id` |
| `data/missing_replays.json` | matches whose recording is not held — `uuid`, `game_id`, `started`, `map` |
| `data/coverage.json` | per-week totals behind the tables |

## coverage.json

| Field | Meaning |
| --- | --- |
| `week` | Monday of the week, ISO date |
| `ids` | match ids collected |
| `replay` | recordings held |
| `record` | ids that have a tagpro.eu record |
| `rebuilt` | records reconstructed here from a recording rather than carried by the mirror |
| `missing_ids` | ids tagpro.eu lists that are absent here |
| `est` | estimated ranked matches that week |
| `partial` | week still in progress, totals not final |

`est` is an estimate, not a published figure: no count exists for how many ranked matches were
played in a week, so it is derived as ids held plus tagpro.eu matches with no id at the same
instant. Counts shown against it carry a `~`. Matching allows 120 seconds of disagreement between
the two sources' start times; a handful of matches differ by more than that and are counted absent
despite probably being present.
