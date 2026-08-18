"""Bracket fetching + normalization for Toornament, shambler and EGB.

PlusForward does not host bracket data — its tournament pages link out to an
external provider. This module detects which provider a tournament uses (from
the cached PlusForward raw_html), fetches the bracket from that provider's
public (no-auth) API, and normalizes it into a single source-agnostic JSON shape
suitable for rendering:

    {
      "source": "toornament" | "shambler" | "egb",
      "title": "...",
      "stages": [{
        "name": "Playoffs",
        "groups": [{
          "name": "Winners Bracket",
          "rounds": [{
            "name": "WB Round 1",
            "round": 0,
            "matches": [
              {"p1": "KMA Frachi", "p2": "An1ml",
               "score1": null, "score2": null, "winner": "p1"}
            ]
          }]
        }]
      }]
    }

Providers:
  - Toornament: play.toornament.com/api/*  (GET, no auth)
  - shambler:   shambler.site/brackets/data-brackets.php (POST, no auth)
  - EGB:        cup.egb.net/tournaments/* (GET, no auth) — slug -> uuid -> bracket

Usage:
    from src.bracket_fetcher import BracketFetcher
    f = BracketFetcher(db)
    ok = f.fetch_for_tournament(94605)   # detect + fetch + store
"""

import json
import logging
import random
import re
import subprocess
import time
from datetime import datetime
from typing import Optional

from config import USER_AGENTS
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# These are external APIs, not PlusForward — separate, gentler rate limit.
BRACKET_RATE_LIMIT_DELAY = float(__import__("os").environ.get("BRACKET_RATE_LIMIT_DELAY", "0.4"))
BRACKET_HTTP_TIMEOUT = int(__import__("os").environ.get("BRACKET_HTTP_TIMEOUT", "15"))

# Retry on non-JSON responses (intermittent Cloudflare challenge pages).
_JSON_RETRIES = int(__import__("os").environ.get("BRACKET_JSON_RETRIES", "4"))
_JSON_RETRY_DELAY = float(__import__("os").environ.get("BRACKET_JSON_RETRY_DELAY", "2.0"))

# Regexes to detect the provider + id from PlusForward raw_html.
_TOORNAMENT_RE = re.compile(
    r"play\.toornament\.com/[a-z_]+/tournaments/(\d+)", re.IGNORECASE)
_SHAMBLER_RE = re.compile(
    r"shambler\.site/(?:(?:250fps|brackets)/)?brackets\.php\?cup=(\d+)", re.IGNORECASE)
# EGB cup links: egb.com / egb.net / egabetz.com / egabe.online with a hash
# route /cup#/t/<slug> (optionally followed by /bracket). Slug is the part
# after /t/. egabe.online is an alias that no longer resolves in a browser but
# the API still serves the bracket (cup.egb.net), so we accept it for detection.
_EGB_RE = re.compile(
    r"(?:egb\.com|egb\.net|egabetz\.com|egabe\.online)/cup#/t/([a-z0-9_-]+)", re.IGNORECASE)

# kuachi.gg cups: /cups/<uuid>/stage/<stage_no> (kuachi cups — AU/NZ/Oceania
# AFPS tournaments). The bracket is served by the kuachi REST API.
_KUACHI_RE = re.compile(
    r"kuachi\.gg/cups/([0-9a-fA-F-]{36})/stage/(\d+)", re.IGNORECASE)

# Some PlusForward tournament pages load their bracket link dynamically (the
# "Groups / Brackets" tab is populated client-side). The bracket content —
# including the link to the external provider — is served by this AJAX
# endpoint keyed by tournament post id. We query it as a fallback when the
# static raw_html has no detectable bracket source.
_AJAX_BRACKETS_URL = "https://plusforward.net/ajax_misc.php"

# EGB side ordering for the bracket (winners -> losers -> grand final).
_EGB_SIDE_ORDER = {"WINNERS": 0, "LOSERS": 1, "GRAND_FINAL": 2, "GRAND_FINAL_RESET": 3}
_EGB_SIDE_NAME = {
    "WINNERS": "Winners Bracket",
    "LOSERS": "Losers Bracket",
    "GRAND_FINAL": "Grand Final",
    "GRAND_FINAL_RESET": "Grand Final (Reset)",
}

