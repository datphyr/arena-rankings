#!/usr/bin/env python3
"""CLI — command-line interface for querying arena rankings data.

Usage:
    python cli.py top [--game GAME] [--system SYS] [--limit N] [--min-matches N]
    python cli.py player NAME [--system SYS] [--game GAME]
    python cli.py history NAME [--system SYS] [--game GAME] [--limit N]
    python cli.py h2h P1 P2 [--game GAME] [--limit N]
    python cli.py matches [--game GAME] [--limit N]
    python cli.py player-matches NAME [--game GAME] [--limit N]
    python cli.py stats
    python cli.py games
    python cli.py tournaments [--tier TIER]

Subcommands:
    top             — leaderboard
    player          — all ratings for a player
    history         — rating progression
    h2h             — head-to-head between two players
    matches         — recent matches
    player-matches  — recent matches for a player
    stats           — overall system stats
    games           — list available games
"""

import argparse
import sys
from pathlib import Path
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent))

from src.data_provider import (
    DataProvider, ALL_GAMES, GAME_ALIASES, resolve_game,
    top_cols, player_cols_elo, player_cols_glicko, history_cols, history_cols_both, h2h_cols,
    matches_cols, player_matches_cols, games_cols,
    tournaments_cols, tier_stats_cols,
)
from src.table import print_table, table_lines, Col, fmt_wr
from config import MIN_MATCHES_ELO, MIN_MATCHES_GLICKO2

# Re-export for downstream consumers
__all__ = ["ALL_GAMES", "GAME_ALIASES", "resolve_game"]


def _system_display(system: str) -> str:
    """Consistent display name for a rating system."""
    return "Glicko-2" if system == "glicko2" else "Elo"


# ─── Formatters (return strings, used by CLI, Discord bot, etc.) ───────────────

def fmt_top(dx: DataProvider, game: str = "", system: str = "elo",
            limit: int = 20, min_matches: int = -1,
            date: str = None, sort_by: str = "rating") -> str:
    """Format leaderboard as a string."""
    buf = StringIO()
    game_label = game or "All Games"
    is_combined = not game

    for sys_name in ("elo", "glicko2"):
        mm = min_matches
        if mm < 0:
            mm = MIN_MATCHES_GLICKO2 if sys_name == "glicko2" else MIN_MATCHES_ELO

        if date:
            players = dx.get_top_players_asof(
                date=date, game=game, system=sys_name, limit=limit, min_matches=mm,
                sort_by=sort_by,
            )
        else:
            players = dx.get_top_players(
                game=game, system=sys_name, limit=limit, min_matches=mm,
                sort_by=sort_by,
            )

        if not players:
            label = f"{game_label} / {_system_display(sys_name)}"
            if date:
                label += f" as of {date}"
            buf.write(f"\n  No players found for {label}\n\n")
            continue

        is_glicko = sys_name == "glicko2"
        for i, p in enumerate(players, 1):
            p["_rank"] = i

        header = f"\n  Top {len(players)} — {game_label} / {_system_display(sys_name)}"
        if date:
            header += f" as of {date}"
        buf.write(header + "\n\n")
        buf.write("\n".join(table_lines(top_cols(is_glicko, is_combined), players)) + "\n\n")

    return buf.getvalue()


