"""Backfill matches that exist only in a tournament's external bracket.

Some events publish a full bracket on the provider (Toornament &co.) while
PlusForward only ever gets match *posts* for part of it. The missing rounds
have no `/post/<id>/` page at all, so discovery never sees them, the parser
never stores them, and ratings silently skip them.

This module reconstructs those matches from the tournament's Toornament
bracket and inserts them into `matches`, so the rating replay includes them.

What it does NOT do: it never changes which bracket source is *preferred* for
display, and it never rewrites the stored bracket. It only reads the provider
payload as *evidence* for slots PlusForward never posted, and appends rows to
`matches`.

Safety rules (a slot is inserted only when all hold):

  - the provider bracket's stages are all complete (no in-progress bracket);
  - both opponents resolve to exactly one known player id;
  - the slot has a definite winner;
  - the slot has a real kickoff time from the provider;
  - no existing `matches` row already accounts for it.

Accounting consumes each stored match at most once, scoped to its bracket half
(winners / losers / grand final):

  1. exact player pair (either orientation);
  2. same winner, nearest kickoff time — absorbs provider/PlusForward opponent
     disagreements, which are NOT gaps. Observed: tid 95229 WB R3 is
     "Keltz vs COFFIN FILLER" on PF but "Keltz vs Coffee by Tomo" on
     Toornament; Keltz's win is already stored and must not be rated twice.

Round numbers are deliberately NOT used to scope pass 1: PlusForward collapses
some provider rounds into one label (tid 95229 stores provider rounds 3 and 4
both as "WB R3"), so a round-scoped bucket would look empty and duplicate
already-stored matches.

Synthetic match ids: a bracket-only match has no PlusForward post id, so it
gets a deterministic id derived from (tournament, source, provider match id).
`SYNTHETIC_BASE` keeps these far above any real post id, so
`is_synthetic_match_id()` can tell them apart without a schema change — the web
layer uses it to suppress the "view on PlusForward" link, which would 404.

Usage:
    python -m src.bracket_match_backfill --tournament 95230 --dry-run
    python -m src.bracket_match_backfill --tournament 95230 --tournament 95229
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.db_client import Database
from src.bracket_fetcher import SYNTHETIC_MATCH_ID_BASE

logger = logging.getLogger(__name__)

# Brackets that only exist on an external provider. 'plusforward' is the
# native bracket and mirrors exactly the posts PF published, so it can never
# reveal a gap on its own.
EXTERNAL_SOURCES = (
    "toornament", "shambler", "egb", "kuachi", "challonge", "battlefy", "startgg",
)

# Synthetic match ids live at 2**56: far above any PlusForward post id
# (~1e5 today) and well inside UInt64. Defined in bracket_fetcher so the
# bracket preference coverage guard can exclude these rows without importing
# this module (which would be circular).
SYNTHETIC_BASE = SYNTHETIC_MATCH_ID_BASE
_SYNTHETIC_MASK = (1 << 48) - 1

_GROUP_WINNERS = "winners"
_GROUP_LOSERS = "losers"
_GROUP_GRAND = "grand"

# Pass-2 time window: a stored row must fall within this of the bracket slot to
# count as the same game under a different opponent name. Wide enough for
# provider/PlusForward clock skew, narrow enough that a genuine rematch hours
# later is not swallowed.
_PASS2_MAX_DELTA_SECONDS = 6 * 3600

# Toornament timestamps arrive as ISO-8601 with an offset ("+00:00").
_PF_UTC = timezone.utc


def is_synthetic_match_id(match_id: int) -> bool:
    """True for bracket-derived match ids (no PlusForward post behind them)."""
    return int(match_id) >= SYNTHETIC_BASE


def synthetic_match_id(tournament_id: int, source: str, key: str) -> int:
    """Deterministic id for a bracket slot, stable across runs and resets."""
    digest = hashlib.blake2b(
        f"{tournament_id}:{source}:{key}".encode("utf-8"), digest_size=6,
    ).digest()
    return SYNTHETIC_BASE + (int.from_bytes(digest, "big") & _SYNTHETIC_MASK)


# ----------------------------------------------------------------------------
# Stage naming (must match the PF-parsed style of the same event)
# ----------------------------------------------------------------------------

def _group_kind(name: str) -> str | None:
    n = (name or "").lower()
    if "grand" in n:
        return _GROUP_GRAND
    if "loser" in n or "lower" in n:
        return _GROUP_LOSERS
    if "winner" in n or "upper" in n:
        return _GROUP_WINNERS
    return None


def stage_name_for(kind: str, round_no: int, is_last_round: bool) -> str:
    """Canonical stage name: 'WB R2' / 'LB R3' / 'WB Final' / 'Grand Final'."""
    if kind == _GROUP_GRAND:
        return "Grand Final"
    prefix = "WB" if kind == _GROUP_WINNERS else "LB"
    return f"{prefix} Final" if is_last_round else f"{prefix} R{round_no}"


def _parse_db_stage(stage_name: str) -> str | None:
    """Map a stored stage_name to its bracket half, or None if not elimination.

    Only the elimination naming used by the parser for these events is
    understood; anything else (group-stage labels such as "Group A - R2")
    returns None and is ignored by accounting.
    """
    s = (stage_name or "").strip().lower()
    if s == "grand final":
        return _GROUP_GRAND
    for prefix, kind in (("wb ", _GROUP_WINNERS), ("ub ", _GROUP_WINNERS),
                         ("lb ", _GROUP_LOSERS)):
        if s.startswith(prefix):
            return kind
    return None


def _to_utc(value) -> datetime | None:
    """Parse a provider timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_PF_UTC)
    return dt.astimezone(_PF_UTC)


