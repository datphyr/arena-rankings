"""Twitch bot — chat commands for arena rankings data.

Twitch chat is one line, plain text — no code blocks, no tables.
Output is compact single-line summaries.

Uses DataProvider directly (not subprocess like Discord bot) for speed
in chat context where latency matters.

Commands (prefix !):
    !top [flags]               — leaderboard
    !player <name> [flags]     — player ratings summary
    !history <name> [flags]    — rating progression
    !h2h <p1> <p2> [flags]     — head-to-head summary
    !matches [flags]           — recent matches
    !pmatches <name> [flags]   — recent matches for a player
    !stats                     — system stats
    !games                     — list available games
    !rank <name> [flags]       — player rank
    !help [command]            — command list or short help

Flags (match cli.py syntax, all optional, order-independent):
    --game <game>      game filter (qc, ql, q3, cpm, q4, qw, dbt, br, ow, ref, ut, xon)
    --limit <N>        number of results (1-10, default varies by command)
    --system <sys>     rating system: elo (default) or glicko2
    --glicko2           use Glicko-2 rating system (default: Elo)
    --elo              use Elo rating system (default)
    --peak             sort by peak rating (top command only)
    --sort <field>     sort by: rating (default) or peak

Short forms -g, -n, -s also accepted (same as cli.py).

For multi-message responses, messages are split at sensible boundaries
and sent sequentially with a small delay.
"""

import asyncio
import logging
import re
from typing import Optional

from config import MIN_MATCHES_ELO, MIN_MATCHES_GLICKO2
from src.data_provider import DataProvider, ALL_GAMES, GAME_ALIASES, resolve_game
from src.table import fmt_rating

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 450

DEFAULT_TOP_LIMIT = 5
DEFAULT_MATCHES_LIMIT = 5
DEFAULT_HISTORY_LIMIT = 8

# ─── Game abbreviation ────────────────────────────────────────────────────────

GAME_SHORT = {
    "Combined": "ALL",
    "Quake Champions": "QC",
    "Quake Live": "QL",
    "Quake 3 Arena": "Q3",
    "Quake 3 CPMA": "CPM",
    "Quake 2": "Q2",
    "Quake 4": "Q4",
    "Quake World": "QW",
    "Diabotical": "DBT",
    "Blood Run": "BR",
    "Overwatch": "OW",
    "Reflex": "RFL",
    "Unreal Tournament": "UT",
    "Xonotic": "XON",
}


def _game_short(game: str) -> str:
    return GAME_SHORT.get(game, game[:3].upper() if game else "ALL")


def _game_alias(game: str, aliases: dict) -> str:
    short = aliases.get(game, [])
    if not short:
        return ""
    return min(short, key=len)


# ─── Flag parser ──────────────────────────────────────────────────────────────

class ParsedArgs:
    def __init__(self):
        self.positional: list[str] = []
        self.game: str = ""
        self.game_label: str = "Combined"
        self.limit: Optional[int] = None
        self.system: str = "elo"
        self.sort_by: str = "rating"

    @property
    def sys_label(self) -> str:
        return "Glicko-2" if self.system == "glicko2" else "Elo"


# Long-form flags matching cli.py, plus short forms and convenience shortcuts.
_FLAGS = {
    "--game": "-g",
    "--limit": "-n",
    "--system": "-s",
    "--sort": None,
}
_BOOL_FLAGS = {"--glicko2", "--elo", "--peak"}


