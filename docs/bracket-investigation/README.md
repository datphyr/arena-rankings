# Bracket parsing — investigation results

Date: 2026-08-14 · No project changes made (only /tmp snippets).

## TL;DR

- **PlusForward does NOT host bracket data.** Its "Groups / Brackets" tab is a JS stub
  that calls `/ajax_misc.php?tourneybrackets=1&pid=<id>` and gets back a **single link**
  to wherever brackets actually live.
- The bracket sources are a **fragmented mix**:
  - **Toornament** (`play.toornament.com`) — ✅ **fully public, no-auth, structured JSON API**
  - **shambler.site** — ✅ **also fully public, no-auth JSON API** (POST `data-brackets.php`)
  - **EGB** (`egb.com`) — ❌ no accessible API (connection failed)
  - **web.archive.org / cyberfight / eswc** — historical archive, no structured data
  - **Some tournaments (e.g. QWC 2023)** — the "bracket" is literally a screenshot image
- **Two sources are realistically parseable: Toornament + shambler.** Together they cover
  most modern brackets (NAQCL, Estoty Duel / 250 FPS, PRO leagues).

### Source counts (out of 1986 tournaments with raw HTML)

| Source | # from ajax scan | Notes |
|---|---|---|
| toornament | 429 | ~287 have a parseable modern `tournaments/<id>` link in cached HTML |
| shambler | 102 | all 57 unique URLs verified live (HTTP 200) |
| archive | 62 | web.archive / cyberfight / eswc — historical only |
| image | 34 | screenshot, no data |
| egb | 7 | unreachable |
| other | 942 | misc (player/profile links) |
| empty | 410 | no bracket |

> **Correction:** an earlier draft said shambler was dead (404). That was based on one
> sample (`/resp/brackets.php`). The real shambler paths (`/250fps/brackets.php` and
> `/brackets/brackets.php`) are **all live** and serve parseable JSON.

## How the PlusForward side works

1. Tournament page has tabs: `Info`, `Groups / Brackets`, `Final Rankings`, `Streams`.
   Server renders the brackets tab as **empty**:
   ```html
   <div class="inner_tc" data-tab="brackets"></div>
   ```
2. `pf_tabs.js` → `getTourneyBrackets(pid)` → `GET /ajax_misc.php?tourneybrackets=1&pid=<id>`
3. That endpoint returns **not bracket data, but a link**, e.g.:
   ```html
   <a href="https://play.toornament.com/en_US/tournaments/2540134907084726271/stages" class="bbcode_url">...</a>
   ```
   (Some return an `<img>` screenshot instead.)

We already have each tournament's raw PlusForward HTML in the `tournaments` table
(`raw_html`), so extracting the external bracket URL is straightforward — no new
fetch needed from PlusForward.

## The Toornament viewer public API (the good part)

`play.toornament.com/api/*` is a **read-only, unauthenticated JSON API** used by the
Toornament viewer SPA. Verified live (no auth headers, HTTP 200):

| Endpoint | Params | Notes |
|---|---|---|
| `GET /api/stages` | `tournament_ids=<id>` | bracket structure: name, type (`double_elimination`, etc.), status |
| `GET /api/groups` | `tournament_ids=<id>` or `stage_ids=` | "Winners Bracket", "Losers Bracket", … |
| `GET /api/rounds` | `tournament_ids=<id>` or `stage_ids=` | rounds per group |
| `GET /api/matches` | `tournament_ids=<id>` | per-match opponents, scores, results, schedule |
| `GET /api/bracket-nodes` | `stage_ids=<id>` | full node tree: depth, branch (`wb`/`lb`), opponents, source nodes → **this is the actual bracket graph** |
| `GET /api/participants` | `tournament_ids=<id>` | players/teams with names + `playerUser` ids |

Pagination via `offset`/`limit` (returns `range: {offset, length, total}`).

### Data shape (bracket-nodes)
Each node:
```json
{
  "id": "2541609375748845533",
  "depth": 8,
  "branch": "wb",                 // winners / losers bracket
  "opponents": [
    {
      "participant": { "id": "...", "type": "player", "name": "KMA Frachi",
                       "playerUser": {"id": "..."} },
      "sourceType": "none",       // or "match" -> sourceNode links prev match
      "sourceNode": null,
      "position": 1,
      "result": "win",            // win / loss / null
      "score": null
    }, ...
  ],
  "tournament": { "id": "...", "name": "...", "discipline": "quake_champions", ... }
}
```
`sourceType`/`sourceNode` gives the edges — enough to reconstruct and render a real
double-elimination bracket (winners/losers, grand final).

