# Schema de-duplication & query-cleanup plan

Status: DRAFT — for review before implementation.

## Goal

1. **Minimal ClickHouse schema** — remove duplicated columns/data across tables.
2. **Clean ClickHouse queries** — remove the layered perf-optimization complexity in
   `data_provider.py` where it obscures intent, *without* regressing correctness.

## Ground truth (measured against live DB)

| Table | Rows | Notes |
|---|---|---|
| match_registry | 83,712 | distinct match_id 71,588 |
| matches | 54,498 | distinct match_id 54,498 |
| match_maps | 25,528 | |
| tournaments | 1,986 | |
| players | 5,449 | |
| maps | 243 | |
| rating_history | 259,248 | MergeTree (append) |
| player_ratings | 26,531 | |

### Confirmed duplication (all in the original design, not from perf commits)

1. **`match_registry.played_at` ↔ `matches.played_at`** — 54,498 matches appear in both.
   `match_registry` is a pipeline-workflow table (discovery/download state + raw HTML);
   `matches` is the parsed result. `played_at` need not be duplicated — the registry
   can keep only what the workflow needs.
2. **`matches.status` is dead** — 0 rows differ from `'Match finished'`. Column can go.
3. **`match_maps.player1_name/player2_name` ↔ `matches`** — 2/25,528 rows mismatch
   (reparse artifacts). These names can be dropped; join to `matches` instead.
4. **`player_ratings.player_name` ↔ `players.name`** — denormalized copy. Used by
   name→id resolution and autocomplete; can be derived via `players`.
5. **`matches.player1_name/country, player2_name/country, tournament_name`** — copies of
   `players.name/country` and `tournaments.name`. Heavy de-normalization (69 refs).
6. **`tournaments` vs `tournament_details`** — a stale, empty leftover table
   (0 rows). Drop it.

## Decision framing

De-normalization in ClickHouse is a deliberate, often-correct trade: it avoids JOINs in
hot read paths. The question is whether these copies earn their keep. My read:

- **Drop with no risk:** `matches.status` (dead), `tournament_details` table (empty).
- **Drop, replace with join:** `match_maps.player1_name/player2_name` (2 stale rows
  prove it's write-inconsistent; join is cheap, maps are small).
- **Drop, replace with join:** `player_ratings.player_name` (players table is the source
  of truth; name→id resolution and autocomplete go through `players`).
- **Keep (hot denorm):** `matches.player*_name/country` and `matches.tournament_name`.
  These feed the most-queried read paths (47 matches-queries); removing them would force
  JOINs on every match row across the whole site for little storage gain (names are
  small strings, already LowCardinality where it matters). **Recommend keeping for now** —
  flag if you want them gone too.
- **Merge `match_registry` into `matches`?** No — they serve different lifecycles.
  `match_registry` tracks discovery→download→parse with raw HTML; `matches` is clean
  parsed output. Merging would re-introduce pipeline state into the read table and force
  raw_html (large strings) into the hot path. **Recommend keeping separate** but trimming
  `match_registry.played_at` duplication is optional (it's cheap and used for ordering).

## Proposed schema changes

1. Drop `tournament_details` table entirely.
2. Drop `matches.status` column.
3. Drop `match_maps.player1_name`, `match_maps.player2_name` (join to `matches`).
4. Drop `player_ratings.player_name` (join to `players`).
5. (Optional, recommend keep) `match_registry.played_at`, `matches.tournament_name`,
   `matches.player*_name/country`.

## Query-cleanup targets (data_provider.py, 3504 lines)

The perf commits worth *reverting/simplifying* because they added Python-side state or
complexity with little gain at current data size:

- **Module-level caches** (`dacc832`, `cf6bc16`, `1b4a6e4`, `a1272d2`, `70a1551`,
  `d810bf4`) — caches keyed at module/instance level make behavior depend on call order
  and can serve stale data after a reparse. At these volumes (26k player_ratings,
  <100ms queries) they're premature. Replace with simple per-request queries or clear
  invalidation. **Highest-value simplification.**
- **Python forward-fill / bisect key tricks** (`1cb8deb`, `ac680d7`, `ebcc5dc`,
  `53ae543`, `0a9b151`) — replaced ClickHouse operations with Python loops/bisects keyed
  on `(played_at, match_id)`. These are the "timestamp-tie" complexity we just patched.
  Consider restoring clearer ClickHouse-side expressions where ClickHouse can do it
  correctly (esp. ASOF where semantics were the problem).

**Keep (correctness/real wins):** `4220896` (FINAL on player_ratings — correctness fix),
`5b739a1` (schema init once — safe), FINAL-drops that provably have no duplicates.

## Migration plan (safe, reversible)

Work on a branch, never touching origin/master working state.

1. Branch `schema-dedup`.
2. Schema: update `db_schema.py` DDL (drop dead columns/table).
3. Write path: update `db_client.py` INSERTs and `match_parser.py` / `rankings_compute.py`
   to stop writing dropped columns.
4. Read path: update `data_provider.py` queries that referenced dropped columns
   (match_maps names, player_ratings.player_name) to JOIN to source tables.
5. Data migration (ClickHouse):
   - `ALTER TABLE ... DROP COLUMN` for dead columns (ClickHouse `DROP COLUMN` is
     metadata-only, cheap, reversible via re-add + reinsert).
   - Drop `tournament_details`.
6. Verify: fresh parse + full query smoke test against live DB; compare outputs before/after.
7. Commit + PR.

## Open questions for you

1. **Confirm drop list** — OK to drop `matches.status`, `tournament_details`,
   `match_maps.player*_name`, `player_ratings.player_name`?
2. **Keep the hot denorm columns** (`matches.tournament_name`, `matches.player*_name/country`)?
   Removing them is the biggest rewrite (69 refs) for marginal storage gain.
3. **How far on the perf-query cleanup?** Just the module/instance caches (safest), or also
   the Python-side rating-history logic (bigger, more churn)?
4. **Live migration vs fresh rebuild?** `reset.py` can rebuild from scratch. If the raw
   HTML is re-parseable, a fresh rebuild is far safer than ALTER on 83k rows. Is that OK,
   or must existing parsed data be preserved in place?