def _parse_args(args: list[str], default_limit: int) -> ParsedArgs:
    pa = ParsedArgs()
    i = 0
    while i < len(args):
        tok = args[i]

        # --glicko2 / --elo / --peak (boolean flags)
        if tok == "--glicko2":
            pa.system = "glicko2"
            i += 1
            continue
        if tok == "--elo":
            pa.system = "elo"
            i += 1
            continue
        if tok == "--peak":
            pa.sort_by = "peak"
            i += 1
            continue

        # --sort <field>
        if tok in ("--sort",) and i + 1 < len(args):
            if args[i + 1].lower() in ("peak", "rating"):
                pa.sort_by = args[i + 1].lower()
            i += 2
            continue

        # --game / -g <value>
        if tok in ("--game", "-g") and i + 1 < len(args):
            game_raw = args[i + 1].lower()
            if game_raw in GAME_ALIASES or game_raw in {g.lower() for g, _ in ALL_GAMES}:
                pa.game = resolve_game(game_raw)
                pa.game_label = pa.game
            else:
                pa.game_label = f"unknown game '{args[i+1]}'"
            i += 2
            continue

        # --limit / -n <N>
        if tok in ("--limit", "-n") and i + 1 < len(args) and args[i + 1].isdigit():
            pa.limit = min(max(int(args[i + 1]), 1), 10)
            i += 2
            continue

        # --system / -s <value>
        if tok in ("--system", "-s") and i + 1 < len(args):
            sys_raw = args[i + 1].lower()
            if sys_raw in ("glicko", "glicko2", "g2"):
                pa.system = "glicko2"
            elif sys_raw in ("elo", "e"):
                pa.system = "elo"
            i += 2
            continue

        # Unknown flag — skip
        if tok.startswith("-"):
            i += 1
            continue

        # Positional — check if it's a bare game name (backward compat)
        if not pa.positional and not pa.game and tok.lower() in GAME_ALIASES:
            pa.game = resolve_game(tok.lower())
            pa.game_label = pa.game
        elif not pa.positional and not pa.game and tok.lower() in {g.lower() for g, _ in ALL_GAMES}:
            pa.game = resolve_game(tok.lower())
            pa.game_label = pa.game
        else:
            pa.positional.append(tok)
        i += 1

    if pa.limit is None:
        pa.limit = default_limit

    return pa


# ─── Help system (single-line per command) ────────────────────────────────────

# Each help entry: one line, under MAX_MSG_LEN.
HELP_TEXTS = {
    "top": "!top [--game <g>] [--limit <N>] [--glicko2] [--peak] — leaderboard. Ex: !top --game qc --limit 10 --glicko2",
    "player": "!player <name> [--glicko2] — all game ratings for a player. Ex: !player rapha --glicko2",
    "history": "!history <name> [--game <g>] [--limit <N>] — rating progression by month. Ex: !history rapha --game qc",
    "h2h": "!h2h <p1> <p2> [--game <g>] — head-to-head win/loss record. Ex: !h2h rapha cYpheR --game qc",
    "matches": "!matches [--game <g>] [--limit <N>] — recent matches across all games. Ex: !matches --game qc --limit 10",
    "pmatches": "!pmatches <name> [--game <g>] [--limit <N>] — player's recent matches with W/L. Ex: !pmatches rapha",
    "rank": "!rank <name> [--game <g>] [--glicko2] — rank position + rating. Ex: !rank rapha --game qc --glicko2",
    "stats": "!stats — total matches, players, tournaments, countries, date range",
    "games": "!games — list all games with data and their aliases",
    "help": "!help [command] — list commands or show usage for a specific command. Ex: !help top",
}

CMD_ALIASES = {
    "pmatches": ("pmatches", "playermatches", "player-matches"),
    "help": ("help", "commands"),
}

GENERAL_HELP = ("Commands: !top !player <name> !history <name> !h2h <p1> <p2> !matches "
                "!pmatches <name> !rank <name> !stats !games !help [cmd] "
                "| Flags: --game <g> --limit <N> --glicko2 --peak "
                "| Ex: !top --game qc --glicko2 --limit 10")


def _fmt_help(cmd: str) -> list[str]:
    for canonical, aliases in CMD_ALIASES.items():
        if cmd in aliases:
            cmd = canonical
            break
    if cmd in HELP_TEXTS:
        return [HELP_TEXTS[cmd]]
    return [GENERAL_HELP]


# ─── Output formatters ─────────────────────────────────────────────────────────

def _trunc(s: str, maxlen: int = MAX_MSG_LEN) -> str:
    if len(s) <= maxlen:
        return s
    return s[:maxlen - 3] + "..."


def _fmt_top(players: list, game_label: str, system: str) -> list[str]:
    sys_label = "Glicko-2" if system == "glicko2" else "Elo"
    header = f"Top {len(players)} — {game_label} / {sys_label}"
    entries = []
    for p in players:
        rank = p.get("_rank", "?")
        name = p["name"]
        rating = fmt_rating(p["rating"])
        w = p.get("wins", 0)
        l = p.get("losses", 0)
        entries.append(f"#{rank} {name} {rating} ({w}W/{l}L)")
    body = " | ".join(entries)
    line = f"{header}: {body}"
    if len(line) <= MAX_MSG_LEN:
        return [line]
    return [header, _trunc(body)]