def _server_tz(db: Database):
    """The ClickHouse server timezone for naive DB DateTime values.

    The driver hands back naive datetimes whose wall clock is in the server
    timezone (Europe/Moscow since the native-ClickHouse migration), so any
    comparison against provider timestamps has to go through this frame.
    """
    global _SERVER_TZ_CACHE
    if _SERVER_TZ_CACHE is None:
        try:
            name = db.client.execute("SELECT timezone()")[0][0]
            from zoneinfo import ZoneInfo
            _SERVER_TZ_CACHE = ZoneInfo(name)
        except Exception:
            _SERVER_TZ_CACHE = timezone.utc
    return _SERVER_TZ_CACHE


_SERVER_TZ_CACHE = None


# ----------------------------------------------------------------------------
# Identity resolution
# ----------------------------------------------------------------------------

# Names that cannot be resolved mechanically, with the evidence for each.
# Anything else is resolved by exact canonical name / alias, the Toornament
# platform name (`playerUser.name`), or a unique clan-tag token — and skipped
# when ambiguous.
#
#   'esports ready' -> 17647 'kuru'
#       Toornament participant user 4109303242905837568 ("kuru_tc") is
#       'esports ready' in NAQCL #58/#73/#75. In NAQCL #58 WB R2 that slot is
#       Toornament 'Aster Yup vs esports ready' == PF post 93685 'Yup vs kuru'
#       (player 17647).
#   'mijkeeee' -> 14940 'imijkeee'
#       tid 95229 WB R2: Toornament 'AN Thri11er vs mijkeeee' == PF post
#       95274 'Thri11er vs imijkeee' (player 14940). One letter differs.
OVERRIDES: dict[str, int] = {
    "esports ready": 17647,
    "mijkeeee": 14940,
}