# Toornament API caps page size at 50 (64+ returns an out-of-range error).
_API_LIMIT = 50


class BracketFetcher:
    """Fetch + normalize + store brackets for a tournament."""

    # Class-level stats for observability.
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    no_source: int = 0

    def __init__(self, db, fetcher: PageFetcher = None):
        """
        Args:
            db: Database instance (for reading raw_html + storing bracket).
            fetcher: PageFetcher for reading cached tournament HTML. If None,
                creates one (used only for the DB round-trip fallback, not
                for external calls — those use the JSON fetcher below).
        """
        self._db = db
        self._html_fetcher = fetcher or PageFetcher()
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def fetch_for_tournament_if_needed(self, tournament_id: int, max_age_days: int = None,
                                       force: bool = False) -> bool:
        """Fetch + store a bracket only if it's missing or stale.

        Cheap no-network fast-path: if the tournament has no bracket source
        (no toornament/shambler link), returns False immediately. Then skips
        the fetch if a bracket is already stored and fresh.

        Args:
            tournament_id: PlusForward tournament id.
            max_age_days: refetch if the stored bracket is older than this.
                None = only fetch when missing.
            force: if True, always re-fetch + re-store even if a fresh bracket
                exists (used to refresh brackets for in-progress events).
        """
        raw_html = self._db.get_tournament_html(tournament_id)
        if not raw_html or not self.detect_source(raw_html):
            # No bracket link in the static HTML — but it may be loaded
            # dynamically ("Groups / Brackets" tab). Let fetch_for_tournament
            # decide via the AJAX fallback rather than returning False here.
            return self.fetch_for_tournament(tournament_id) if raw_html else False
        if not force:
            existing = self._db.get_tournament_bracket(tournament_id)
            if existing and existing.get("data"):
                # An empty stored bracket (no match data) doesn't count as
                # fetched — it was likely a transient empty API response that
                # got persisted. Always retry those.
                if not self._has_matches(existing["data"]):
                    return self.fetch_for_tournament(tournament_id)
                if max_age_days is None:
                    return False  # already stored, fresh enough
                fa = existing.get("fetched_at")
                if fa and (datetime.datetime.utcnow() - fa).days < max_age_days:
                    return False
        return self.fetch_for_tournament(tournament_id)

    @staticmethod
    def _has_matches(normalized: dict) -> bool:
        """True if the normalized bracket contains at least one match.

        An empty bracket (stages present but no groups/rounds/matches) is a
        transient API miss, not a real bracket — treat it as not-fetched.
        """
        for st in normalized.get("stages", []) or []:
            for g in st.get("groups", []) or []:
                for r in g.get("rounds", []) or []:
                    if r.get("matches"):
                        return True
        return False


    def fetch_for_tournament(self, tournament_id: int) -> bool:
        """Detect provider, fetch bracket, normalize, and store in the DB.

        Returns True if a bracket was stored, False if the tournament has no
        bracket source (or fetching failed).
        """
        raw_html = self._db.get_tournament_html(tournament_id)
        if not raw_html:
            BracketFetcher.no_source += 1
            return False

        source = self.detect_source(raw_html)
        if not source:
            # The static HTML may lack the bracket link (loaded dynamically
            # into the "Groups / Brackets" tab). Fall back to the AJAX
            # endpoint the page JS uses before declaring no source.
            source = self._detect_source_ajax(tournament_id)
        if not source:
            BracketFetcher.no_source += 1
            return False

        kind, ref = source  # ('toornament', tid), ('shambler', cup), ('egb', slug), ('kuachi', (cup_id, stage_no))
        try:
            if kind == "toornament":
                normalized = self._fetch_toornament(ref)
            elif kind == "shambler":
                normalized = self._fetch_shambler(ref)
            elif kind == "egb":
                normalized = self._fetch_egb(ref)
            else:  # kuachi
                cup_id, stage_no = ref
                normalized = self._fetch_kuachi(cup_id, stage_no)
        except Exception as e:
            BracketFetcher.failed += 1
            logger.warning(f"bracket fetch failed for tournament {tournament_id} ({kind}): {e}")
            return False

        if not normalized or not normalized.get("stages"):
            BracketFetcher.failed += 1
            logger.debug(f"tournament {tournament_id}: {kind} has no bracket data")
            return False

        # Don't persist an empty bracket (stages present but zero matches) —
        # that's a transient API miss, not a real bracket. Leaving it unstored
        # lets fetch_for_tournament_if_needed retry on a later pass.
        if not self._has_matches(normalized):
            BracketFetcher.failed += 1
            logger.warning(
                f"tournament {tournament_id}: {kind} bracket empty (no matches), not storing"
            )
            return False

        self._db.upsert_tournament_bracket(
            tournament_id, normalized.get("source", kind),
            json.dumps(normalized, ensure_ascii=False),
        )
        BracketFetcher.fetched += 1
        logger.debug(f"tournament {tournament_id}: stored {kind} bracket")
        return True

    @staticmethod
    def detect_source(raw_html: str):
        """Detect the bracket provider from PlusForward raw_html.

        Returns (kind, ref):
          ('toornament', <int tournament id>),
          ('shambler', <int cup id>) or
          ('egb', <slug>)
        or None if no bracket source is present.
        """
        m = _TOORNAMENT_RE.search(raw_html)
        if m:
            return ("toornament", int(m.group(1)))
        m = _SHAMBLER_RE.search(raw_html)
        if m:
            return ("shambler", int(m.group(1)))
        m = _EGB_RE.search(raw_html)
        if m:
            return ("egb", m.group(1))
        m = _KUACHI_RE.search(raw_html)
        if m:
            return ("kuachi", (m.group(1), int(m.group(2))))
        return None

    def _detect_source_ajax(self, tournament_id: int):
        """Detect the bracket provider via the dynamic PlusForward AJAX endpoint.

        Some tournament pages load their bracket link client-side (the "Groups
        / Brackets" tab), so the link is absent from the static raw_html. Query
        the same AJAX endpoint the page JS uses and run source detection on its
        response. Returns a (kind, ref) tuple like detect_source, or None if the
        endpoint yields no bracket link.
        """
        try:
            body = self._curl(
                "GET",
                f"{_AJAX_BRACKETS_URL}?tourneybrackets=1&pid={tournament_id}",
            )
        except Exception as e:
            logger.debug(f"ajax bracket detection failed for {tournament_id}: {e}")
            return None
        if not body:
            return None
        return self.detect_source(body)

    # ------------------------------------------------------------------
    # HTTP (JSON) helpers — external APIs, curl-based like PageFetcher
    # ------------------------------------------------------------------

    def _json_get(self, url: str, params: dict = None) -> Optional[dict]:
        """GET a JSON API endpoint and return parsed JSON (or None).

        Retries on non-JSON responses (e.g. intermittent Cloudflare challenge
        pages) so a single blocked request doesn't yield an empty bracket.
        """
        if params:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        for attempt in range(_JSON_RETRIES):
            body = self._curl("GET", url, attempt=attempt)
            if body is None:
                continue
            try:
                return json.loads(body)
            except Exception as e:
                logger.debug(f"bad JSON from {url} (attempt {attempt + 1}): {e}")
                time.sleep(_JSON_RETRY_DELAY + random.uniform(0, 0.3))
        return None

    def _json_post(self, url: str, data: dict) -> Optional[dict]:
        """POST form-encoded data to a JSON API endpoint (with retry)."""
        for attempt in range(_JSON_RETRIES):
            body = self._curl("POST", url, data=data, attempt=attempt)
            if body is None:
                continue
            try:
                return json.loads(body)
            except Exception as e:
                logger.debug(f"bad JSON from {url} (attempt {attempt + 1}): {e}")
                time.sleep(_JSON_RETRY_DELAY + random.uniform(0, 0.3))
        return None

    def _curl(self, method: str, url: str, data: dict = None, attempt: int = 0) -> Optional[str]:
        """Raw curl GET/POST, returning the response body (or None)."""
        self._rate_limit()
        ua = random.choice(USER_AGENTS)
        cmd = [
            "curl", "-s", "--compressed",
            "--connect-timeout", str(BRACKET_HTTP_TIMEOUT),
            "--max-time", str(BRACKET_HTTP_TIMEOUT),
            "-A", ua,
            "-H", "Accept: application/json",
        ]
        if method == "POST":
            cmd += ["-X", "POST"]
            if data:
                import urllib.parse
                cmd += ["--data", urllib.parse.urlencode(data)]
        cmd.append(url)
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=BRACKET_HTTP_TIMEOUT + 2)
            body = result.stdout.decode("utf-8", errors="replace")
            if result.returncode in (0, 28) and body:
                return body
            if attempt == 0:
                logger.debug(f"curl rc={result.returncode}, {len(body)}b for {url}")
        except (subprocess.TimeoutExpired, OSError) as e:
            if attempt == 0:
                logger.debug(f"curl failed for {url}: {e}")
        return None

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < BRACKET_RATE_LIMIT_DELAY:
            time.sleep(BRACKET_RATE_LIMIT_DELAY - elapsed + random.uniform(0, 0.2))
        self._last_request = time.time()

    # ------------------------------------------------------------------
    # Toornament
    # ------------------------------------------------------------------

    API_TOORNAMENT = "https://play.toornament.com/api"

    def _fetch_toornament(self, tournament_id: int) -> dict:
        """Fetch + normalize a Toornament bracket."""
        stages = self._toornament_stages(tournament_id)
        if not stages:
            return {"source": "toornament", "title": "", "stages": []}

        # Gather all matches for the tournament once (they carry stage/group/round refs).
        matches = self._toornament_matches(tournament_id)
        matches_by_stage = {}
        for m in matches:
            sid = m.get("stage", {}).get("id")
            matches_by_stage.setdefault(sid, []).append(m)

        result_stages = []
        for st in stages:
            sid = st["id"]
            sm = matches_by_stage.get(sid, [])
            groups = self._toornament_groups(sid, sm)
            result_stages.append({
                "name": st["name"],
                "groups": groups,
            })
        # Completeness: a Toornament bracket is full/final when all its stages
        # are 'completed' (per-match status can lag / show LIVE during the
        # grand final, so use the stage-level status — not per-match).
        complete = bool(stages) and all(st.get("status") == "completed" for st in stages)
        return {"source": "toornament", "title": "", "complete": complete,
                "stages": result_stages}

    def _toornament_stages(self, tournament_id: int) -> list[dict]:
        d = self._json_get(f"{self.API_TOORNAMENT}/stages",
                           {"tournament_ids": tournament_id, "offset": 0, "limit": _API_LIMIT})
        if not d:
            return []
        return [{"id": s["id"], "name": s.get("name", ""), "type": s.get("type", ""),
                 "status": s.get("status", ""), "closed": bool(s.get("closed", False))}
                for s in d.get("items", [])]

    def _toornament_matches(self, tournament_id: int) -> list[dict]:
        offset, limit, out = 0, _API_LIMIT, []
        while True:
            d = self._json_get(f"{self.API_TOORNAMENT}/matches",
                               {"tournament_ids": tournament_id,
                                "offset": offset, "limit": limit})
            if not d:
                break
            items = d.get("items", [])
            out.extend(items)
            rng = d.get("range", {})
            total = rng.get("total", 0)
            offset += len(items)
            if offset >= total or not items:
                break
        return out

    def _toornament_groups(self, stage_id: int, matches: list[dict]) -> list[dict]:
        """Group matches by group (Winners/Losers/Grand Final), then by round.

        Group names are taken from the matches themselves (always present),
        with the groups endpoint as a fallback — robust to transient failures
        of the /groups endpoint.
        """
        # Primary: group names from the matches' own group objects.
        gname = {}
        for m in matches:
            g = m.get("group", {})
            gid = g.get("id")
            if gid and gid not in gname and g.get("name"):
                gname[gid] = g["name"]
        # Fallback: the groups endpoint (may fail transiently).
        d = self._json_get(f"{self.API_TOORNAMENT}/groups",
                           {"stage_ids": stage_id, "offset": 0, "limit": _API_LIMIT})
        if d:
            for g in d.get("items", []):
                gname.setdefault(g["id"], g.get("name", ""))

        # Group matches by (group_id, round_number), preserving order.
        by_group = {}
        for m in matches:
            gid = m.get("group", {}).get("id")
            by_group.setdefault(gid, []).append(m)

        # Order groups: winners first, then losers, then grand final.
        def sort_key(gid):
            name = gname.get(gid, "").lower()
            if "winner" in name:
                return 0
            if "loser" in name:
                return 1
            if "grand" in name or "final" in name:
                return 2
            return 3

        ordered_gids = sorted(by_group.keys(), key=lambda g: (sort_key(g), gname.get(g, "")))

        out_groups = []
        for gid in ordered_gids:
            gm = by_group[gid]
            # Build rounds from matches.
            rounds_map = {}
            for m in gm:
                rnd = m.get("round", {})
                rn = rnd.get("number", 0)
                rounds_map.setdefault(rn, []).append(m)
            rounds = [self._toornament_round(rn, ms) for rn, ms in sorted(rounds_map.items())]
            out_groups.append({
                "name": gname.get(gid, ""),
                "rounds": rounds,
            })
        return out_groups

    @staticmethod
    def _toornament_round(round_number: int, matches: list[dict]) -> dict:
        # Match name from the first match's round name (e.g. "WB Round 1"),
        # falling back to "Round N".
        rname = ""
        if matches:
            rname = matches[0].get("round", {}).get("name", "")
        norm = []
        for m in matches:
            opps = m.get("opponents", []) or []

            def _opp(idx, field):
                if idx >= len(opps) or not opps[idx]:
                    return None
                o = opps[idx]
                if field == "name":
                    part = o.get("participant") or {}
                    return part.get("name", "") or ""
                return o.get(field)

            p1 = _opp(0, "name") or ""
            p2 = _opp(1, "name") or ""
            s1 = _opp(0, "score")
            s2 = _opp(1, "score")
            r1 = _opp(0, "result") or ""
            r2 = _opp(1, "result") or ""
            winner = None
            if r1 == "win":
                winner = "p1"
            elif r2 == "win":
                winner = "p2"
            norm.append({
                "p1": p1, "p2": p2,
                "score1": s1, "score2": s2,
                "winner": winner,
            })
        return {"name": rname, "round": round_number, "matches": norm}

    # ------------------------------------------------------------------
    # Shambler
    # ------------------------------------------------------------------

    API_SHAMBLER = "https://shambler.site/brackets/data-brackets.php"

    def _fetch_shambler(self, cup_id: int) -> dict:
        """Fetch + normalize a shambler bracket (POST data-brackets.php)."""
        d = self._json_post(self.API_SHAMBLER, {"cup": cup_id, "update": 0})
        if not d:
            return {"source": "shambler", "title": "", "stages": []}

        pmap = {p["id"]: p.get("discord_name", "") for p in d.get("players", [])}

        # shambler brackets: wb / lb / gf. Each is a list of matches with
        # round (0-based) + num. Treat each bracket id as a "group".
        stages = [{
            "name": d.get("title", ""),
            "groups": [self._shambler_group(b, pmap)
                       for b in d.get("brackets", [])],
        }]
        # Shambler status: 2 = finished, 1 = live/in-progress, 0 = not started.
        status = d.get("status")
        complete = (status == 2)
        return {"source": "shambler", "title": d.get("title", ""),
                "complete": complete, "status": status, "stages": stages}

    @staticmethod
    def _shambler_group(bracket: dict, pmap: dict) -> dict:
        name = {"wb": "Winners Bracket", "lb": "Losers Bracket", "gf": "Grand Final"} \
            .get(bracket.get("id"), bracket.get("id", ""))
        matches = bracket.get("matches", [])
        # Group by round (0-based).
        rounds_map = {}
        for m in matches:
            rn = m.get("round", 0)
            rounds_map.setdefault(rn, []).append(m)
        rounds = []
        for rn in sorted(rounds_map.keys()):
            ms = sorted(rounds_map[rn], key=lambda m: m.get("num", 0))
            norm = []
            for m in ms:
                players = m.get("players", [])
                scores = m.get("scores", [])
                p1 = pmap.get(players[0], "") if len(players) > 0 else ""
                p2 = pmap.get(players[1], "") if len(players) > 1 else ""
                s1 = scores[0] if len(scores) > 0 else None
                s2 = scores[1] if len(scores) > 1 else None
                winner = None
                if s1 is not None and s2 is not None and s1 != s2:
                    winner = "p1" if s1 > s2 else "p2"
                norm.append({"p1": p1, "p2": p2, "score1": s1, "score2": s2, "winner": winner})
            rounds.append({
                "name": f"Round {rn + 1}",
                "round": rn,
                "matches": norm,
            })
        return {"name": name, "rounds": rounds}

    # ------------------------------------------------------------------
    # EGB (cup.egb.net)
    # ------------------------------------------------------------------

    API_EGB = "https://cup.egb.net"

    def _fetch_egb(self, slug: str) -> dict:
        """Fetch + normalize an EGB bracket by cup slug.

        Flow: /tournaments/by-slug/<slug> -> tournament uuid, then
        /tournaments/<uuid>/bracket for the bracket graph.
        """
        meta = self._json_get(f"{self.API_EGB}/tournaments/by-slug/{slug}")
        if not meta or not meta.get("id"):
            return {"source": "egb", "title": "", "stages": []}
        tid = meta["id"]
        bracket = self._json_get(f"{self.API_EGB}/tournaments/{tid}/bracket")
        if not bracket or not bracket.get("matches"):
            return {"source": "egb", "title": meta.get("name", ""), "stages": []}
        return self._egb_normalize(meta, bracket)

    @classmethod
    def _egb_normalize(cls, meta: dict, bracket: dict) -> dict:
        """Normalize EGB's flat match list into the shared stages/groups schema.

        EGB matches carry: side (WINNERS/LOSERS/GRAND_FINAL[_RESET]), round
        (1-based), indexInRound, home/away ({type, participant}), status,
        winner (participant id) and score ({home, away}). We group by side
        (a "group") then by round, resolving participant ids to names.
        """
        pmap = {p["id"]: p.get("displayName", "") for p in bracket.get("participants", [])}

        # Split matches into side groups.
        by_side = {}
        for m in bracket.get("matches", []):
            by_side.setdefault(m.get("side", ""), []).append(m)

        groups = []
        for side in sorted(by_side, key=lambda s: _EGB_SIDE_ORDER.get(s, 9)):
            sm = by_side[side]
            # Group by round, order by round number then indexInRound.
            by_round = {}
            for m in sm:
                by_round.setdefault(m.get("round", 1), []).append(m)
            rounds = []
            for rn in sorted(by_round):
                rms = sorted(by_round[rn], key=lambda m: m.get("indexInRound", 0))
                norm = []
                for m in rms:
                    norm.append(cls._egb_match(m, pmap))
                rounds.append({"name": f"Round {rn}", "round": rn, "matches": norm})
            groups.append({"name": _EGB_SIDE_NAME.get(side, side), "rounds": rounds})

        # Finished when every match is terminal (no pending/live matches).
        statuses = {m.get("status") for m in bracket.get("matches", [])}
        complete = bool(statuses) and statuses.issubset(
            {"COMPLETED", "WALKOVER", "CANCELLED"})
        return {
            "source": "egb",
            "title": meta.get("name", ""),
            "format": bracket.get("format", ""),
            "complete": complete,
            "stages": [{"name": meta.get("name", ""), "groups": groups}],
        }

    @staticmethod
    def _egb_match(m: dict, pmap: dict) -> dict:
        """Normalize a single EGB match to {p1, p2, score1, score2, winner}."""
        home = m.get("home", {}) or {}
        away = m.get("away", {}) or {}

        def _name(slot):
            if slot.get("type") != "player" or not slot.get("participant"):
                return ""
            return pmap.get(slot.get("participant"), "")

        p1 = _name(home)
        p2 = _name(away)
        score = m.get("score") or {}
        s1 = score.get("home")
        s2 = score.get("away")
        winner = None
        wid = m.get("winner")
        if wid:
            if home.get("participant") == wid:
                winner = "p1"
            elif away.get("participant") == wid:
                winner = "p2"
        return {"p1": p1, "p2": p2, "score1": s1, "score2": s2, "winner": winner}

    # ------------------------------------------------------------------
    # kuachi.gg (kuachi cups)
    # ------------------------------------------------------------------
    API_KUACHI = "https://kuachi.gg/api"

    def _fetch_kuachi(self, cup_id: str, stage_no: int) -> dict:
        """Fetch + normalize a kuachi.gg cup stage bracket.

        Flow: list stages for the cup, pick the requested stage (by stage_no),
        fetch all matches for the cup, filter to that stage, then resolve
        signup ids -> player names. Returns the shared stages/groups schema.
        """
        stages = self._json_get(f"{self.API_KUACHI}/cup/{cup_id}/stages")
        if not stages:
            return {"source": "kuachi", "title": "", "stages": []}
        stage = next((s for s in stages if s.get("stage_no") == stage_no), stages[0])
        stage_id = stage.get("id")
        stage_title = stage.get("title") or f"Stage {stage_no + 1}"

        matches = self._json_get(f"{self.API_KUACHI}/cup/{cup_id}/matches")
        if not matches:
            return {"source": "kuachi", "title": stage_title, "stages": []}
        sm = [m for m in matches if m.get("cup_stage_id") == stage_id]
        if not sm:
            return {"source": "kuachi", "title": stage_title, "stages": []}

        signup_names = self._kuachi_signup_names(sm)
        groups = self._kuachi_groups(sm, signup_names)
        complete = all(m.get("is_scored") for m in sm)
        return {
            "source": "kuachi",
            "title": stage_title,
            "complete": complete,
            "stages": [{"name": stage_title, "groups": groups}],
        }

    def _kuachi_signup_names(self, stage_matches: list) -> dict:
        """Resolve signup id -> display name for the matches in a stage.

        Two batched API calls: cup_signups (signup -> player_id) then
        profile (player_id -> discord_username).
        """
        sig_ids = []
        seen = set()
        for m in stage_matches:
            for k in ("low_id", "high_id"):
                v = m.get(k)
                if v and v not in seen:
                    seen.add(v)
                    sig_ids.append(v)
        if not sig_ids:
            return {}
        signups = self._json_get(
            f"{self.API_KUACHI}/cup_signups/{','.join(sig_ids)}") or []
        pid_to_sig = {s.get("player_id"): s.get("id") for s in signups if s.get("player_id")}
        pids = [p for p in pid_to_sig if p]
        names = {}
        if pids:
            profiles = self._json_get(
                f"{self.API_KUACHI}/profile/{','.join(pids)}") or []
            names = {p.get("id"): (p.get("discord_username") or "") for p in profiles}
        return {sig: names.get(pid, "") for pid, sig in pid_to_sig.items()}

    @classmethod
    def _kuachi_groups(cls, stage_matches: list, names: dict) -> list:
        """Group kuachi stage matches into the shared stages/groups schema.

        Elimination matches carry elim_type (WB/LB/GF/GF2) + elim_round;
        group-stage matches carry group_no + group_round. We split by elim
        type (a "group") then by round, ordering winners -> losers -> final.
        """
        _KUACHI_SIDE_ORDER = {"WB": 0, "LB": 1, "GF": 2, "GF1": 2, "GF2": 3}
        _KUACHI_SIDE_NAME = {"WB": "Winners Bracket", "LB": "Losers Bracket", "GF": "Grand Final", "GF1": "Grand Final", "GF2": "Grand Final Reset"}
        by_side = {}
        by_group = {}
        for m in stage_matches:
            if m.get("elim_type"):
                by_side.setdefault(m.get("elim_type"), []).append(m)
            else:
                by_group.setdefault(m.get("group_no") or 0, []).append(m)

        groups = []
        # Elimination groups (winners -> losers -> grand final).
        for side in sorted(by_side, key=lambda s: _KUACHI_SIDE_ORDER.get(s, 9)):
            side_matches = by_side[side]
            by_round = {}
            for m in side_matches:
                by_round.setdefault(m.get("elim_round") or 0, []).append(m)
            rounds = []
            for rn in sorted(by_round):  # earlier rounds first; final (highest round) rightmost
                rms = sorted(by_round[rn], key=lambda m: m.get("elim_index") or 0)
                rounds.append({
                    "name": f"Round {rn + 1}",
                    "round": rn,
                    "matches": [cls._kuachi_match(m, names) for m in rms],
                })
            groups.append({"name": _KUACHI_SIDE_NAME.get(side, side), "rounds": rounds})

        # Group stage matches: one group per group_no.
        for gno in sorted(by_group):
            gm = by_group[gno]
            by_round = {}
            for m in gm:
                by_round.setdefault(m.get("group_round") or 0, []).append(m)
            rounds = []
            for rn in sorted(by_round):
                rms = sorted(by_round[rn], key=lambda m: m.get("id") or 0)
                rounds.append({
                    "name": f"Round {rn + 1}",
                    "round": rn,
                    "matches": [cls._kuachi_match(m, names) for m in rms],
                })
            groups.append({"name": f"Group {gno + 1}", "rounds": rounds})
        return groups

    @staticmethod
    def _kuachi_match(m: dict, names: dict) -> dict:
        """Normalize a single kuachi match to {p1, p2, score1, score2, winner}."""
        p1 = names.get(m.get("low_id") or "", "")
        p2 = names.get(m.get("high_id") or "", "")
        # Score = maps won per side from the per-map reports.
        s1 = s2 = None
        rep = m.get("low_report") or m.get("high_report")
        if rep and m.get("low_id") and m.get("high_id"):
            s1 = sum(1 for r in rep if r.get("low", 0) > r.get("high", 0))
            s2 = sum(1 for r in rep if r.get("high", 0) > r.get("low", 0))
        winner = None
        wid = m.get("winner_id")
        if wid:
            if m.get("low_id") == wid:
                winner = "p1"
            elif m.get("high_id") == wid:
                winner = "p2"
        return {"p1": p1, "p2": p2, "score1": s1, "score2": s2, "winner": winner}

    @classmethod
    def log_stats(cls):
        logger.info(
            f"brackets: {cls.fetched} fetched, {cls.skipped} skipped, "
            f"{cls.failed} failed, {cls.no_source} no source"
        )


def backfill(db, limit: int = 0, max_age_days: int = None):
    """One-time backfill: find tournaments with a bracket source that have no
    stored bracket (or whose bracket is older than max_age_days) and fetch them.

    Args:
        db: Database instance.
        limit: max tournaments to process this run (0 = unlimited).
        max_age_days: if set, only refetch brackets older than this.
    """
    import datetime
    f = BracketFetcher(db)
    rows = db.client.execute(
        "SELECT post_id, raw_html FROM raw_posts FINAL WHERE raw_html != ''"
    )
    todo = []
    cutoff = None
    if max_age_days:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
    for tid, html in rows:
        if not html:
            continue
        if not BracketFetcher.detect_source(html):
            continue
        # Already fetched recently?
        existing = db.get_tournament_bracket(tid)
        if existing:
            if cutoff is not None and existing["fetched_at"] and existing["fetched_at"] > cutoff:
                continue
        todo.append(tid)
    if limit:
        todo = todo[:limit]
    logger.info(f"backfill: {len(todo)} tournaments to process")
    done = 0
    for tid in todo:
        if f.fetch_for_tournament(tid):
            done += 1
    logger.info(f"backfill complete: {done}/{len(todo)} stored")
    return done