def _fmt_player(ratings: list, name: str, system: str) -> list[str]:
    if not ratings:
        return [f"No ratings found for '{name}'"]
    display = ratings[0].get("name", name)
    sys_label = "Glicko-2" if system == "glicko2" else "Elo"
    entries = []
    for r in ratings:
        game = _game_short(r.get("game") or "Combined")
        if system == "glicko2":
            rating = fmt_rating(r["rating"])
            rd = r.get("rd")
            rd_str = f" ±{fmt_rating(rd)}" if rd else ""
            rank = r.get("rank", "—")
            m = r.get("matches", 0)
            entries.append(f"{game} {rating}{rd_str} #{rank} ({m}m)")
        else:
            rating = fmt_rating(r["rating"])
            rank = r.get("rank", "—")
            m = r.get("matches", 0)
            entries.append(f"{game} {rating} #{rank} ({m}m)")
    line = f"{display} ({sys_label}): " + " | ".join(entries)
    if len(line) <= MAX_MSG_LEN:
        return [line]
    msgs = [f"{display} ({sys_label}):"]
    chunk = ""
    for e in entries:
        if chunk and len(chunk) + 3 + len(e) > MAX_MSG_LEN:
            msgs.append(chunk)
            chunk = e
        else:
            chunk = f"{chunk} | {e}" if chunk else e
    if chunk:
        msgs.append(chunk)
    return msgs


def _fmt_history(entries: list, name: str, game_label: str) -> list[str]:
    if not entries:
        return [f"No history found for '{name}' ({game_label})"]

    seen_months = set()
    spread = []
    for e in entries:
        d = e.get("played_at")
        if not d:
            continue
        month_key = f"{d.year}-{d.month:02d}"
        if month_key not in seen_months:
            seen_months.add(month_key)
            spread.append(e)

    if len(spread) <= 1:
        parts = []
        for e in entries[:8]:
            d = e.get("played_at")
            if not d:
                continue
            date_str = d.strftime("%m-%d %H:%M")
            elo = fmt_rating(e.get("elo"))
            glicko = fmt_rating(e.get("glicko2"))
            if glicko != "—":
                parts.append(f"{date_str}: {elo}/{glicko}")
            else:
                parts.append(f"{date_str}: {elo}")
    else:
        parts = []
        for e in spread:
            d = e.get("played_at")
            date_str = d.strftime("%Y-%m")
            elo = fmt_rating(e.get("elo"))
            glicko = fmt_rating(e.get("glicko2"))
            if glicko != "—":
                parts.append(f"{date_str}: {elo}/{glicko}")
            else:
                parts.append(f"{date_str}: {elo}")

    line = f"{name} ({game_label}) — " + " → ".join(parts)
    return [_trunc(line)]


def _fmt_h2h(result: dict) -> list[str]:
    if result["total"] == 0:
        return [f"No matches between {result.get('player1', '?')} and {result.get('player2', '?')}"]
    p1 = result["player1"]
    p2 = result["player2"]
    total = result["total"]
    pct = result['p1_wins'] / total * 100 if total else 0
    line = (f"{p1} vs {p2}: {result['p1_wins']}W-{result['p2_wins']}W "
            f"({total} matches, {pct:.0f}%/{100-pct:.0f}%)")
    return [line]


def _fmt_matches(matches: list, game_label: str) -> list[str]:
    if not matches:
        return [f"No matches found ({game_label})"]
    header = f"Recent ({game_label}): "
    entries = []
    for m in matches:
        p1 = m["player1"]
        p2 = m["player2"]
        score = m["score"]
        game = _game_short(m.get("game") or "")
        entries.append(f"{p1} {score} {p2} ({game})")
    body = " | ".join(entries)
    line = header + body
    if len(line) <= MAX_MSG_LEN:
        return [line]
    msgs = []
    chunk = header.rstrip(": ")
    for e in entries:
        if chunk and len(chunk) + 3 + len(e) > MAX_MSG_LEN:
            msgs.append(chunk)
            chunk = e
        else:
            chunk = f"{chunk} | {e}" if chunk else e
    if chunk:
        msgs.append(chunk)
    return msgs