class PlayerResolver:
    """Resolve bracket participant names to PlusForward player ids.

    Every candidate key must hit exactly one player; ambiguous keys stay
    unresolved so a wrong id can never enter the rating replay.
    """

    def __init__(self, db: Database, overrides: dict[str, int] | None = None):
        self._name: dict[str, set[int]] = {}
        for pid, name in db.client.execute(
            "SELECT player_id, name FROM players FINAL WHERE name != ''"
        ):
            self._name.setdefault(name.strip().lower(), set()).add(pid)
        self._alias: dict[str, set[int]] = {}
        for pid, name in db.client.execute(
            "SELECT player_id, name FROM player_aliases FINAL WHERE name != ''"
        ):
            self._alias.setdefault(name.strip().lower(), set()).add(pid)
        self._overrides = {
            k.strip().lower(): v for k, v in (overrides or OVERRIDES).items()
        }
        self.resolved: dict[str, int] = {}
        self.unresolved: dict[str, str] = {}

    def _exact(self, key: str) -> int | None:
        if not key:
            return None
        for table in (self._name, self._alias):
            hit = table.get(key)
            if hit:
                return next(iter(hit)) if len(hit) == 1 else None
        return None

    def _tokens(self, key: str) -> int | None:
        """Clan-tagged names: 'estoty St0n3', 'KMA Frachi', '4K Monterey'.

        Only tokens of 4+ characters count, so 'Mr T' cannot match by 't'.
        """
        for tok in [t for t in key.split() if len(t) >= 4]:
            hit = self._exact(tok)
            if hit:
                return hit
        return None

    def resolve(self, name: str, platform_name: str = "") -> int | None:
        keys = [k.strip().lower() for k in (name or "", platform_name or "")]
        for key in keys:
            if key and key in self._overrides:
                self.resolved[name] = self._overrides[key]
                return self._overrides[key]
        for key in keys:
            hit = self._exact(key) if key else None
            if hit:
                self.resolved[name] = hit
                return hit
        for key in keys:
            hit = self._tokens(key) if key else None
            if hit:
                self.resolved[name] = hit
                return hit
        self.unresolved[name] = "no unique player match"
        return None


# ----------------------------------------------------------------------------
# Slot extraction
# ----------------------------------------------------------------------------

@dataclass
class Slot:
    group: str
    round_no: int
    is_last_round: bool
    p1_id: int
    p2_id: int
    winner_id: int
    played_at: datetime
    score1: int
    score2: int
    key: str
    p1_name: str = ""
    p2_name: str = ""

    @property
    def stage_name(self) -> str:
        return stage_name_for(self.group, self.round_no, self.is_last_round)

    @property
    def pair(self) -> frozenset:
        return frozenset((self.p1_id, self.p2_id))


def _score_pair(opps: list[dict]) -> tuple[int, int]:
    """Provider scores -> Int8 pair using PF's own walkover encoding.

    PlusForward stores a no-show side as -1 and the winner as 0. When the
    provider reports a forfeit we mirror that; when it simply has no score we
    store 0:0 and rely on winner_id (the rating kernel honours a recorded
    winner even at equal scores).
    """
    s1, s2 = opps[0].get("score"), opps[1].get("score")
    ff1 = bool(opps[0].get("forfeit"))
    ff2 = bool(opps[1].get("forfeit"))
    if s1 is None and s2 is None:
        # PF writes the WINNER as 0 and the side that did not show as -1, so
        # the forfeiting player takes the -1, not their opponent.
        if ff1 and not ff2:
            return -1, 0
        if ff2 and not ff1:
            return 0, -1
        return 0, 0
    return int(s1 or 0), int(s2 or 0)


