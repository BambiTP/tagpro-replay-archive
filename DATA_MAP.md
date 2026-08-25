# Field reference

## `data/weeks/YYYY-MM-DD.json`

One file per week, keyed by the Monday of that week. Array of objects, one per known ranked match.

| Field | Type | Meaning |
| --- | --- | --- |
| `uuid` | string | the replay's identifier |
| `game_id` | string | the id used for the recording itself |
| `started` | string | match start, ISO-8601 UTC |
| `map` | string | map name |
| `duration` | int | match length in frames (60 = 1 second) |
| `have_replay` | bool | the recording is held |
| `have_record` | bool | a tagpro.eu match record is linked |
| `eu_match_id` | int / null | tagpro.eu match id where linked |

## `data/missing_replays.json`

Every known ranked match whose recording is not held. Fields: `uuid`, `game_id`, `started`, `map`.

## `data/coverage.json`

Per-week totals used to build the tables.

| Field | Meaning |
| --- | --- |
| `week` | Monday of the week, ISO date |
| `ids` | match ids collected |
| `replay` | recordings held |
| `est` | estimated ranked matches that week |
| `partial` | week still in progress, totals not final |

`est` combines ids found by the ranked scan with tagpro.eu matches carrying no linked ranked id,
so it is an estimate rather than a known total. Counts shown against it are marked `~`.