def _fmt_player_matches(matches: list, name: str, game_label: str) -> list[str]:
    if not matches:
        return [f"No matches found for '{name}' ({game_label})"]
    header = f"{name} ({game_label}): "
    entries = []
    for m in matches:
        opponent = m["player2"] if m["player1"] == name else m["player1"]
        result = "W" if m["winner"] == name else "L"
        score = m["score"]
        game = _game_short(m.get("game") or "")
        if m["player1"] != name:
            parts_s = score.split("-")
            if len(parts_s) == 2:
                score = f"{parts_s[1]}-{parts_s[0]}"
        entries.append(f"{result} {opponent} {score} ({game})")
    body = ", ".join(entries)
    line = header + body
    if len(line) <= MAX_MSG_LEN:
        return [line]
    msgs = []
    chunk = ""
    for e in entries:
        if chunk and len(chunk) + 2 + len(e) > MAX_MSG_LEN:
            msgs.append(f"{name}: {chunk}")
            chunk = e
        else:
            chunk = f"{chunk}, {e}" if chunk else e
    if chunk:
        msgs.append(f"{name}: {chunk}")
    return msgs


def _fmt_stats(stats: dict) -> list[str]:
    total_m = stats["total_matches"]
    total_p = stats["total_players"]
    total_t = stats["tournaments"]
    countries = stats["countries"]
    earliest, latest = stats["date_range"]
    yr_from = earliest.year if earliest else "?"
    yr_to = latest.year if latest else "?"
    return [f"{total_m:,} matches | {total_p:,} players | {total_t:,} tournaments | "
            f"{countries} countries ({yr_from}-{yr_to})"]


def _fmt_games(games: list, aliases: dict) -> list[str]:
    parts = []
    for name in games:
        short = GAME_SHORT.get(name, name)
        alias = _game_alias(name, aliases)
        if alias:
            parts.append(f"{short} ({alias})")
        else:
            parts.append(short)
    return [_trunc("Games: " + ", ".join(parts))]


def _fmt_rank(rank_info: Optional[dict], name: str, game_label: str, sys_label: str) -> list[str]:
    if not rank_info:
        return [f"No rank found for '{name}' ({game_label} / {sys_label})"]
    game_short = _game_short(rank_info.get("game", game_label))
    return [f"{rank_info['name']} — #{rank_info['rank']}/{rank_info['total']} "
            f"{game_short} {rank_info['system'].title()} "
            f"({fmt_rating(rank_info['rating'])}, {rank_info['wins']}W/{rank_info['losses']}L)"]


# ─── Bot ───────────────────────────────────────────────────────────────────────

