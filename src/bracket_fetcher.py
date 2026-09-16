"""Bracket fetching + normalization for Toornament, shambler, EGB,
Challonge, Battlefy and start.gg.

PlusForward does not host bracket data — its tournament pages link out to an
external provider. This module detects which provider a tournament uses (from
the cached PlusForward raw_html), fetches the bracket from that provider's
public (no-auth) API, and normalizes it into a single source-agnostic JSON shape
suitable for rendering:

    {
      "source": "toornament" | "shambler" | "egb" | "challonge"
                | "battlefy" | "startgg",
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
  - Challonge:  archived pages via web.archive.org (the live site is
                Cloudflare-gated and the API needs a key, but every page
                embeds the full bracket state as JSON, and wayback has them)
  - Battlefy:   api.battlefy.com (GET, no auth; needs Origin/Referer headers)
  - start.gg:   api.start.gg GraphQL (needs STARTGG_API_TOKEN env var)

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

from bs4 import BeautifulSoup

from config import USER_AGENTS
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# These are external APIs, not PlusForward — separate, gentler rate limit.
BRACKET_RATE_LIMIT_DELAY = float(__import__("os").environ.get("BRACKET_RATE_LIMIT_DELAY", "0.4"))
BRACKET_HTTP_TIMEOUT = int(__import__("os").environ.get("BRACKET_HTTP_TIMEOUT", "15"))

# Wayback Machine fetches (Challonge archive) get their own slower rate
# limit and timeout — archive.org 429s aggressively on bursts and serves
# snapshots slowly. After a block page we cool down before the next attempt.
WAYBACK_RATE_LIMIT_DELAY = float(__import__("os").environ.get("WAYBACK_RATE_LIMIT_DELAY", "2.5"))
WAYBACK_HTTP_TIMEOUT = int(__import__("os").environ.get("WAYBACK_HTTP_TIMEOUT", "60"))
WAYBACK_BLOCK_COOLDOWN = float(__import__("os").environ.get("WAYBACK_BLOCK_COOLDOWN", "30"))

# Optional start.gg API token (free self-service at developer.start.gg).
# Without it start.gg/smash.gg tournaments are skipped, not crashed.
_STARTGG_API_TOKEN = (__import__("os").environ.get("STARTGG_API_TOKEN") or "").strip()

# Retry on non-JSON responses (intermittent Cloudflare challenge pages).
_JSON_RETRIES = int(__import__("os").environ.get("BRACKET_JSON_RETRIES", "4"))
_JSON_RETRY_DELAY = float(__import__("os").environ.get("BRACKET_JSON_RETRY_DELAY", "2.0"))

# Regexes to detect the provider + id from PlusForward raw_html.
_TOORNAMENT_RE = re.compile(
    r"play\.toornament\.com/[a-z_]+/tournaments/(\d+)", re.IGNORECASE)
_SHAMBLER_RE = re.compile(
    r"shambler\.site/(?:[a-z0-9_-]+/)*brackets\.php\?cup=(\d+)", re.IGNORECASE)
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

# Challonge links: [<subdomain>.]challonge.com/<slug>
# (e.g. 125fps.challonge.com/sundaycup21). The live site is Cloudflare-gated
# and the API needs a key, but archived pages embed the full bracket state.
_CHALLONGE_RE = re.compile(
    r"(?<![a-z0-9-])((?:[a-z0-9-]+\.)*challonge\.com)/([a-z0-9_-]+)",
    re.IGNORECASE)

# Battlefy tournament pages: battlefy.com/<org>/<slug>/<24-hex-id>/...
_BATTLEFY_RE = re.compile(
    r"(?<![a-z0-9-])(?:www\.)?battlefy\.com/[a-z0-9_-]+/[a-z0-9_-]+/([0-9a-f]{24})",
    re.IGNORECASE)

# start.gg / smash.gg tournament pages: /tournament/<slug>[/...]
_STARTGG_RE = re.compile(
    r"(?<![a-z0-9-])(?:smash\.gg|start\.gg)/tournament/([a-z0-9-]+)",
    re.IGNORECASE)

# Round-title patterns for PlusForward-native brackets (double elimination).
# Winners-bracket rounds are any non-loser, non-grand-final round (e.g.
# Quarterfinals / Semifinals / Winner's Final); loser rounds start with
# "Loser"; the grand final is matched explicitly.
_PF_WINNERS_RE = re.compile(r"^(?!loser|grand)[a-z'\s-]+$", re.IGNORECASE)
_PF_LOSERS_RE = re.compile(r"^loser", re.IGNORECASE)


def _pf_round_group(title: str) -> str:
    """Classify a PlusForward-native round title into winners/losers/grand.

    Round titles vary across eras: 'WB R1'/'LB Semi' (newer, contains digits),
    "Winner's Final"/"Loser's Round 1" (older), plain 'Quarterfinals' for
    single-elim. The old charset-only regexes silently DROPPED any title with
    digits (every 'R1' round vanished from stored brackets) and mis-filed
    'LB ...' rounds into the winners group (only '^loser|grand' excluded).
    Classify by prefix instead; unknown titles default to the winners group
    (legacy behavior for plain single-elim rounds).
    """
    t = title.strip().lower()
    if "grand" in t:
        return "grand"
    if t.startswith(("loser", "lower", "lb")):
        return "losers"
    return "winners"


# Consolidation / placement matches (3rd place decider, 5th-8th placement,
# "Consolidation Final") share the final round column on PF-native brackets,
# separated only by a bracket-match-title header. They must NOT merge into the
# elimination tree: a 2-match "Final" round defeats the shrinking-rounds
# geometry (rendered flat, no connectors) and mislabels the decider as part
# of the bracket proper.
_PF_CONSOLIDATION_RE = re.compile(
    r"(?:\d+(?:st|nd|rd|th)\s*place|consolidation|third\s*place)", re.IGNORECASE)

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
        self._last_wayback = 0.0
        self._wayback_blocked_until = 0.0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def fetch_for_tournament_if_needed(self, tournament_id: int, max_age_days: int = None,
                                       force: bool = False) -> bool:
        """Fetch + store a bracket only if it's missing or stale.

        Cheap fast-path: a fresh, non-empty stored bracket returns False
        immediately (no network, no tournament-page read). A tournament with
        no bracket source in its cached HTML still goes through
        fetch_for_tournament, which falls back to the dynamic AJAX probe.

        Args:
            tournament_id: PlusForward tournament id.
            max_age_days: refetch if the stored bracket is older than this.
                None = only fetch when missing.
            force: if True, always re-fetch + re-store even if a fresh bracket
                exists (used to refresh brackets for in-progress events).
        """
        # Cheap fast-path first: a fresh, non-empty stored bracket means there
        # is nothing to do. Checking it before reading the cached tournament
        # page matters now that the parse path calls this for every match —
        # that read pulls the whole (50-100 KB) tournament HTML out of
        # ClickHouse per match, all to immediately conclude "already stored".
        if not force:
            existing = self._db.get_tournament_bracket(tournament_id)
            if existing and existing.get("data") and self._has_matches(existing["data"]):
                if max_age_days is None:
                    return False  # already stored, fresh enough
                fa = existing.get("fetched_at")
                if fa and (datetime.datetime.utcnow() - fa).days < max_age_days:
                    return False
        raw_html = self._db.get_tournament_html(tournament_id)
        if not raw_html or not self.detect_source(raw_html):
            # No bracket link in the static HTML — but it may be loaded
            # dynamically ("Groups / Brackets" tab). Let fetch_for_tournament
            # decide via the AJAX fallback rather than returning False here.
            return self.fetch_for_tournament(tournament_id) if raw_html else False
        # Reaching here means either forced, nothing stored/fresh, or an empty
        # stored bracket (no match data — a transient API miss that was
        # persisted; the fast-path above deliberately skips those, so they
        # always retry). All of them fetch.
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
        """Detect provider, fetch raw payload, normalize, and store in the DB.

        The raw provider payload is persisted in raw_brackets before
        normalization, so parsed brackets stay re-derivable offline (the
        bracket-side equivalent of caching match HTML in raw_posts).

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

        kind, ref = source  # ('toornament', tid), ('shambler', cup), ('egb', slug), ('kuachi', (cup_id, stage_no)), ('plusforward', tournament_id), ('challonge', (host, slug)), ('battlefy', tid), ('startgg', slug)
        try:
            payload = self._fetch_payload(kind, ref)
            # Cache the raw payload BEFORE normalizing: even if normalization
            # or the parsed store fails later, the provider's answer survived.
            if payload is not None:
                self._db.upsert_raw_bracket(
                    tournament_id, kind,
                    json.dumps(payload, ensure_ascii=False),
                )
            normalized = self._normalize_payload(kind, payload or {})
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

    # ------------------------------------------------------------------
    # Raw payload layer: fetch (network) vs normalize (pure, offline).
    # Each provider is split into _fetch_payload (gathers raw provider data
    # into a JSON-safe payload) and _normalize_payload (payload -> shared
    # bracket schema, no network). Successful payloads are persisted in the
    # raw_brackets table so brackets survive `reset.py parsed` wipes and can
    # be replayed offline after parsing changes.

    def _fetch_payload(self, kind: str, ref) -> dict | None:
        """Fetch one provider's raw data into a JSON-safe payload (network).

        Returns None only when the provider gives nothing usable at all.
        Must never be called on the rebuild path.
        """
        if kind == "toornament":
            return self._toornament_payload(ref)
        if kind == "shambler":
            return self._shambler_payload(ref)
        if kind == "egb":
            return self._egb_payload(ref)
        if kind == "kuachi":
            return self._kuachi_payload(ref)
        if kind == "challonge":
            return self._challonge_payload(ref)
        if kind == "battlefy":
            return self._battlefy_payload(ref)
        if kind == "startgg":
            return self._startgg_payload(ref)
        if kind == "plusforward":
            return self._pf_native_payload(ref)
        raise ValueError(f"unknown bracket source: {kind}")

    def _normalize_payload(self, kind: str, payload: dict) -> dict:
        """Payload -> shared bracket schema. Pure: no network, no DB."""
        if kind == "toornament":
            return self._toornament_normalize(payload)
        if kind == "shambler":
            return self._shambler_normalize(payload)
        if kind == "egb":
            return self._egb_normalize(payload.get("meta") or {}, payload.get("bracket") or {})
        if kind == "kuachi":
            return self._kuachi_normalize(payload)
        if kind == "challonge":
            return self._challonge_normalize(payload)
        if kind == "battlefy":
            return self._battlefy_normalize(payload)
        if kind == "startgg":
            return self._startgg_normalize(payload)
        if kind == "plusforward":
            return self._pf_native_normalize(payload)
        raise ValueError(f"unknown bracket source: {kind}")

    def rebuild_from_cache(self, tournament_id: int, source: str, payload_json: str) -> bool:
        """Re-derive one tournament's parsed bracket from its raw cached payload.

        Pure-offline counterpart of fetch_for_tournament: same normalization,
        same empty-bracket guards, no network. Returns True when the parsed
        bracket was (re)stored.
        """
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            logger.warning(f"bracket rebuild {tournament_id}: unparseable cached payload")
            return False
        try:
            normalized = self._normalize_payload(source, payload)
        except Exception as e:
            logger.warning(f"bracket rebuild failed for {tournament_id} ({source}): {e}")
            return False
        if not normalized or not normalized.get("stages"):
            return False
        if not self._has_matches(normalized):
            return False
        self._db.upsert_tournament_bracket(
            tournament_id, normalized.get("source", source),
            json.dumps(normalized, ensure_ascii=False),
        )
        return True

    # ------------------------------------------------------------------
    # Toornament
    # ------------------------------------------------------------------

    def _toornament_payload(self, tournament_id: int) -> dict:
        """Gather a Toornament bracket payload: trimmed stages + raw matches +
        group-name fallback resolution (the /groups endpoint probe moves here
        from normalize so rebuilds stay offline)."""
        stages = self._toornament_stages(tournament_id)
        if not stages:
            return {"stages": [], "matches": [], "group_names": {}}
        matches = self._toornament_matches(tournament_id)
        # /groups fallback probe (per stage): group-name coverage for any
        # group the matches' own group objects don't name. Same calls the
        # old normalize path made, just moved to payload-gathering time.
        gname_extra = {}
        for st in stages:
            d = self._json_get(f"{self.API_TOORNAMENT}/groups",
                               {"stage_ids": st["id"], "offset": 0, "limit": _API_LIMIT})
            if d:
                for g in d.get("items", []):
                    gname_extra.setdefault(g["id"], g.get("name", ""))
        # Project raw match items down to the keys normalization consumes —
        # raw API items are 5-10x larger (metadata/opponent profiles we
        # never read), and raw_brackets stores one row per tournament.
        projected = []
        for m in matches:
            opps = []
            for o in (m.get("opponents") or []):
                part = o.get("participant") or {}
                opps.append({"participant": {"name": part.get("name", "")},
                             "score": o.get("score"),
                             "result": o.get("result")})
            projected.append({
                "stage": {"id": (m.get("stage") or {}).get("id")},
                "group": {"id": (m.get("group") or {}).get("id"),
                          "name": (m.get("group") or {}).get("name", "")},
                "round": {"number": (m.get("round") or {}).get("number", 0),
                          "name": (m.get("round") or {}).get("name", "")},
                "opponents": opps,
            })
        return {"stages": stages, "matches": projected,
                "group_names": {str(k): v for k, v in gname_extra.items()}}

    def _toornament_normalize(self, payload: dict) -> dict:
        stages = payload.get("stages") or []
        matches = payload.get("matches") or []
        gname_extra = payload.get("group_names") or {}
        if not stages:
            return {"source": "toornament", "title": "", "stages": []}

        # Gather all matches for the tournament once (they carry stage/group/round refs).
        matches_by_stage = {}
        for m in matches:
            sid = m.get("stage", {}).get("id")
            matches_by_stage.setdefault(sid, []).append(m)

        result_stages = []
        for st in stages:
            sid = st["id"]
            sm = matches_by_stage.get(sid, [])
            groups = self._toornament_groups(sid, sm, gname_extra)
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
        m = _CHALLONGE_RE.search(raw_html)
        if m:
            return ("challonge", (m.group(1).lower(), m.group(2)))
        m = _BATTLEFY_RE.search(raw_html)
        if m:
            return ("battlefy", m.group(1))
        m = _STARTGG_RE.search(raw_html)
        if m:
            return ("startgg", m.group(1))
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
        src = self.detect_source(body)
        if src:
            return src
        # No external provider link — but PlusForward itself may render the
        # bracket natively (the AJAX response is bracket HTML, not a link).
        if '<div class="bracket"' in body:
            return ("plusforward", tournament_id)
        return None

    # ------------------------------------------------------------------
    # HTTP (JSON) helpers — external APIs, curl-based like PageFetcher
    # ------------------------------------------------------------------

    def _json_get(self, url: str, params: dict = None, headers: list = None) -> Optional[dict]:
        """GET a JSON API endpoint and return parsed JSON (or None).

        Retries on non-JSON responses (e.g. intermittent Cloudflare challenge
        pages) so a single blocked request doesn't yield an empty bracket.
        """
        if params:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        for attempt in range(_JSON_RETRIES):
            body = self._curl("GET", url, attempt=attempt, headers=headers)
            if body is None:
                continue
            try:
                return json.loads(body)
            except Exception as e:
                logger.debug(f"bad JSON from {url} (attempt {attempt + 1}): {e}")
                time.sleep(_JSON_RETRY_DELAY + random.uniform(0, 0.3))
        return None

    def _json_post(self, url: str, data: dict, headers: list = None) -> Optional[dict]:
        """POST form-encoded data to a JSON API endpoint (with retry)."""
        for attempt in range(_JSON_RETRIES):
            body = self._curl("POST", url, data=data, attempt=attempt, headers=headers)
            if body is None:
                continue
            try:
                return json.loads(body)
            except Exception as e:
                logger.debug(f"bad JSON from {url} (attempt {attempt + 1}): {e}")
                time.sleep(_JSON_RETRY_DELAY + random.uniform(0, 0.3))
        return None

    def _curl(self, method: str, url: str, data: dict = None, attempt: int = 0,
              headers: list = None) -> Optional[str]:
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
        if headers:
            cmd += headers
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

    # (_fetch_toornament was split into _toornament_payload + _toornament_normalize;
    # both live near the raw-payload layer above.)

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

    def _toornament_groups(self, stage_id: int, matches: list[dict],
                           gname_extra: dict | None = None) -> list[dict]:
        """Group matches by group (Winners/Losers/Grand Final), then by round.

        Group names are taken from the matches themselves (always present).
        `gname_extra` (persisted in the raw payload from the fetch-time
        /groups probe) is the fallback - keeps normalize runnable offline.
        """
        # Primary: group names from the matches' own group objects.
        gname = {}
        for m in matches:
            g = m.get("group", {})
            gid = g.get("id")
            if gid and gid not in gname and g.get("name"):
                gname[gid] = g["name"]
        # Fallback: /groups-endpoint names resolved at fetch time (pure here);
        # JSON round-trips dict keys to strings, so normalize back to int.
        for gid, name in (gname_extra or {}).items():
            try:
                gname.setdefault(int(gid), name)
            except (TypeError, ValueError):
                gname.setdefault(gid, name)

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

    def _shambler_payload(self, cup_id: int) -> dict:
        """Fetch a shambler bracket payload (POST data-brackets.php, network)."""
        d = self._json_post(self.API_SHAMBLER, {"cup": cup_id, "update": 0})
        if not d:
            return {"response": {}}
        return {"response": d}

    def _shambler_normalize(self, payload: dict) -> dict:
        d = payload.get("response") or {}
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

    def _egb_payload(self, slug: str) -> dict | None:
        """Fetch an EGB bracket payload (network): meta + bracket graph."""
        meta = self._json_get(f"{self.API_EGB}/tournaments/by-slug/{slug}")
        if not meta or not meta.get("id"):
            return None
        tid = meta["id"]
        bracket = self._json_get(f"{self.API_EGB}/tournaments/{tid}/bracket") or {}
        return {"meta": meta, "bracket": bracket}

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

    def _kuachi_payload(self, ref) -> dict:
        """Fetch a kuachi.gg stage payload (network).

        Matches are filtered to the requested stage and signup names are
        resolved at fetch time (two API calls), so normalize stays offline.
        """
        cup_id, stage_no = ref
        stages = self._json_get(f"{self.API_KUACHI}/cup/{cup_id}/stages") or []
        stage = next((s for s in stages if s.get("stage_no") == stage_no), stages[0] if stages else None)
        stage_title = (stage.get("title") if stage else None) or f"Stage {stage_no + 1}"
        matches = self._json_get(f"{self.API_KUACHI}/cup/{cup_id}/matches") or []
        sm = [m for m in matches if stage and m.get("cup_stage_id") == stage.get("id")]
        if not sm:
            return {"stage_title": stage_title, "stage_matches": [], "names": {}}
        names = self._kuachi_signup_names(sm)
        return {"stage_title": stage_title, "stage_matches": sm, "names": names}

    def _kuachi_normalize(self, payload: dict) -> dict:
        stage_title = payload.get("stage_title") or ""
        sm = payload.get("stage_matches") or []
        if not sm:
            return {"source": "kuachi", "title": stage_title, "stages": []}
        signup_names = payload.get("names") or {}
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
            # A real bracket match needs both sides. Matches with a missing
            # side (e.g. an un-played double-elimination reset placeholder
            # with only one player and no score) are skipped.
            if not m.get("low_id") or not m.get("high_id"):
                continue
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

    # ------------------------------------------------------------------
    # Challonge (archived pages via the Wayback Machine)
    # ------------------------------------------------------------------
    #
    # The live challonge.com is Cloudflare-gated and its API needs a key, but
    # every Challonge bracket page embeds the full bracket state in the
    # TournamentStore JSON. archive.org archived these pages (many with the
    # bracket already complete), so we fetch the nearest snapshot of the
    # bracket page and extract the embedded JSON — no auth, no API key.
    #
    # The Wayback "web/2/" shorthand redirects to the closest snapshot to the
    # present for the given URL (or 404 if none exists).
    WAYBACK_BASE = "https://web.archive.org/web/2/"

    @staticmethod
    def _challonge_bracket_url(ref) -> str:
        """Challonge bracket-page URL from a (host, slug) detection ref."""
        host, slug = ref
        return f"https://{host}/{slug}"

    def _challonge_payload(self, ref) -> dict | None:
        """Fetch the archived Challonge bracket page and pull its state.

        The embedded JSON is the raw bracket state (players, rounds, matches,
        scores, winners) — exactly what normalize consumes, so no further
        network calls are needed and rebuilds stay offline.
        """
        src_url = self._challonge_bracket_url(ref)
        body = self._curl_wayback(f"{self.WAYBACK_BASE}{src_url}")
        if not body:
            return None
        i = body.find("_initialStoreState['TournamentStore']")
        if i < 0:
            logger.debug(f"challonge {src_url}: no TournamentStore JSON in archived page")
            return None
        j = body.find("{", i)
        if j < 0:
            return None
        try:
            store, _ = json.JSONDecoder().raw_decode(body[j:])
        except Exception as e:
            logger.debug(f"challonge {src_url}: bad embedded JSON: {e}")
            return None
        # Sanity gate: a real Challenge bracket has tournaments + rounds/matches
        # data (the store shape lives on the bracket page itself).
        if not isinstance(store, dict) or not store.get("tournament"):
            return None
        return {"source_url": src_url, "store": store}

    def _curl_wayback(self, url: str) -> Optional[str]:
        """Rate-limited GET for web.archive.org (it 429s on bursts).

        Wayback can serve an anti-bot block page to some UAs, so on a block
        (tiny body / HTML but no wayback banner) we retry with the next UA.
        """
        now = time.time()
        if now < self._wayback_blocked_until:
            # IP-level block: wait out the cooldown before trying again.
            time.sleep(self._wayback_blocked_until - now + random.uniform(0.1, 0.5))
        elapsed = time.time() - self._last_wayback
        if elapsed < WAYBACK_RATE_LIMIT_DELAY:
            time.sleep(WAYBACK_RATE_LIMIT_DELAY - elapsed + random.uniform(0.1, 0.4))
        self._last_wayback = time.time()
        for attempt, ua in enumerate(USER_AGENTS):
            cmd = [
                "curl", "-s", "-L", "--compressed",
                "--connect-timeout", str(WAYBACK_HTTP_TIMEOUT),
                "--max-time", str(WAYBACK_HTTP_TIMEOUT),
                "-A", ua,
                "-H", "Accept: text/html",
            ]
            cmd.append(url)
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=WAYBACK_HTTP_TIMEOUT + 5)
                body = result.stdout.decode("utf-8", errors="replace")
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.debug(f"wayback curl failed for {url}: {e}")
                body = ""
            if not body:
                continue
            if result.returncode in (0, 28) and "_initialStoreState['TournamentStore']" in body:
                return body
            if attempt < len(USER_AGENTS) - 1 and len(body) < 2000:
                # Block page ("abusive bot traffic") or 429 — try next UA.
                time.sleep(WAYBACK_RATE_LIMIT_DELAY)
                continue
            if "_initialStoreState['TournamentStore']" in body:
                return body
        # All UAs blocked (or no snapshot): cool down so a sustained backfill
        # doesn't keep hammering archive.org.
        self._wayback_blocked_until = time.time() + WAYBACK_BLOCK_COOLDOWN
        return None

    @staticmethod
    def _challonge_normalize(payload: dict) -> dict:
        """Normalize an archived Challonge TournamentStore (pure, offline).

        Challonge's own round titles are accurate ("WB Semi-finals (bo3)",
        "Grand-finals", ...) and its winners bracket already includes the
        grand final as the last positive round, so we keep the source titles
        (marking the groups "named" so the display pipeline doesn't
        positionally rename them) and do NOT create a separate Grand Final
        group — that would duplicate the final round.
        """
        store = payload.get("store") or {}
        mbr = store.get("matches_by_round") or {}
        if not mbr:
            return {"source": "challonge", "title": "", "stages": []}
        # Round titles from the store's rounds list (e.g. "WB Semi-finals
        # (bo3)"); the map name follows on a second line and is dropped.
        titles = {}
        for r in store.get("rounds") or []:
            try:
                rn = int(r.get("number"))
            except (TypeError, ValueError):
                continue
            title = (r.get("title") or "").strip().split("\n")[0].strip()
            titles[rn] = title
        rounds = {}
        for rk, ms in mbr.items():
            try:
                rn = int(rk)
            except (TypeError, ValueError):
                continue
            rounds.setdefault(rn, []).extend(ms or [])
        if not rounds:
            return {"source": "challonge", "title": "", "stages": []}

        def _rname(rn: int) -> str:
            return titles.get(rn) or f"Round {abs(rn)}"

        groups = []
        for side in ("winners", "losers"):
            rs = [r for r in rounds if (r > 0) == (side == "winners")]
            if not rs:
                continue
            rs = sorted(rs, key=lambda r: abs(r))
            groups.append({
                "name": "Winners Bracket" if side == "winners" else "Losers Bracket",
                "named": True,  # keep Challonge's own round titles
                "rounds": [
                    {"name": _rname(r), "round": i,
                     "matches": [BracketFetcher._challonge_match(m) for m in rounds[r]]}
                    for i, r in enumerate(rs)
                ],
            })
        # Optional third-place decider (a separate match object in the store).
        tpm = store.get("third_place_match")
        if isinstance(tpm, dict) and (tpm.get("player1") or tpm.get("player2")):
            groups.append({
                "name": "Third place",
                "named": True,
                "rounds": [{"name": "Third place", "round": 0,
                            "matches": [BracketFetcher._challonge_match(tpm)]}],
            })
        complete = all(
            m.get("winner") or not (m.get("p1") and m.get("p2"))
            for g in groups for r in g["rounds"] for m in r["matches"]
        )
        return {
            "source": "challonge",
            "title": "",
            "complete": complete,
            "stages": [{"name": "", "groups": groups}],
        }

    @staticmethod
    def _challonge_match(m: dict) -> dict:
        """Normalize a Challonge match to {p1, p2, score1, score2, winner}."""
        p1 = ((m.get("player1") or {}).get("display_name") or "").strip()
        p2 = ((m.get("player2") or {}).get("display_name") or "").strip()
        scores = m.get("scores") or []
        s1 = scores[0] if len(scores) > 0 else None
        s2 = scores[1] if len(scores) > 1 else None
        winner = None
        wid = m.get("winner_id")
        p1id = (m.get("player1") or {}).get("id")
        p2id = (m.get("player2") or {}).get("id")
        if wid is not None and wid == p1id:
            winner = "p1"
        elif wid is not None and wid == p2id:
            winner = "p2"
        # Bye placeholders ("player2" empty) carry no winner; the generic
        # complete check treats those as non-blocking.
        return {"p1": p1, "p2": p2, "score1": s1, "score2": s2, "winner": winner}

    # ------------------------------------------------------------------
    # Battlefy (api.battlefy.com)
    # ------------------------------------------------------------------
    #
    # Battlefy's API needs no key but rejects requests without browser-like
    # Origin/Referer headers (403 "Direct API access is not permitted").
    # The matches endpoint serves the full bracket: winner/loser/final
    # matches with team names + scores, linked via matchNumber/next.
    API_BATTLEFY = "https://api.battlefy.com"
    _BF_HEADERS = [
        "-H", "Origin: https://battlefy.com",
        "-H", "Referer: https://battlefy.com/",
    ]

    def _battlefy_payload(self, tournament_hex_id: str) -> dict | None:
        """Fetch a Battlefy tournament's bracket payload (network).

        Two calls: tournament (stage ids) then stage matches. Both raw
        payloads are kept so rebuilds stay offline (normalize below is pure).
        """
        d = self._json_get(
            f"{self.API_BATTLEFY}/tournaments/{tournament_hex_id}",
            headers=self._BF_HEADERS)
        if not d or not isinstance(d, dict):
            return None
        stage_ids = d.get("stageIDs") or []
        if not stage_ids:
            return {"tournament": d, "stage": None, "matches": []}
        stage = self._json_get(
            f"{self.API_BATTLEFY}/stages/{stage_ids[0]}",
            headers=self._BF_HEADERS)
        matches = self._json_get(
            f"{self.API_BATTLEFY}/stages/{stage_ids[0]}/matches",
            headers=self._BF_HEADERS) or []
        return {
            "tournament": d,
            "stage": stage,
            "matches": matches,
            "name": (d.get("name") or ""),
        }

    @staticmethod
    def _battlefy_normalize(payload: dict) -> dict:
        """Normalize a Battlefy payload into the shared schema (pure).

        Battlefy matchTypes: winner / loser / final; roundNumber is 1-based
        within the winners and losers sides (0 = third-place decider), and
        the 'final' matches (roundNumber 1..) end the bracket.
        """
        ms = payload.get("matches") or []
        if not ms:
            return {"source": "battlefy", "title": "", "stages": []}
        w, l, f, third = {}, {}, [], []
        for m in ms:
            nm = BracketFetcher._battlefy_match(m)
            if nm.get("_bye"):
                continue
            t = nm.get("_t")
            rn = m.get("roundNumber")
            if t == "winner":
                w.setdefault(rn, []).append(nm)
            elif t == "loser" and rn == 0:
                # roundNumber 0 = third-place decider, not a losers-bracket round
                third.append(nm)
            elif t == "loser":
                l.setdefault(rn, []).append(nm)
            else:
                f.append(nm)

        def _mk(name, by_round):
            if not by_round:
                return None
            return {"name": name, "rounds": [
                {"name": f"Round {rn}", "round": i,
                 "matches": by_round[rn]}
                for i, rn in enumerate(sorted(by_round))
            ]}

        groups = [g for g in (
            _mk("Winners Bracket", w),
            _mk("Losers Bracket", l),
            (_mk("Grand Final", {1: f}) if f else None),
        ) if g]
        if third:
            groups.append({"name": "Third place", "rounds": [
                {"name": "Third place", "round": 0, "matches": third}]})
        complete = all(
            m.get("winner") or not (m.get("p1") and m.get("p2"))
            for g in groups for r in g["rounds"] for m in r["matches"]
        )
        return {
            "source": "battlefy",
            "title": payload.get("name") or "",
            "complete": complete,
            "stages": [{"name": "", "groups": groups}],
        }

    @staticmethod
    def _battlefy_match(m: dict) -> dict:
        """Normalize one Battlefy match to {p1, p2, score1, score2, winner}.

        Keeps the source matchType in "_t" (stripped by _battlefy_normalize's
        grouping) so the bracket-side grouping can split winners/losers/final
        exactly like the other providers' group structure.
        """
        t = (m.get("top") or {}).get("team") or {}
        b = (m.get("bottom") or {}).get("team") or {}
        p1 = (t.get("name") or "").strip()
        p2 = (b.get("name") or "").strip()
        # Byes: an empty side (no team) is a placeholder, not a real match.
        if not p1 or not p2:
            return {"p1": p1, "p2": p2, "score1": None, "score2": None,
                    "winner": None, "_t": m.get("matchType"), "_bye": True}
        s1 = (m.get("top") or {}).get("score")
        s2 = (m.get("bottom") or {}).get("score")
        if s1 is None or s2 is None:
            s1 = s2 = None
        winner = None
        if (m.get("top") or {}).get("winner"):
            winner = "p1"
        elif (m.get("bottom") or {}).get("winner"):
            winner = "p2"
        return {"p1": p1, "p2": p2, "score1": s1, "score2": s2,
                "winner": winner, "_t": m.get("matchType")}

    # ------------------------------------------------------------------
    # start.gg / smash.gg (api.start.gg GraphQL)
    # ------------------------------------------------------------------
    #
    # The legacy REST API (api.smash.gg/*, expand=...) is dead (503/403),
    # and the GraphQL API requires a token. When STARTGG_API_TOKEN is set we
    # query the phase/set structure the same way the site does; without a
    # token the source simply isn't fetchable and we skip (never crash).
    API_STARTGG = "https://api.start.gg/gql/alpha"

    def _startgg_payload(self, slug: str) -> dict | None:
        """Fetch a start.gg tournament bracket via GraphQL (network).

        Requires STARTGG_API_TOKEN (free self-service token). Queries the
        tournament's events -> phases -> sets with entrant/score data.
        """
        if not _STARTGG_API_TOKEN:
            logger.debug(f"startgg {slug}: STARTGG_API_TOKEN not set, skipping")
            return None
        query = """query Q($slug: String!) {
          tournament(slug: $slug) {
            id
            name
            events {
              id
              name
              phaseGroups {
                id
                phase { id name }
                sets {
                  id
                  round
                  identifier
                  totalGames
                  state
                  slots {
                    entrant { name }
                    standing { placement }
                  }
                }
              }
            }
          }
        }"""
        body = self._json_post(
            self.API_STARTGG, {"query": query, "variables": {"slug": slug}},
            headers=["-H", f"Authorization: Bearer {_STARTGG_API_TOKEN}",
                     "-H", "Content-Type: application/json"])
        if not body:
            return None
        data = body.get("data") or {}
        tourn = data.get("tournament") or {}
        if not tourn:
            return None
        return {"tournament": tourn}

    @staticmethod
    def _startgg_normalize(payload: dict) -> dict:
        """Normalize a start.gg GraphQL payload into the shared schema (pure).

        Sets carry a signed `round` (positive = winners, negative = losers,
        matching the site's bracket display) and slots with entrant names +
        standing (1 = winner). Group by the phaseGroup's phase name when
        available, else by round sign.
        """
        tourn = payload.get("tournament") or {}
        events = tourn.get("events") or []
        groups = []
        for ev in events:
            for pg in ev.get("phaseGroups") or []:
                phase = pg.get("phase") or {}
                pname = phase.get("name") or ""
                sets = pg.get("sets") or []
                if not sets:
                    continue
                by_round = {}
                for s in sets:
                    rn = s.get("round")
                    by_round.setdefault(rn, []).append(s)
                rounds = []
                for rn in sorted(by_round, key=lambda r: abs(r or 0)):
                    rounds.append({
                        "name": f"Round {abs(rn or 0)}",
                        "round": rn or 0,
                        "matches": [BracketFetcher._startgg_match(s) for s in by_round[rn]],
                    })
                # Group label: prefer the phase name; fall back to Winners/
                # Losers by the sign of the first round.
                sign = 1
                if by_round:
                    first = min(by_round, key=lambda r: abs(r or 0))
                    sign = 1 if (first or 0) >= 0 else -1
                gname = pname or ("Winners Bracket" if sign > 0 else "Losers Bracket")
                groups.append({"name": gname, "rounds": rounds})
        if not groups:
            return {"source": "startgg", "title": "", "stages": []}
        complete = all(
            m.get("winner") or not (m.get("p1") and m.get("p2"))
            for g in groups for r in g["rounds"] for m in r["matches"]
        )
        return {
            "source": "startgg",
            "title": tourn.get("name") or "",
            "complete": complete,
            "stages": [{"name": "", "groups": groups}],
        }

    @staticmethod
    def _startgg_match(s: dict) -> dict:
        """Normalize one start.gg set to {p1, p2, score1, score2, winner}."""
        slots = s.get("slots") or []
        p1 = p2 = ""
        s1 = s2 = None
        winner = None
        if len(slots) >= 1 and slots[0].get("entrant"):
            p1 = (slots[0]["entrant"].get("name") or "").strip()
        if len(slots) >= 2 and slots[1].get("entrant"):
            p2 = (slots[1]["entrant"].get("name") or "").strip()
        # standing.placement 1 marks the winner; scores are derived from the
        # set's completed games (not carried in this query), so they stay
        # None unless present in the payload.
        if slots and slots[0].get("standing", {}).get("placement") == 1:
            winner = "p1"
        elif len(slots) > 1 and slots[1].get("standing", {}).get("placement") == 1:
            winner = "p2"
        score1 = s.get("score1")
        score2 = s.get("score2")
        if isinstance(score1, int) and isinstance(score2, int):
            s1, s2 = score1, score2
        return {"p1": p1, "p2": p2, "score1": s1, "score2": s2, "winner": winner}

    # ------------------------------------------------------------------
    # PlusForward-native brackets (rendered by plusforward.net itself)
    # ------------------------------------------------------------------
    def _pf_native_payload(self, tournament_id: int) -> dict | None:
        """Fetch a PlusForward-native bracket payload (AJAX HTML, network)."""
        body = self._curl(
            "GET",
            f"{_AJAX_BRACKETS_URL}?tourneybrackets=1&pid={tournament_id}",
        )
        if not body:
            return None
        return {"html": body}

    def _pf_native_normalize(self, payload: dict) -> dict:
        """Normalize a PlusForward-native AJAX HTML payload (pure)."""
        body = payload.get("html") or ""
        if not body:
            return {"source": "plusforward", "title": "", "stages": []}
        rounds = self._parse_pf_native_rounds(body)
        if not rounds:
            return {"source": "plusforward", "title": "", "stages": []}

        # Group round titles into winners / losers / grand-final groups.
        # Winner's Final + Grand Final are treated as their own single-match
        # groups; loser rounds ("LB ...", "Loser's ...") form the losers group.
        # Consolidation deciders (3rd place etc., marked by the parser) are
        # pulled out into their own "Third place" group so they don't bloat
        # the elimination tree's round counts (the connector-geometry check
        # treats equal round counts as round-robin and drops connectors).
        groups = []
        remaining, third = [], []
        for r in rounds:
            (third if r.pop("consolidation", False) else remaining).append(r)
        rounds = remaining
        winners = [r for r in rounds if _pf_round_group(r["title"]) == "winners"]
        losers = [r for r in rounds if _pf_round_group(r["title"]) == "losers"]
        grand = [r for r in rounds if _pf_round_group(r["title"]) == "grand"]

        def _mk_group(name, round_list):
            return {
                "name": name,
                "rounds": [
                    {"name": r["title"], "round": i, "matches": r["matches"]}
                    for i, r in enumerate(round_list)
                ],
            }

        if winners:
            groups.append(_mk_group("Winners Bracket", winners))
        if losers:
            groups.append(_mk_group("Losers Bracket", losers))
        for g in grand:
            groups.append({
                "name": "Grand Final",
                "rounds": [{"name": g["title"], "round": 0, "matches": g["matches"]}],
            })
        if third:
            groups.append({
                "name": "Third place",
                "rounds": [
                    {"name": r["title"], "round": i, "matches": r["matches"]}
                    for i, r in enumerate(third)
                ],
            })
        # complete = every real match in the bracket has a decided winner. TBD
        # placeholders (pending rounds of a live event) have winner=None, so a
        # still-running tournament renders as Live, not Finished — it used to
        # be hardcoded True, which mislabelled every live native bracket.
        # Empty placeholder cells (byes, unfilled slots) have no players at
        # all and by themselves don't make an otherwise finished bracket
        # "Live" — only a p1+p2 match without a winner does.
        complete = bool(rounds) and all(
            m.get("winner") or not (m.get("p1") and m.get("p2"))
            for grp in groups for r_ in grp["rounds"] for m in r_["matches"]
        )
        return {
            "source": "plusforward",
            "title": "",
            "complete": complete,
            "stages": [{"name": "", "groups": groups}],
        }

    @staticmethod
    def _parse_pf_native_rounds(body: str) -> list:
        """Parse PlusForward-native bracket HTML into a list of round dicts.

        Returns [{title, matches: [{p1, p2, score1, score2, winner}],
        consolidation: bool}]. Columns that only contain spacing/connectors
        (no bracket-match) are skipped. A column may contain several segments:
        the main round plus optional sub-headed sections (a bracket-match-title
        header, e.g. a '3rd Place Match' decider) — each becomes its own round
        dict, with consolidation segments flagged for separate grouping.
        """
        try:
            soup = BeautifulSoup(body, "html.parser")
        except Exception:
            return []
        bracket = soup.find("div", class_="bracket")
        if not bracket:
            return []
        rounds = []
        for col in bracket.find_all("div", class_="bracket-column"):
            title_el = col.find("div", class_="bracket-round-title")
            title = title_el.get_text(strip=True) if title_el else ""
            # Segments: main round + optional sub-headed sections. A
            # bracket-match-title header splits the column; consolidation
            # headers ('3rd Place Match' & co.) mark their segment for
            # extraction from the elimination tree.
            seg_title, seg_cons = title, False
            matches = []
            for el in col.find_all("div", class_=True):
                cls = el.get("class") or []
                if "bracket-match-title" in cls:
                    if matches:
                        rounds.append({"title": seg_title, "matches": matches,
                                       "consolidation": seg_cons})
                    header = el.get_text(" ", strip=True)
                    seg_title = header or title
                    seg_cons = bool(_PF_CONSOLIDATION_RE.search(header or ""))
                    matches = []
                elif "bracket-match" in cls:
                    cells = [
                        c for c in el.find_all(recursive=False)
                        if any("bracket-cell-r" in (cc or "") for cc in (c.get("class") or []))
                    ]
                    def _pf_bold(el) -> bool:
                        st = (el.get("style") or "").lower().replace(" ", "") if el else ""
                        return "font-weight:700" in st or "font-weight:bold" in st

                    players = []
                    for c in cells:
                        name_el = c.find("div", class_="bracket-name")
                        score_el = c.find("div", class_="bracket-score")
                        name = name_el.get_text(strip=True) if name_el else ""
                        score = score_el.get_text(strip=True) if score_el else ""
                        # PF bolds the winner — but WHICH element carries the
                        # bold changed over the years: older pages style the
                        # name (font-weight:700), newer ones style the SCORE
                        # (font-weight:bold) and leave the name unstyled.
                        # Check both, whitespace-insensitive (styles arrive as
                        # e.g. ' font-weight:bold; '). Only checking the name
                        # silently dropped every winner on the new layout
                        # (no bold anywhere in the parsed bracket).
                        is_win = _pf_bold(name_el) or _pf_bold(score_el)
                        players.append({"name": name, "score": score, "winner": is_win})
                    if len(players) >= 2:
                        p1, p2 = players[0], players[1]
                        matches.append({
                            "p1": p1["name"],
                            "p2": p2["name"],
                            "score1": int(p1["score"]) if p1["score"].isdigit() else None,
                            "score2": int(p2["score"]) if p2["score"].isdigit() else None,
                            "winner": "p1" if p1["winner"] else ("p2" if p2["winner"] else None),
                        })
            if matches:
                rounds.append({"title": seg_title, "matches": matches,
                               "consolidation": seg_cons})
        return rounds

    @classmethod
    def log_stats(cls):
        logger.info(
            f"brackets: {cls.fetched} fetched, {cls.skipped} skipped, "
            f"{cls.failed} failed, {cls.no_source} no source"
        )


def rebuild_missing(db, limit: int = 0) -> int:
    """Rebuild parsed tournament_brackets from the raw_brackets cache — offline.

    Replays each cached provider payload through the pure normalizer and
    upserts brackets for tournaments whose parsed row is missing (wiped by
    `reset.py parsed` or awaiting a re-parse after normalization changes).
    No provider requests. Returns the number of brackets rebuilt.
    """
    f = BracketFetcher(db)
    rows = db.missing_bracket_cache_rows(limit or 0)
    n = 0
    for tid, source, payload in rows:
        if f.rebuild_from_cache(int(tid), source, payload):
            n += 1
    return n


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