def extract_toornament_slots(payload: dict, resolver: PlayerResolver,
                             platforms: dict[str, str] | None = None
                             ) -> tuple[list[Slot], list[str]]:
    """Build bracket slots from a Toornament payload.

    Slots without a definite winner, a real kickoff time, or two distinct
    resolved players are dropped with a note.
    """
    matches = payload.get("matches") or []
    notes: list[str] = []
    platforms = platforms or {}
    if not matches:
        return [], ["payload has no matches"]

    rounds_by_group: dict[str, set[int]] = {}
    for m in matches:
        gid = (m.get("group") or {}).get("id")
        rounds_by_group.setdefault(gid, set()).add(
            (m.get("round") or {}).get("number", 0))
    last_round = {gid: max(r) for gid, r in rounds_by_group.items()}

    slots: list[Slot] = []
    for m in matches:
        gname = (m.get("group") or {}).get("name", "")
        kind = _group_kind(gname)
        rno = (m.get("round") or {}).get("number", 0)
        gid = (m.get("group") or {}).get("id")
        opps = m.get("opponents") or []
        if kind is None:
            notes.append(f"unknown group {gname!r}, skipped")
            continue
        if len(opps) != 2:
            notes.append(f"{gname} R{rno}: {len(opps)} opponents, skipped")
            continue
        names = [(o.get("participant") or {}).get("name", "") for o in opps]
        ids = [
            resolver.resolve(n, platforms.get(n, "") or
                             (o.get("participant") or {}).get("platform_name", ""))
            for n, o in zip(names, opps)
        ]
        if any(i is None for i in ids):
            notes.append(f"{gname} R{rno}: unresolved opponent {names!r}, skipped")
            continue
        if ids[0] == ids[1]:
            notes.append(f"{gname} R{rno}: both sides resolve to {ids[0]}, skipped")
            continue
        r1, r2 = opps[0].get("result"), opps[1].get("result")
        if r1 == "win" and r2 != "win":
            winner = ids[0]
        elif r2 == "win" and r1 != "win":
            winner = ids[1]
        else:
            notes.append(f"{gname} R{rno}: no definite winner ({names!r}), skipped")
            continue
        played = _to_utc(m.get("played_at") or m.get("playedAt") or "")
        if played is None:
            notes.append(f"{gname} R{rno}: no kickoff time ({names!r}), skipped")
            continue
        s1, s2 = _score_pair(opps)
        slots.append(Slot(
            group=kind,
            round_no=rno,
            is_last_round=(rno == last_round.get(gid)),
            p1_id=ids[0], p2_id=ids[1], winner_id=winner,
            played_at=played, score1=s1, score2=s2,
            key=str(m.get("id") or f"{gid}:{rno}:{sorted(ids)}"),
            p1_name=names[0], p2_name=names[1],
        ))
    return slots, notes


# ----------------------------------------------------------------------------
# Payload acquisition
# ----------------------------------------------------------------------------

def latest_external_payload(db: Database, tournament_id: int) -> tuple[str, dict] | None:
    """Newest *external* raw bracket payload for a tournament.

    `raw_brackets` holds one row per fetch and the newest row is often the
    PlusForward-native bracket, which cannot reveal a gap. Pick the newest row
    among the external providers instead.
    """
    rows = db.client.execute(
        "SELECT source, payload FROM raw_brackets "
        "WHERE tournament_id = %(t)s AND source IN %(s)s "
        "ORDER BY fetched_at DESC",
        {"t": tournament_id, "s": EXTERNAL_SOURCES},
    )
    for source, payload in rows:
        try:
            parsed = json.loads(payload or "{}")
        except Exception:
            continue
        if parsed and parsed.get("matches"):
            return source, parsed
    return None


def _payload_has_slot_metadata(payload: dict) -> bool:
    """True when the cached projection carries match ids and kickoff times.

    Payloads cached before this data was retained have neither, which makes
    gap detection impossible (no stable slot key, no played_at).
    """
    matches = payload.get("matches") or []
    if not matches:
        return False
    return all(m.get("id") and m.get("played_at") is not None for m in matches)


def _toornament_ref(db: Database, tournament_id: int) -> int | None:
    """Toornament tournament id linked from the PF tournament page."""
    from src.bracket_fetcher import BracketFetcher
    html = db.get_tournament_html(tournament_id)
    src = BracketFetcher.detect_source(html or "")
    if src and src[0] == "toornament":
        return src[1]
    return None


def fetch_participant_platforms(fetcher, toornament_id: int) -> dict[str, str]:
    """Toornament participant name -> platform account name.

    Platform names disambiguate clan-tagged entries ('Mr T' -> 'Mr Trouble',
    'carson' -> 'CARS0N', 'BASE' -> 'base'). Best-effort: an empty map just
    leaves those slots unresolved.
    """
    out: dict[str, str] = {}
    try:
        data = fetcher._json_get(
            f"{fetcher.API_TOORNAMENT}/participants",
            {"tournament_ids": toornament_id, "offset": 0, "limit": 50},
        )
    except Exception as e:
        logger.debug("participants fetch failed for %s: %s", toornament_id, e)
        return out
    for p in (data or {}).get("items", []):
        name = (p.get("name") or "").strip()
        user = (p.get("playerUser") or {}).get("name") or ""
        if name and user:
            out[name] = user.strip()
    return out