def fmt_player(dx: DataProvider, name: str, min_matches: int = -1) -> str:
    """Format player ratings as a string."""
    buf = StringIO()
    ratings = dx.get_player_ratings(name)
    if not ratings:
        return f"No ratings found for '{name}'\n"

    mm = min_matches
    if mm < 0:
        elo_ratings = [r for r in ratings if r['system'] == 'elo' and r['matches'] >= MIN_MATCHES_ELO]
        glicko_ratings = [r for r in ratings if r['system'] == 'glicko2' and r['matches'] >= MIN_MATCHES_GLICKO2]
    else:
        elo_ratings = [r for r in ratings if r['system'] == 'elo' and r['matches'] >= mm]
        glicko_ratings = [r for r in ratings if r['system'] == 'glicko2' and r['matches'] >= mm]
    elo_ratings = sorted(elo_ratings, key=lambda r: r['rating'], reverse=True)
    glicko_ratings = sorted(glicko_ratings, key=lambda r: r['rating'], reverse=True)

    elo_mm = MIN_MATCHES_ELO if mm < 0 else mm
    glicko_mm = MIN_MATCHES_GLICKO2 if mm < 0 else mm
    for r in elo_ratings + glicko_ratings:
        r_mm = elo_mm if r['system'] == 'elo' else glicko_mm
        rank = dx.get_player_rank(
            name, game=r['game'] if r['game'] != 'All Games' else '',
            system=r['system'], min_matches=r_mm,
        )
        r['rank'] = rank['rank'] if rank else '—'

    display_name = ratings[0]['name'] if ratings else name
    buf.write(f"\n  Player: {display_name}\n\n")
    if elo_ratings:
        buf.write("\n  Elo\n\n")
        buf.write("\n".join(table_lines(player_cols_elo(), elo_ratings)) + "\n\n")
    if glicko_ratings:
        buf.write("\n  Glicko-2\n\n")
        buf.write("\n".join(table_lines(player_cols_glicko(), glicko_ratings)) + "\n\n")

    return buf.getvalue()


def fmt_history(dx: DataProvider, name: str, game: str = "",
                system: str = "elo", limit: int = 30) -> str:
    """Format rating history as a string."""
    buf = StringIO()
    resolved = dx._resolve_name(name)
    game_label = game or "All Games"

    merged = dx.get_player_history_both(name, game=game, limit=limit)
    if not merged:
        return f"No history found for '{name}' ({game_label})\n"

    buf.write(f"\n  History: {resolved} — {game_label} (last {len(merged)})\n\n")
    buf.write("\n".join(table_lines(history_cols_both(), merged)) + "\n\n")
    return buf.getvalue()


def fmt_h2h(dx: DataProvider, player1: str, player2: str,
            game: str = "", limit: int = 20) -> str:
    """Format head-to-head as a string."""
    buf = StringIO()
    result = dx.get_head_to_head(player1, player2, game=game, limit=limit)
    if result["total"] == 0:
        game_label = game or "all games"
        return f"No matches found between {player1} and {player2} ({game_label})\n"

    p1 = result['player1']
    p2 = result['player2']
    game_label = game or "all games"
    buf.write(f"\n  Head-to-head: {p1} vs {p2} ({game_label})\n")
    buf.write(f"  {p1}: {result['p1_wins']}W  {p2}: {result['p2_wins']}W  "
              f"(total {result['total']} matches)\n\n")
    buf.write("\n".join(table_lines(h2h_cols(), result["matches"])) + "\n\n")
    return buf.getvalue()


def fmt_matches(dx: DataProvider, game: str = "", limit: int = 20) -> str:
    """Format recent matches as a string."""
    buf = StringIO()
    matches = dx.get_recent_matches(game=game, limit=limit)
    if not matches:
        game_label = game or "all games"
        return f"No matches found ({game_label})\n"

    game_label = game or "all games"
    buf.write(f"\n  Recent matches ({game_label}, last {len(matches)})\n\n")
    buf.write("\n".join(table_lines(matches_cols(), matches)) + "\n\n")
    return buf.getvalue()


def fmt_player_matches(dx: DataProvider, name: str, game: str = "",
                       limit: int = 20) -> str:
    """Format a player's recent matches as a string."""
    buf = StringIO()
    resolved = dx._resolve_name(name)
    matches = dx.get_player_matches(name, game=game, limit=limit)
    if not matches:
        game_label = game or "all games"
        return f"No matches found for '{name}' ({game_label})\n"

    game_label = game or "all games"
    buf.write(f"\n  Matches: {resolved} ({game_label}, last {len(matches)})\n\n")
    buf.write("\n".join(table_lines(player_matches_cols(resolved), matches)) + "\n\n")
    return buf.getvalue()