### Verification
Confirmed working for **multiple** tournaments (NAQCL #67 and Estoty #212), same
schema, zero auth.

## The shambler public API

`shambler.site/brackets/data-brackets.php` — **unauthenticated**, `POST` with
`cup=<id>&update=0` (update=1 polls for live updates). Returns a full JSON doc:

```json
{
  "id": 250,
  "title": "EGB Cup #57",
  "status": 2,
  "brackets_type": 1,
  "maps": ["Aerowalk", "Battleforged", ...],
  "players": [{"id": 25, "discord_name": "DANCHY", "country": "Russia", ...}],
  "brackets": [
    {"id": "wb", "matches": [{"id": 7172, "bracket": "wb", "round": 0, "num": 0,
       "games": 3, "status": 2, "scores": [1, 0], "players": [72, 0]}]},
    {"id": "lb", "matches": [...]},
    {"id": "gf", "matches": [...]}   // grand final
  ],
  "results": [ [72], [70], ... ]       // final placement by player id
}
```
- `brackets` = winners (`wb`) / losers (`lb`) / grand final (`gf`) — complete double-elim.
- `players` maps numeric player ids → names/country.
- `results` = final standing order.
- Same no-auth, structured-JSON story as Toornament, just a different endpoint.

**Path note:** `/250fps/brackets.php?cup=N` 301-redirects to `/brackets/brackets.php?cup=N`.
The `data-brackets.php` endpoint sits under `/brackets/`.

## What this means for our project (if we ever build it)

Feasible scope:
1. From the already-cached `raw_html`, extract the `play.toornament.com/.../<tournament_id>` URL.
2. For Toornament-only tournaments, call the public API (stages → groups → rounds →
   bracket-nodes → matches/participants) and store a normalized bracket graph in
   ClickHouse (new table(s)) + render it on the tournament page.

Effort is moderate but **self-contained** (one source, one clean JSON schema, no auth).

### Caveats / not covered
- **Non-parseable brackets** (EGB, archives, screenshots, empty) — would render as
  "external link / image" as today. EGB is unreachable; archives have no structured data;
  some tournaments are just images.
- Shambler `/250fps/` URLs 301-redirect to `/brackets/` — normalize the path before calling.
- Shambler bracket data uses numeric player IDs resolved via the `players` array in the
  same JSON response (not global player ids like Toornament's `playerUser`).
- **final-standings endpoint** returns 302/404 on this API — but we already parse final
  standings from PlusForward, so no loss.
- Toornament URL patterns may vary (`/stages`, `/stages/`, old paths) — needs a small
  URL-normalization step. The `tournament_id` (19-digit) is what matters.

## Recommendation
Worth doing **if** we're OK with covering the two parseable sources (Toornament +
shambler) and keeping the external-link/image fallback for the rest. This covers
**~531 tournaments (~27%)** with real bracket data. "Parse everything" isn't achievable
— EGB is unreachable and archives/screenshots have no structured data.

## Example links (one per bracket-source type, from our DB)

**Toornament (parseable — public JSON API, HTTP 200)**
- NAQCL Duel Tournament #67 (id 94605):
  https://play.toornament.com/en_US/tournaments/2540134907084726271/stages
  - API: `https://play.toornament.com/api/bracket-nodes?stage_ids=2541609375656570879`
- Estoty Duel Tournament #212 (id 94587):
  https://play.toornament.com/en_US/tournaments/2540327274109573119/stages/

**EGB (not parseable — unreachable, HTTP 000)**
- EGB QC Cup #4 (id 94586): https://egb.com/cup#/t/qc-egb-cup-4
- EGB Cup #64 (id 94419): https://egb.com/cup#/t/ql-egb-cup-64

**EGB mirror (same as above, `.net` variant)**
- EGB QC Cup #2 (id 94291): https://egb.net/cup#/t/qc-egb-cup-2/bracket

**Shambler (parseable — public JSON API, all 57 URLs verified HTTP 200)**
- EGB Cup #57 (id 93693): https://shambler.site/brackets/brackets.php?cup=250
  - API: `POST https://shambler.site/brackets/data-brackets.php` body `cup=250&update=0`
- Blood LAN Finals (id 83579): https://shambler.site/resp/brackets.php?cup=67 (dead variant;
  the `/resp/` path 404s — real shambler links use `/250fps/` or `/brackets/`)

**Image/screenshot only (no data)**
- Quake World Championship 2023 (id 78572):
  https://www.plusforward.net/files/2023/78572/1_sans-titre.png

**Web-archive link only (historical, no structured data)**
- AMD Invitational XS Cup - 1v1 (id 81876):
  https://web.archive.org/web/20060114222357/http://www.cyberfight.org/site/coverage/22/

**No bracket at all**
- 250 FPS Showmatch - fire_bot vs baksteen (id 94358): empty result

**PlusForward source pages (context)**
- Tournament: https://www.plusforward.net/post/94605/NAQCL-Duel-Tournament-67/
- Brackets tab: https://www.plusforward.net/post/94605/NAQCL-Duel-Tournament-67/brackets/

## Files in this folder

- `README.md` — this investigation write-up
- `toornament_probe.js` — headless-chromium script that captured the live
  `play.toornament.com/api/*` calls (proves no-auth access)
- `sample-bracket-nodes.json` — real bracket-nodes API response (NAQCL #67, Playoffs stage)
- `sample-groups-rounds.json` — real groups/rounds API response
- `sample-matches.json` — real matches API response
- `sample-shambler-data.json` — real `data-brackets.php` JSON (EGB Cup #57, cup=250)
- `sample-shambler-page.html` — raw shambler brackets page shell (JS-rendered)
- `sample-plusforward-ajax-response.html` — what `/ajax_misc.php?tourneybrackets=1` returns
  (i.e. the bracket link, not data)
- `sample-plusforward-brackets-tab.html` — raw PlusForward brackets tab (empty server-side container)

