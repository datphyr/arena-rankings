"""Discord bot — slash commands for arena rankings data.

Uses discord.py 2.x with app_commands (slash commands).
All output uses the shared formatter functions from cli.py (fmt_*) via
DataProvider directly — no subprocess, no extra DB connections per command.

Commands:
    /top        — leaderboard (optional game, system, limit, min_matches)
    /player     — all ratings for a player
    /history    — rating progression
    /h2h        — head-to-head between two players
    /matches    — recent matches
    /player-matches — recent matches for a player
    /stats      — overall system stats
    /games      — list available games
"""

import asyncio
import logging
import time
from typing import Optional, Tuple

# Debounce: (user_id, command_name) -> (timestamp, cached_content)
_recent_commands: dict[Tuple[int, str], Tuple[float, str]] = {}
_DEBOUNCE_SECONDS = 5.0

import discord
from discord import app_commands
from discord.ext import commands

NL = "\n"
logger = logging.getLogger(__name__)

from config import MIN_MATCHES_ELO, MIN_MATCHES_GLICKO2
from src.data_provider import DataProvider, resolve_game
from cli import (
    fmt_top, fmt_player, fmt_history, fmt_h2h, fmt_matches,
    fmt_player_matches, fmt_stats, fmt_games,
)

# Game choices for slash command options
GAME_CHOICES = [
    app_commands.Choice(name="All Games", value=""),
    app_commands.Choice(name="Quake Live", value="Quake Live"),
    app_commands.Choice(name="Quake Champions", value="Quake Champions"),
    app_commands.Choice(name="Quake 3 Arena", value="Quake 3 Arena"),
    app_commands.Choice(name="Quake 3 CPMA", value="Quake 3 CPMA"),
    app_commands.Choice(name="Quake 2", value="Quake 2"),
    app_commands.Choice(name="Quake 4", value="Quake 4"),
    app_commands.Choice(name="Quake World", value="Quake World"),
    app_commands.Choice(name="Diabotical", value="Diabotical"),
    app_commands.Choice(name="Blood Run", value="Blood Run"),
    app_commands.Choice(name="Overwatch", value="Overwatch"),
    app_commands.Choice(name="Reflex", value="Reflex"),
    app_commands.Choice(name="Unreal Tournament", value="Unreal Tournament"),
    app_commands.Choice(name="Xonotic", value="Xonotic"),
]

SYSTEM_CHOICES = [
    app_commands.Choice(name="Elo", value="elo"),
    app_commands.Choice(name="Glicko-2", value="glicko2"),
]
SORT_CHOICES = [
    app_commands.Choice(name="Rating", value="rating"),
    app_commands.Choice(name="Peak", value="peak"),
]


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _game_val(game):
    return game.value if game else ""

def _game_name(game):
    return game.name if game else "All Games"

def _system_val(system):
    return system.value if system else "elo"

def _system_name(system):
    return system.name if system else "Elo"


def _wrap_codeblock(text: str) -> str:
    """Wrap formatter output in a Discord code block, truncating if needed."""
    text = text.strip()
    if not text:
        return "No results found."
    content = "```" + NL + text + NL + "```"
    if len(content) > 1900:
        content = content[:1890] + "\n... (truncated)\n```"
    return content


def _check_debounce(interaction: discord.Interaction, cmd_name: str) -> str | None:
    """Return cached content if same user sent same command recently."""
    key = (interaction.user.id, cmd_name)
    now = time.time()
    if key in _recent_commands:
        ts, content = _recent_commands[key]
        if now - ts < _DEBOUNCE_SECONDS:
            return content
    return None

def _store_debounce(interaction: discord.Interaction, cmd_name: str, content: str):
    """Cache command result for debounce."""
    key = (interaction.user.id, cmd_name)
    _recent_commands[key] = (time.time(), content)
    # Cleanup old entries
    cutoff = time.time() - _DEBOUNCE_SECONDS * 2
    for k in list(_recent_commands):
        if _recent_commands[k][0] < cutoff:
            del _recent_commands[k]