class TwitchBot:
    """Twitch IRC bot for arena rankings.

    Uses raw IRC over TLS (no external deps beyond what's already installed).
    Supports multiple channels — responses go to the channel where the
    command was issued.
    """

    def __init__(self, token: str, channels: str | list[str], nickname: str = "arenabot",
                 prefix: str = "!"):
        self.token = token if token.startswith("oauth:") else f"oauth:{token}"
        if isinstance(channels, str):
            channels = [c.strip() for c in channels.split(",") if c.strip()]
        self.channels = [c.lstrip("#").lower() for c in channels if c.strip()]
        self.nickname = nickname.lower()
        self.prefix = prefix
        self._reader = None
        self._writer = None
        self._dx: Optional[DataProvider] = None

    async def run(self):
        """Connect to Twitch IRC and process messages."""
        import ssl

        logger.info(f"connecting to Twitch IRC as {self.nickname}, channels: {self.channels}")

        self._dx = DataProvider()

        ctx = ssl.create_default_context()

        while True:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "irc.chat.twitch.tv", 6697, ssl=ctx,
                )
                self._writer.write(f"PASS {self.token}\r\n".encode())
                self._writer.write(f"NICK {self.nickname}\r\n".encode())
                join_cmd = ",".join(f"#{c}" for c in self.channels)
                self._writer.write(f"JOIN {join_cmd}\r\n".encode())
                self._writer.write(b"CAP REQ :twitch.tv/tags\r\n")
                await self._writer.drain()

                logger.info(f"connected to {len(self.channels)} channel(s): {', '.join('#' + c for c in self.channels)}")

                if self._dx is None:
                    self._dx = DataProvider()

                await self._read_loop()

            except (ConnectionError, OSError) as e:
                logger.warning(f"connection lost: {e}, reconnect in 10s")
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"unexpected error: {e}", exc_info=True)
                await asyncio.sleep(10)
            finally:
                if self._writer and not self._writer.is_closing():
                    self._writer.close()
                    try:
                        await self._writer.wait_closed()
                    except Exception:
                        pass

    async def _read_loop(self):
        buf = b""
        while True:
            data = await self._reader.read(4096)
            if not data:
                logger.warning("IRC connection closed by server")
                return
            buf += data
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                await self._handle_line(line.decode("utf-8", errors="replace"))

    async def _handle_line(self, line: str):
        if line.startswith("PING"):
            self._writer.write(line.replace("PING", "PONG").encode() + b"\r\n")
            await self._writer.drain()
            return

        # Log NOTICE and CLEARCHAT etc. for debugging
        if "NOTICE" in line or "CLEARCHAT" in line or "CLEARMSG" in line:
            logger.warning(f"IRC: {line[:300]}")
            return

        if "PRIVMSG" not in line:
            # Log USERSTATE (response to our PRIVMSG) for debugging
            if "USERSTATE" in line:
                logger.debug(f"IRC: {line[:200]}")
            return

        chan_match = re.search(r'PRIVMSG #([^ ]+)', line)
        if not chan_match:
            return
        target_channel = chan_match.group(1).lower()

        msg_match = re.search(r'PRIVMSG #[^ ]+ :(.+)$', line)
        if not msg_match:
            return
        msg = msg_match.group(1).strip()

        name_match = re.search(r'display-name=([^;]+)', line)
        user = name_match.group(1) if name_match else "?"

        logger.debug(f"RECV #{target_channel} {user}: {msg}")

        if not msg.startswith(self.prefix):
            return

        cmd_parts = msg[len(self.prefix):].split()
        if not cmd_parts:
            return
        cmd = cmd_parts[0].lower()
        args = cmd_parts[1:]

        logger.info(f"CMD #{target_channel} {user}: !{cmd} {args}")

        response = await self._dispatch(cmd, args)
        if response:
            logger.info(f"SEND #{target_channel}: {response}")
            if isinstance(response, str):
                await self._send([response], target_channel)
            else:
                await self._send(response, target_channel)
        else:
            logger.debug(f"NORESP #{target_channel} !{cmd} — no handler or None returned")

    async def _send(self, messages: list[str], channel: Optional[str] = None):
        targets = [channel] if channel else self.channels
        for ch in targets:
            for msg in messages:
                raw = f"PRIVMSG #{ch} :{msg}\r\n"
                logger.debug(f"SEND_RAW #{ch}: {msg[:80]}")
                if len(msg) <= MAX_MSG_LEN:
                    self._writer.write(raw.encode())
                else:
                    for i in range(0, len(msg), MAX_MSG_LEN):
                        self._writer.write(f"PRIVMSG #{ch} :{msg[i:i+MAX_MSG_LEN]}\r\n".encode())
                if len(messages) > 1:
                    await asyncio.sleep(0.3)
        await self._writer.drain()

    async def _dispatch(self, cmd: str, args: list[str]) -> Optional[str | list[str]]:
        try:
            if cmd == "top":
                return self._cmd_top(args)
            elif cmd == "player":
                return self._cmd_player(args)
            elif cmd == "history":
                return self._cmd_history(args)
            elif cmd == "h2h":
                return self._cmd_h2h(args)
            elif cmd == "matches":
                return self._cmd_matches(args)
            elif cmd in CMD_ALIASES["pmatches"]:
                return self._cmd_player_matches(args)
            elif cmd == "stats":
                return self._cmd_stats()
            elif cmd == "games":
                return self._cmd_games()
            elif cmd == "rank":
                return self._cmd_rank(args)
            elif cmd in CMD_ALIASES["help"]:
                return self._cmd_help(args)
            else:
                return None
        except Exception as e:
            logger.error(f"command !{cmd} failed: {e}", exc_info=True)
            return [f"Error processing !{cmd}"]

    # ─── Command implementations ───────────────────────────────────────────

    def _cmd_help(self, args: list[str]) -> list[str]:
        if args:
            return _fmt_help(args[0].lower())
        return [GENERAL_HELP]

    def _cmd_top(self, args: list[str]) -> list[str]:
        pa = _parse_args(args, DEFAULT_TOP_LIMIT)
        min_m = MIN_MATCHES_ELO if pa.system == "elo" else MIN_MATCHES_GLICKO2

        players = self._dx.get_top_players(
            game=pa.game, system=pa.system, limit=pa.limit,
            min_matches=min_m, sort_by=pa.sort_by,
        )
        if not players:
            return [f"No players found ({pa.game_label} / {pa.sys_label})"]
        for i, p in enumerate(players, 1):
            p["_rank"] = i
        return _fmt_top(players, pa.game_label, pa.system)

    def _cmd_player(self, args: list[str]) -> list[str]:
        if not args:
            return ["Usage: !player <name> [--glicko2]. See !help player"]
        pa = _parse_args(args, 0)
        if not pa.positional:
            return ["Usage: !player <name> [--glicko2]. See !help player"]
        name = " ".join(pa.positional)
        min_m = MIN_MATCHES_ELO if pa.system == "elo" else MIN_MATCHES_GLICKO2

        ratings = self._dx.get_player_ratings(name)
        if not ratings:
            return [f"No ratings found for '{name}'"]

        sys_r = sorted(
            [r for r in ratings if r["system"] == pa.system and r["matches"] >= min_m],
            key=lambda r: r["rating"], reverse=True,
        )
        if not sys_r:
            sys_label = "Glicko-2" if pa.system == "glicko2" else "Elo"
            return [f"No {sys_label} ratings found for '{name}' (min {min_m} matches)"]

        for r in sys_r:
            rank = self._dx.get_player_rank(
                name, game=r["game"] if r["game"] != "Combined" else "",
                system=pa.system, min_matches=min_m,
            )
            r["rank"] = rank["rank"] if rank else "—"

        if pa.system == "glicko2":
            for r in sys_r:
                r.setdefault("rd", None)

        return _fmt_player(sys_r, name, pa.system)

    def _cmd_history(self, args: list[str]) -> list[str]:
        if not args:
            return ["Usage: !history <name> [--game <g>] [--limit <N>]. See !help history"]
        pa = _parse_args(args, DEFAULT_HISTORY_LIMIT)
        if not pa.positional:
            return ["Usage: !history <name> [--game <g>] [--limit <N>]. See !help history"]
        name = pa.positional[0]

        fetch_limit = max(pa.limit * 4, 30)
        merged = self._dx.get_player_history_both(
            name, game=pa.game, limit=fetch_limit,
        )
        if not merged:
            return [f"No history found for '{name}' ({pa.game_label})"]

        resolved = self._dx._resolve_name(name)
        return _fmt_history(merged, resolved, pa.game_label)

    def _cmd_h2h(self, args: list[str]) -> list[str]:
        if not args:
            return ["Usage: !h2h <p1> <p2> [--game <g>]. See !help h2h"]
        pa = _parse_args(args, 0)
        if len(pa.positional) < 2:
            return ["Usage: !h2h <p1> <p2> [--game <g>]. See !help h2h"]
        p1, p2 = pa.positional[0], pa.positional[1]
        result = self._dx.get_head_to_head(p1, p2, game=pa.game, limit=50)
        return _fmt_h2h(result)

    def _cmd_matches(self, args: list[str]) -> list[str]:
        pa = _parse_args(args, DEFAULT_MATCHES_LIMIT)
        matches = self._dx.get_recent_matches(game=pa.game, limit=pa.limit)
        return _fmt_matches(matches, pa.game_label)

    def _cmd_player_matches(self, args: list[str]) -> list[str]:
        if not args:
            return ["Usage: !pmatches <name> [--game <g>] [--limit <N>]. See !help pmatches"]
        pa = _parse_args(args, DEFAULT_MATCHES_LIMIT)
        if not pa.positional:
            return ["Usage: !pmatches <name> [--game <g>] [--limit <N>]. See !help pmatches"]
        name = pa.positional[0]
        resolved = self._dx._resolve_name(name)
        matches = self._dx.get_player_matches(name, game=pa.game, limit=pa.limit)
        return _fmt_player_matches(matches, resolved, pa.game_label)

    def _cmd_stats(self) -> list[str]:
        stats = self._dx.get_stats()
        return _fmt_stats(stats)

    def _cmd_games(self) -> list[str]:
        games = self._dx.get_games()
        alias_map = {}
        for alias, full in GAME_ALIASES.items():
            alias_map.setdefault(full, []).append(alias)
        return _fmt_games(games, alias_map)

    def _cmd_rank(self, args: list[str]) -> list[str]:
        if not args:
            return ["Usage: !rank <name> [--game <g>] [--glicko2]. See !help rank"]
        pa = _parse_args(args, 0)
        if not pa.positional:
            return ["Usage: !rank <name> [--game <g>] [--glicko2]. See !help rank"]
        name = pa.positional[0]
        min_m = MIN_MATCHES_ELO if pa.system == "elo" else MIN_MATCHES_GLICKO2
        rank = self._dx.get_player_rank(
            name, game=pa.game, system=pa.system, min_matches=min_m,
        )
        return _fmt_rank(rank, name, pa.game_label, pa.sys_label)