def acquire_payload(db: Database, tournament_id: int, allow_network: bool = True) -> dict:
    """Return {source, payload, platforms, needs_network, note}.

    Uses the cached external payload when it carries the slot metadata this
    tool needs; otherwise re-reads the provider live (without touching the
    stored bracket) because the old projection dropped ids and kickoff times.
    """
    found = latest_external_payload(db, tournament_id)
    if found and _payload_has_slot_metadata(found[1]):
        return {"source": found[0], "payload": found[1], "platforms": {}}

    if not allow_network:
        return {"source": found[0] if found else "", "payload": {},
                "platforms": {}, "needs_network": True,
                "note": "cached payload lacks match ids/kickoff times"}

    from src.bracket_fetcher import BracketFetcher
    ref = _toornament_ref(db, tournament_id)
    if not ref:
        return {"source": "", "payload": {}, "platforms": {},
                "note": "no toornament link on the tournament page"}
    fetcher = BracketFetcher(db)
    payload = fetcher._toornament_payload(ref)
    platforms = fetch_participant_platforms(fetcher, ref)
    return {"source": "toornament", "payload": payload or {},
            "platforms": platforms}


# ----------------------------------------------------------------------------
# Database access
# ----------------------------------------------------------------------------

def stored_matches(db: Database, tournament_id: int) -> list[dict]:
    rows = db.client.execute(
        "SELECT match_id, player1_id, player2_id, winner_id, stage_name, "
        "played_at, player1_score, player2_score "
        "FROM matches FINAL WHERE tournament_id = %(t)s",
        {"t": tournament_id},
    )
    return [
        {"match_id": r[0], "p1": r[1], "p2": r[2], "winner": r[3],
         "stage_name": r[4], "played_at": r[5],
         "s1": r[6], "s2": r[7], "group": _parse_db_stage(r[4])}
        for r in rows
    ]


def _tournament_context(db: Database, tournament_id: int, rows: list[dict]) -> dict:
    """Game id + match format for the event, taken from its own parsed rows."""
    info = db.client.execute(
        "SELECT game FROM tournaments FINAL WHERE tournament_id = %(t)s",
        {"t": tournament_id},
    )
    game_name = info[0][0] if info else ""
    game_id = 0
    if rows:
        res = db.client.execute(
            "SELECT game_id FROM matches FINAL WHERE tournament_id = %(t)s "
            "AND game_id > 0 LIMIT 1", {"t": tournament_id},
        )
        if res:
            game_id = res[0][0]
    if not game_id and game_name:
        game_id = db.resolve_game_id(game_name)

    # The rating kernel only rates formats matching '%duel%' or '%1v1%'.
    # Reuse the event's own format so backfilled rows are rated exactly like
    # their siblings.
    res = db.client.execute(
        "SELECT match_format, count() AS c FROM matches FINAL "
        "WHERE tournament_id = %(t)s AND match_format != '' "
        "GROUP BY match_format ORDER BY c DESC LIMIT 1",
        {"t": tournament_id},
    )
    fmt = res[0][0] if res else ""
    if "duel" not in (fmt or "").lower() and "1v1" not in (fmt or "").lower():
        fmt = "Duel"
    return {"game_id": game_id, "game": game_name, "match_format": fmt}