async def _safe_defer(interaction: discord.Interaction) -> bool:
    """Defer interaction, return True if successful, False if expired."""
    try:
        await asyncio.wait_for(
            interaction.response.defer(),
            timeout=3,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(f"defer timeout: {interaction.user}")
        return False
    except discord.NotFound:
        logger.warning(f"defer failed {interaction.user}: not found")
        return False
    except discord.HTTPException as e:
        logger.warning(f"defer failed {interaction.user}: HTTP {e.status}")
        return False
    except Exception as e:
        logger.warning(f"defer failed {interaction.user}: {type(e).__name__}: {e}")
        return False


async def _safe_followup(interaction: discord.Interaction, content: str, *, retries: int = 3):
    """Send followup with retries and timeout — survives brief WebSocket disconnects."""
    for attempt in range(retries):
        try:
            await asyncio.wait_for(
                interaction.followup.send(content=content),
                timeout=10,
            )
            return
        except asyncio.TimeoutError:
            logger.warning(f"followup timeout ({attempt + 1}/{retries})")
        except discord.HTTPException as e:
            logger.warning(f"followup HTTP {e.status} ({attempt + 1}/{retries})")
        except Exception as e:
            logger.warning(f"followup {type(e).__name__} ({attempt + 1}/{retries})")
        if attempt < retries - 1:
            await asyncio.sleep(2)
    logger.error(f"followup failed after {retries} attempts ({len(content)} chars)")


class ArenaBot(commands.Bot):
    """Arena Rankings Discord bot."""

    def __init__(self, token: str, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents, **kwargs)
        self.token = token
        self._commands_synced = False
        self._dx: Optional[DataProvider] = None

    async def setup_hook(self):
        # Persistent DataProvider for all commands (no per-command DB connection)
        self._dx = DataProvider()
        logger.info("DataProvider connected")

    async def on_disconnect(self):
        logger.warning("websocket disconnected")

    async def on_resumed(self):
        logger.info("websocket resumed")

    async def on_ready(self):
        logger.info(f"logged in as {self.user}")
        if not self._commands_synced:
            try:
                synced = await self.tree.sync()
                logger.info(f"synced {len(synced)} commands")
                self._commands_synced = True
            except Exception as e:
                logger.error(f"sync failed: {e}")

    def run(self):
        super().run(self.token, log_handler=None)


def create_bot(token: str) -> ArenaBot:
    bot = ArenaBot(token=token)

    @bot.tree.command(name="top", description="Leaderboard")
    @app_commands.choices(game=GAME_CHOICES, system=SYSTEM_CHOICES, sort=SORT_CHOICES)
    async def top(
        interaction: discord.Interaction,
        game: Optional[app_commands.Choice[str]] = None,
        system: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
        min_matches: app_commands.Range[int, 0, 1000] = -1,
        date: Optional[str] = None,
        sort: Optional[app_commands.Choice[str]] = None,
    ):
        # Check debounce — respond immediately with cached result
        cached = _check_debounce(interaction, "top")
        if cached is not None:
            try:
                await interaction.response.send_message(content=cached, ephemeral=False)
            except Exception as e:
                logger.warning(f"/top debounce send failed: {e}")
            logger.debug(f"/top debounced, sent cached")
            return
        if not await _safe_defer(interaction): return
        sys_val = _system_val(system)
        mm = min_matches if min_matches >= 0 else -1  # -1 = use config defaults
        gv = _game_val(game)
        sv = sort.value if sort else "rating"
        try:
            text = fmt_top(bot._dx, game=gv, system="elo", limit=limit,
                          min_matches=mm, date=date, sort_by=sv)
        except Exception as e:
            logger.error(f"/top query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        _store_debounce(interaction, "top", content)
        logger.info(f"/top: {len(content)} chars")

    @bot.tree.command(name="player", description="All ratings for a player")
    async def player(interaction: discord.Interaction, name: str):
        if not await _safe_defer(interaction): return
        try:
            text = fmt_player(bot._dx, name=name)
        except Exception as e:
            logger.error(f"/player query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/player {name}: {len(content)} chars")

    @bot.tree.command(name="history", description="Rating progression for a player")
    @app_commands.choices(game=GAME_CHOICES, system=SYSTEM_CHOICES)
    async def history(
        interaction: discord.Interaction,
        name: str,
        game: Optional[app_commands.Choice[str]] = None,
        system: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 50] = 20,
    ):
        if not await _safe_defer(interaction): return
        gv = _game_val(game)
        try:
            text = fmt_history(bot._dx, name=name, game=gv, limit=limit)
        except Exception as e:
            logger.error(f"/history query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/history {name}: {len(content)} chars")

    @bot.tree.command(name="h2h", description="Head-to-head between two players")
    @app_commands.choices(game=GAME_CHOICES)
    async def h2h(
        interaction: discord.Interaction,
        player1: str,
        player2: str,
        game: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 50] = 20,
    ):
        if not await _safe_defer(interaction): return
        gv = _game_val(game)
        try:
            text = fmt_h2h(bot._dx, player1=player1, player2=player2,
                          game=gv, limit=limit)
        except Exception as e:
            logger.error(f"/h2h query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/h2h {player1} vs {player2}: {len(content)} chars")

    @bot.tree.command(name="matches", description="Recent matches")
    @app_commands.choices(game=GAME_CHOICES)
    async def matches(
        interaction: discord.Interaction,
        game: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        if not await _safe_defer(interaction): return
        gv = _game_val(game)
        try:
            text = fmt_matches(bot._dx, game=gv, limit=limit)
        except Exception as e:
            logger.error(f"/matches query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/matches: {len(content)} chars")

    @bot.tree.command(name="player-matches", description="Recent matches for a player")
    @app_commands.choices(game=GAME_CHOICES)
    async def player_matches(
        interaction: discord.Interaction,
        name: str,
        game: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ):
        if not await _safe_defer(interaction): return
        gv = _game_val(game)
        try:
            text = fmt_player_matches(bot._dx, name=name, game=gv, limit=limit)
        except Exception as e:
            logger.error(f"/player-matches query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/player-matches {name}: {len(content)} chars")

    @bot.tree.command(name="stats", description="Overall system stats")
    async def stats(interaction: discord.Interaction):
        if not await _safe_defer(interaction): return
        try:
            text = fmt_stats(bot._dx)
        except Exception as e:
            logger.error(f"/stats query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        await _safe_followup(interaction, content)
        logger.info(f"/stats: {len(content)} chars")

    @bot.tree.command(name="games", description="List available games")
    async def games(interaction: discord.Interaction):
        try:
            text = fmt_games(bot._dx)
        except Exception as e:
            logger.error(f"/games query failed: {e}", exc_info=True)
            text = f"Error: {e}"
        content = _wrap_codeblock(text)
        try:
            await interaction.response.send_message(content=content)
            logger.info(f"/games: {len(content)} chars")
        except (discord.NotFound, discord.HTTPException):
            pass

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"command error: {error}", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content=f"Error: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(content=f"Error: {error}", ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    return bot