def fmt_stats(dx: DataProvider) -> str:
    """Format system stats as a string."""
    buf = StringIO()
    stats = dx.get_stats()
    earliest, latest = stats['date_range']

    summary = [
        {"k": "Matches discovered", "v": f"{stats['total_discovered']:,}"},
        {"k": "Matches parsed",     "v": f"{stats['total_matches']:,}"},
        {"k": "Players total",      "v": f"{stats['total_players']:,}"},
        {"k": "Players active",     "v": f"{stats['active_players']:,}"},
        {"k": "Player avg matches", "v": f"{stats['avg_matches']}"},
        {"k": "Tournaments",        "v": f"{stats['tournaments']:,}"},
        {"k": "Countries",          "v": f"{stats['countries']:,}"},
        {"k": "Date from",          "v": f"{earliest:%Y-%m-%d}"},
        {"k": "Date to",            "v": f"{latest:%Y-%m-%d}"},
    ]

    buf.write("\n  Arena Rankings System\n\n")
    buf.write("\n".join(table_lines(
        [Col("Metric", "<", key=lambda r: r["k"]),
         Col("Value", ">", key=lambda r: r["v"])],
        summary,
    )) + "\n\n")

    game_rows = [{"name": g, "tournaments": stats['tournaments_per_game'].get(g, 0), "matches": c}
                 for g, c in stats['matches_per_game'].items()]
    buf.write("\n".join(table_lines(
        [Col("Game", "<", key=lambda r: r["name"]),
         Col("Tournaments", ">", key=lambda r: f"{r['tournaments']:,}"),
         Col("Matches", ">", key=lambda r: f"{r['matches']:,}")],
        game_rows,
    )) + "\n\n")

    tier_stats = dx.get_tournament_stats()
    if tier_stats["tiers"]:
        buf.write("\n".join(table_lines(tier_stats_cols(), tier_stats["tiers"])) + "\n\n")

    return buf.getvalue()


def fmt_games(dx: DataProvider) -> str:
    """Format games list as a string."""
    buf = StringIO()
    db_games = set(dx.get_games())
    alias_map = {}
    for alias, full in GAME_ALIASES.items():
        alias_map.setdefault(full, []).append(alias)

    buf.write("\n")
    rows = [{"name": name} for name, _ in ALL_GAMES]
    buf.write("\n".join(table_lines(games_cols(db_games, alias_map), rows)) + "\n\n")
    return buf.getvalue()


def fmt_tournaments(dx: DataProvider, tier: str = "", game: str = "",
                    limit: int = 30) -> str:
    """Format tournaments list as a string."""
    buf = StringIO()
    tournaments = dx.get_tournaments(tier=tier, limit=limit)
    if game:
        tournaments = [t for t in tournaments if t["game"] == game]
    if not tournaments:
        filters = []
        if tier:
            filters.append(f"tier={tier}")
        if game:
            filters.append(f"game={game}")
        filter_label = ", ".join(filters) if filters else "all"
        return f"No tournaments found ({filter_label})\n"

    parts = []
    if tier:
        parts.append(f"tier={tier}")
    if game:
        parts.append(f"game={game}")
    filter_label = ", ".join(parts) if parts else "all"
    buf.write(f"\n  Tournaments ({filter_label}, {len(tournaments)} shown)\n\n")
    buf.write("\n".join(table_lines(tournaments_cols(), tournaments)) + "\n\n")
    return buf.getvalue()


# ─── CLI command handlers (thin wrappers that print) ───────────────────────────

def cmd_top(args, dx: DataProvider):
    print(fmt_top(
        dx, game=args.game, system="elo", limit=args.limit,
        min_matches=args.min_matches, date=getattr(args, 'date', None),
        sort_by=getattr(args, 'sort', 'rating'),
    ), end="")


def cmd_player(args, dx: DataProvider):
    print(fmt_player(dx, name=args.name, min_matches=args.min_matches), end="")


def cmd_history(args, dx: DataProvider):
    print(fmt_history(
        dx, name=args.name, game=args.game,
        system=args.system, limit=args.limit,
    ), end="")