def _countries(db: Database, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = db.client.execute(
        "SELECT player_id, country FROM players FINAL WHERE player_id IN %(i)s",
        {"i": tuple(ids)},
    )
    return {r[0]: r[1] for r in rows}


# ----------------------------------------------------------------------------
# Accounting: bracket slots vs stored matches
# ----------------------------------------------------------------------------

@dataclass
class Plan:
    tournament_id: int
    source: str = ""
    insert: list[Slot] = field(default_factory=list)
    # Synthetic rows we already wrote whose stored scores no longer match the
    # bracket (e.g. after a scoring-convention fix). Each entry carries the
    # stored match_id so the rewrite targets that exact row.
    repair: list[tuple[Slot, int]] = field(default_factory=list)
    already_present: int = 0
    disagreements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    status: str = "ok"

    @property
    def slots(self) -> int:
        return len(self.insert) + len(self.repair) + self.already_present

    def summary(self) -> dict:
        return {
            "tournament_id": self.tournament_id,
            "source": self.source,
            "status": self.status,
            "bracket_slots": self.slots,
            "already_present": self.already_present,
            "missing": len(self.insert),
            "repair": len(self.repair),
            "missing_stages": sorted({s.stage_name for s in self.insert}),
            "missing_detail": [
                {"stage": s.stage_name, "p1": s.p1_name, "p2": s.p2_name,
                 "played_at": s.played_at.isoformat()}
                for s in sorted(self.insert, key=lambda s: s.played_at)
            ],
            "disagreements": self.disagreements,
            "notes": self.notes,
            "game": self.context.get("game"),
        }


def build_plan(db: Database, tournament_id: int,
               resolver: PlayerResolver | None = None,
               allow_network: bool = True) -> Plan:
    plan = Plan(tournament_id=tournament_id)

    got = acquire_payload(db, tournament_id, allow_network=allow_network)
    plan.source = got.get("source", "")
    if got.get("note"):
        plan.status = got["note"]
        return plan
    payload = got.get("payload") or {}
    if not payload.get("stages"):
        plan.status = "payload has no stages" if payload else "no provider payload"
        return plan
    if not all((st or {}).get("status") == "completed"
               for st in payload.get("stages") or []):
        plan.status = "provider bracket is not complete yet"
        return plan

    resolver = resolver or PlayerResolver(db)
    slots, notes = extract_toornament_slots(payload, resolver, got.get("platforms"))
    plan.notes.extend(notes)
    plan.notes.extend(
        f"unresolved name {n!r}: {why}" for n, why in resolver.unresolved.items())

    rows = stored_matches(db, tournament_id)
    plan.context = _tournament_context(db, tournament_id, rows)

    used: set[int] = set()
    ignored = [r for r in rows if r["group"] is None]
    pool = [r for r in rows if r["group"] is not None]

    # Stored played_at values come back as *naive wall clock in the ClickHouse
    # server timezone* (Europe/Moscow today), while provider timestamps are
    # aware UTC. Convert the provider side into the same wall-clock frame so
    # the kickoff comparison is exact rather than off by the server offset.
    server_tz = _server_tz(db)

    def db_wall(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(server_tz).replace(tzinfo=None)

    def take(predicate, rank=None, group: str | None = None) -> dict | None:
        candidates = [
            r for r in pool
            if r["match_id"] not in used
            and (group is None or r["group"] == group)
            and predicate(r)
        ]
        if not candidates:
            return None
        if rank is not None:
            candidates.sort(key=rank)
        chosen = candidates[0]
        used.add(chosen["match_id"])
        return chosen

    # Pass 1 runs for EVERY slot before pass 2 is allowed to touch anything.
    # Doing pass 2 inline (per slot, in kickoff order) lets a fuzzy match steal
    # a stored row that a later slot matches exactly — observed on tid 95230,
    # where the "Keltz vs carson" LB/WB slots consumed rows belonging to
    # already-stored matches and reported real results as missing.
    unmatched: list[Slot] = []
    for slot in sorted(slots, key=lambda s: s.played_at):
        hit = take(lambda r: {r["p1"], r["p2"]} == set(slot.pair),
                   group=slot.group)
        if hit:
            plan.already_present += 1
            # A synthetic row is ours to maintain: if the bracket's score
            # disagrees with what we stored, rewrite it in place.
            if is_synthetic_match_id(hit["match_id"]) and \
                    (hit["s1"], hit["s2"]) != (slot.score1, slot.score2):
                plan.repair.append((slot, hit["match_id"]))
        else:
            unmatched.append(slot)

    for slot in unmatched:
        slot_wall = db_wall(slot.played_at)

        # Same winner + same bracket half + a kickoff within a few hours: the
        # result is already stored under a different opponent name (provider
        # and PlusForward disagree), so inserting it would double-rate it.
        # Time is only a proximity filter, never an identity: PF stores
        # *scheduled round slots* while Toornament stores *actual kickoff*, so
        # the two clocks legitimately differ by a couple of hours.
        def same_winner(r):
            if r["group"] != slot.group or r["winner"] != slot.winner_id:
                return False
            return abs((r["played_at"] - slot_wall).total_seconds()) \
                <= _PASS2_MAX_DELTA_SECONDS

        hit = take(same_winner,
                   rank=lambda r: abs((r["played_at"] - slot_wall).total_seconds()),
                   group=slot.group)
        if hit:
            plan.already_present += 1
            plan.disagreements.append(
                f"{slot.stage_name}: bracket lists {slot.p1_name} vs "
                f"{slot.p2_name}; already stored as match {hit['match_id']} "
                f"with the same winner — not a gap")
            continue

        plan.insert.append(slot)

    if ignored:
        plan.notes.append(
            f"{len(ignored)} stored match(es) have non-elimination stage names "
            f"and were ignored: {sorted({r['stage_name'] for r in ignored})}")
    return plan


# ----------------------------------------------------------------------------
# Insert
# ----------------------------------------------------------------------------

def apply_plan(db: Database, plan: Plan, dry_run: bool = False) -> list[int]:
    """Insert/replace the planned slots. Returns the affected match ids."""
    todo: list[tuple[Slot, int]] = [
        (s, synthetic_match_id(plan.tournament_id, plan.source, s.key))
        for s in plan.insert
    ] + list(plan.repair)
    if not todo:
        return []
    ctx = plan.context
    if not ctx.get("game_id"):
        raise RuntimeError(
            f"tournament {plan.tournament_id}: no game_id for backfill")

    countries = _countries(
        db, {p for s, _ in todo for p in (s.p1_id, s.p2_id)})
    rows, ids, seen = [], [], set()
    for slot, mid in todo:
        if mid in seen:
            logger.warning("tournament %s: duplicate match id for slot %s",
                           plan.tournament_id, slot.key)
            continue
        seen.add(mid)
        ids.append(mid)
        rows.append((
            mid,
            slot.p1_id, slot.p2_id,
            max(-128, min(127, slot.score1)),
            max(-128, min(127, slot.score2)),
            slot.winner_id,
            ctx["game_id"],
            ctx["match_format"],
            plan.tournament_id,
            slot.stage_name,
            slot.played_at,          # aware UTC, like the parser writes
            countries.get(slot.p1_id, ""),
            countries.get(slot.p2_id, ""),
            f"Result taken from the tournament bracket ({plan.source}); "
            f"PlusForward has no match post for this game.",
        ))

    if dry_run:
        return ids

    # ReplacingMergeTree ORDER BY (game_id, played_at, match_id): re-running
    # the backfill overwrites the same rows instead of duplicating them.
    db.client.execute(
        "INSERT INTO matches "
        "(match_id, player1_id, player2_id, player1_score, player2_score, "
        " winner_id, game_id, match_format, tournament_id, stage_name, "
        " played_at, player1_country, player2_country, information) VALUES",
        rows,
    )
    return ids


def backfill_tournament(db: Database, tournament_id: int, dry_run: bool = False,
                        allow_network: bool = True) -> dict:
    plan = build_plan(db, tournament_id, allow_network=allow_network)
    ids = apply_plan(db, plan, dry_run=dry_run) if (plan.insert or plan.repair) else []
    out = plan.summary()
    out["inserted"] = len(plan.insert) if not dry_run else 0
    out["repaired"] = len(plan.repair) if not dry_run else 0
    out["match_ids"] = ids
    if dry_run:
        out["dry_run"] = True
    return out


def backfill(db: Database, tournament_ids: list[int], dry_run: bool = False,
             allow_network: bool = True) -> list[dict]:
    return [backfill_tournament(db, tid, dry_run=dry_run,
                                allow_network=allow_network)
            for tid in tournament_ids]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Backfill matches that only exist in a tournament bracket")
    ap.add_argument("--tournament", type=int, action="append", required=True,
                    help="tournament id (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="use only the cached provider payload")
    args = ap.parse_args()

    db = Database()
    try:
        for res in backfill(db, args.tournament, dry_run=args.dry_run,
                            allow_network=not args.offline):
            print(json.dumps(res, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