def cmd_h2h(args, dx: DataProvider):
    print(fmt_h2h(
        dx, player1=args.player1, player2=args.player2,
        game=args.game, limit=args.limit,
    ), end="")


def cmd_matches(args, dx: DataProvider):
    print(fmt_matches(dx, game=args.game, limit=args.limit), end="")


def cmd_player_matches(args, dx: DataProvider):
    print(fmt_player_matches(dx, name=args.name, game=args.game, limit=args.limit), end="")


def cmd_stats(args, dx: DataProvider):
    print(fmt_stats(dx), end="")


def cmd_games(args, dx: DataProvider):
    print(fmt_games(dx), end="")


def cmd_tournaments(args, dx: DataProvider):
    print(fmt_tournaments(dx, tier=args.tier, game=args.game, limit=args.limit), end="")


def main():
    parser = argparse.ArgumentParser(
        description="Arena Rankings CLI — query player ratings and match data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # top
    p = sub.add_parser("top", help="Leaderboard")
    p.add_argument("--game", "-g", default="", help="Game name (empty = all games)")
    p.add_argument("--system", "-s", default="elo", choices=["elo", "glicko2"])
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--min-matches", type=int, default=-1, help=f"Minimum matches played (default: {MIN_MATCHES_GLICKO2} for glicko2, {MIN_MATCHES_ELO} for elo)")
    p.add_argument("--date", "-d", default=None, help="Leaderboard as of this date (YYYY-MM-DD). Uses rating history snapshots.")
    p.add_argument("--sort", default="rating", choices=["rating", "peak"], help="Sort by current rating or all-time peak (default: rating)")

    # player
    p = sub.add_parser("player", help="All ratings for a player")
    p.add_argument("name", help="Player name")
    p.add_argument("--min-matches", type=int, default=-1, help=f"Minimum matches to show (default: {MIN_MATCHES_GLICKO2} for glicko2, {MIN_MATCHES_ELO} for elo)")

    # history
    p = sub.add_parser("history", help="Rating progression")
    p.add_argument("name", help="Player name")
    p.add_argument("--game", "-g", default="")
    p.add_argument("--system", "-s", default="elo", choices=["elo", "glicko2"])
    p.add_argument("--limit", "-n", type=int, default=30)

    # h2h
    p = sub.add_parser("h2h", help="Head-to-head between two players")
    p.add_argument("player1", help="Player 1 name")
    p.add_argument("player2", help="Player 2 name")
    p.add_argument("--game", "-g", default="")
    p.add_argument("--limit", "-n", type=int, default=20)

    # matches
    p = sub.add_parser("matches", help="Recent matches")
    p.add_argument("--game", "-g", default="")
    p.add_argument("--limit", "-n", type=int, default=20)

    # player-matches
    p = sub.add_parser("player-matches", help="Recent matches for a player")
    p.add_argument("name", help="Player name")
    p.add_argument("--game", "-g", default="")
    p.add_argument("--limit", "-n", type=int, default=20)

    # stats
    sub.add_parser("stats", help="Overall system stats")

    # games
    sub.add_parser("games", help="List available games")

    # tournaments
    p = sub.add_parser("tournaments", help="List tournaments")
    p.add_argument("--tier", "-t", default="", choices=["", "premier", "major", "minor"], help="Filter by tier")
    p.add_argument("--game", "-g", default="", help="Filter by game")
    p.add_argument("--limit", "-n", type=int, default=30)

    args = parser.parse_args()
    if hasattr(args, "game") and args.game:
        args.game = resolve_game(args.game)

    with DataProvider() as dx:
        commands = {
            "top": cmd_top,
            "player": cmd_player,
            "history": cmd_history,
            "h2h": cmd_h2h,
            "matches": cmd_matches,
            "player-matches": cmd_player_matches,
            "stats": cmd_stats,
            "games": cmd_games,
            "tournaments": cmd_tournaments,
        }
        commands[args.command](args, dx)


if __name__ == "__main__":
    main()