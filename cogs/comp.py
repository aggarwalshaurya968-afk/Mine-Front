from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, GREEN, RED, ORANGE, GREY, BLUE
import utils.embeds as E
from cogs.access import require_admin_or_owner
# Reuse the exact same gamemode lists/editions Tier Testing already uses so
# both systems stay visually/conceptually consistent. Comp Fight keeps its
# OWN copy of each server's list in comp_gamemodes though, so editing one
# system's gamemodes from its own admin panel never touches the other.
from cogs.tier_test import GAMEMODES_BY_EDITION, EDITIONS

logger = logging.getLogger('TicketBot.comp')

# NOTE: Every admin-only slash command / button in this cog is gated with
# require_admin_or_owner() (shared in cogs/access.py) — same permission
# model as every other cog in this bot.


# ═══════════════════════════════════════════════════════════════════════════════
#  MODES — 1v1 / 2v2 / 3v3 / 4v4, just like the queue systems on big servers.
# ═══════════════════════════════════════════════════════════════════════════════

MODE_META = {
    '1v1': {'team_size': 1, 'emoji': '⚔️', 'style': discord.ButtonStyle.primary},
    '2v2': {'team_size': 2, 'emoji': '🤝', 'style': discord.ButtonStyle.success},
    '3v3': {'team_size': 3, 'emoji': '🔥', 'style': discord.ButtonStyle.danger},
    '4v4': {'team_size': 4, 'emoji': '👑', 'style': discord.ButtonStyle.secondary},
}
MODES = list(MODE_META.keys())
TEAM_SIZE_TO_MODE = {v['team_size']: k for k, v in MODE_META.items()}


def mode_label(team_size: int) -> str:
    return TEAM_SIZE_TO_MODE.get(team_size, f'{team_size}v{team_size}')


# ═══════════════════════════════════════════════════════════════════════════════
#  TUNABLES — anti-dodge + elo
# ═══════════════════════════════════════════════════════════════════════════════

DODGE_THRESHOLD = 3        # dodges within the window before a cooldown kicks in
DODGE_WINDOW_HOURS = 24    # rolling window for counting dodges
DODGE_COOLDOWN_MINUTES = 30  # how long queueing is blocked once threshold is hit
ELO_K_FACTOR = 32
ELO_DEFAULT = 1000
STREAK_FIRE_THRESHOLD = 3  # win streak length before showing the 🔥 badge


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE — fully self-contained tables, only touched from this file.
# ═══════════════════════════════════════════════════════════════════════════════

async def _ensure_tables(bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_settings (
                guild_id                INTEGER PRIMARY KEY,
                java_enabled            INTEGER DEFAULT 1,
                bedrock_enabled         INTEGER DEFAULT 1,
                queue_channel_id        INTEGER,
                challenge_channel_id    INTEGER,
                result_channel_id       INTEGER,
                log_channel_id          INTEGER,
                ping_role_id            INTEGER,
                queue_status_message_id INTEGER,
                season_number           INTEGER DEFAULT 1
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_gamemodes (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                edition  TEXT NOT NULL,
                name     TEXT NOT NULL,
                emoji    TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_players (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                tier_label TEXT DEFAULT 'Unranked',
                wins       INTEGER DEFAULT 0,
                losses     INTEGER DEFAULT 0,
                elo        INTEGER DEFAULT 1000,
                streak     INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, edition, gamemode)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                team_size  INTEGER NOT NULL DEFAULT 1,
                tier_label TEXT DEFAULT 'Unranked',
                queued_at  TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_matches (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id       INTEGER NOT NULL,
                edition        TEXT NOT NULL,
                gamemode       TEXT NOT NULL,
                team_size      INTEGER NOT NULL DEFAULT 1,
                channel_id     INTEGER,
                message_id     INTEGER,
                team1_ids      TEXT NOT NULL,
                team1_tiers    TEXT NOT NULL DEFAULT '{}',
                team2_ids      TEXT NOT NULL,
                team2_tiers    TEXT NOT NULL DEFAULT '{}',
                ready_ids      TEXT NOT NULL DEFAULT '[]',
                status         TEXT DEFAULT 'pending',
                winner_team    INTEGER,
                score          TEXT,
                changelog      TEXT,
                created_at     TEXT DEFAULT (datetime('now')),
                completed_at   TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_tier_roles (
                guild_id   INTEGER NOT NULL,
                tier_label TEXT NOT NULL,
                role_id    INTEGER NOT NULL,
                PRIMARY KEY (guild_id, tier_label)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_dodge_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                match_id INTEGER,
                logged_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                until_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_challenges (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                challenger_id   INTEGER NOT NULL,
                opponent_id     INTEGER NOT NULL,
                edition         TEXT NOT NULL,
                gamemode        TEXT NOT NULL,
                challenger_tier TEXT DEFAULT 'Unranked',
                opponent_tier   TEXT DEFAULT 'Unranked',
                status          TEXT DEFAULT 'pending',
                channel_id      INTEGER,
                message_id      INTEGER,
                match_id        INTEGER,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS comp_season_archive (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                season     INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                edition    TEXT NOT NULL,
                gamemode   TEXT NOT NULL,
                tier_label TEXT,
                wins       INTEGER,
                losses     INTEGER,
                elo        INTEGER,
                streak     INTEGER,
                archived_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        # Lightweight migration for anyone upgrading from the old 1v1-only
        # schema — add the new columns if this table already existed.
        for table, col, ddl in (
            ('comp_queue', 'team_size', "ALTER TABLE comp_queue ADD COLUMN team_size INTEGER NOT NULL DEFAULT 1"),
            ('comp_matches', 'team_size', "ALTER TABLE comp_matches ADD COLUMN team_size INTEGER NOT NULL DEFAULT 1"),
            ('comp_matches', 'team1_ids', "ALTER TABLE comp_matches ADD COLUMN team1_ids TEXT"),
            ('comp_matches', 'team1_tiers', "ALTER TABLE comp_matches ADD COLUMN team1_tiers TEXT NOT NULL DEFAULT '{}'"),
            ('comp_matches', 'team2_ids', "ALTER TABLE comp_matches ADD COLUMN team2_ids TEXT"),
            ('comp_matches', 'team2_tiers', "ALTER TABLE comp_matches ADD COLUMN team2_tiers TEXT NOT NULL DEFAULT '{}'"),
            ('comp_matches', 'ready_ids', "ALTER TABLE comp_matches ADD COLUMN ready_ids TEXT NOT NULL DEFAULT '[]'"),
            ('comp_matches', 'winner_team', "ALTER TABLE comp_matches ADD COLUMN winner_team INTEGER"),
            ('comp_players', 'elo', "ALTER TABLE comp_players ADD COLUMN elo INTEGER DEFAULT 1000"),
            ('comp_players', 'streak', "ALTER TABLE comp_players ADD COLUMN streak INTEGER DEFAULT 0"),
            ('comp_settings', 'season_number', "ALTER TABLE comp_settings ADD COLUMN season_number INTEGER DEFAULT 1"),
        ):
            try:
                await db.execute(ddl)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await db.commit()


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_comp_settings(bot, guild_id: int) -> dict:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_settings WHERE guild_id = ?', (guild_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.commit()
    return {
        'guild_id': guild_id, 'java_enabled': 1, 'bedrock_enabled': 1,
        'queue_channel_id': None, 'challenge_channel_id': None,
        'result_channel_id': None, 'log_channel_id': None,
        'ping_role_id': None, 'queue_status_message_id': None,
        'season_number': 1,
    }


async def set_comp_toggle(bot, guild_id: int, field: str, enabled: bool):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE comp_settings SET {field} = ? WHERE guild_id = ?', (1 if enabled else 0, guild_id))
        await db.commit()


COMP_CHANNEL_FIELDS = ('queue_channel_id', 'challenge_channel_id', 'result_channel_id', 'log_channel_id')


async def set_comp_channel(bot, guild_id: int, field: str, channel_id: int | None):
    if field not in COMP_CHANNEL_FIELDS:
        raise ValueError(f'Unknown comp channel field: {field}')
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(f'UPDATE comp_settings SET {field} = ? WHERE guild_id = ?', (channel_id, guild_id))
        await db.commit()


async def set_comp_ping_role(bot, guild_id: int, role_id: int | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE comp_settings SET ping_role_id = ? WHERE guild_id = ?', (role_id, guild_id))
        await db.commit()


async def set_queue_status_message(bot, guild_id: int, message_id: int | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO comp_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE comp_settings SET queue_status_message_id = ? WHERE guild_id = ?', (message_id, guild_id))
        await db.commit()


# ── Gamemodes (own copy, seeded from tier_test's default lists) ────────────

async def get_comp_gamemodes(bot, guild_id: int, edition: str) -> list[tuple[str, str]]:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT name, emoji FROM comp_gamemodes WHERE guild_id = ? AND edition = ? ORDER BY id',
            (guild_id, edition)
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [(r['name'], r['emoji']) for r in rows]
        defaults = GAMEMODES_BY_EDITION.get(edition, [])
        if defaults:
            await db.executemany(
                'INSERT INTO comp_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
                [(guild_id, edition, name, emoji) for name, emoji in defaults]
            )
            await db.commit()
        return list(defaults)


async def add_comp_gamemode(bot, guild_id: int, edition: str, name: str, emoji: str) -> tuple[bool, str]:
    current = await get_comp_gamemodes(bot, guild_id, edition)
    if len(current) >= 25:
        return False, 'You can have a maximum of **25** gamemodes per edition (Discord dropdown limit).'
    if any(n.lower() == name.lower() for n, _ in current):
        return False, f'**{name}** already exists in {edition}.'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
            (guild_id, edition, name, emoji)
        )
        await db.commit()
    return True, f'Added **{emoji} {name}** to {edition}.'


async def remove_comp_gamemode(bot, guild_id: int, edition: str, name: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'DELETE FROM comp_gamemodes WHERE guild_id = ? AND edition = ? AND name = ?',
            (guild_id, edition, name)
        )
        await db.commit()


async def reset_comp_gamemodes(bot, guild_id: int, edition: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_gamemodes WHERE guild_id = ? AND edition = ?', (guild_id, edition))
        await db.commit()


# ── Players (tier + record, per edition + gamemode — shared across modes) ──

async def get_player(bot, guild_id: int, user_id: int, edition: str, gamemode: str) -> dict:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_players WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
    return {'guild_id': guild_id, 'user_id': user_id, 'edition': edition,
            'gamemode': gamemode, 'tier_label': 'Unranked', 'wins': 0, 'losses': 0,
            'elo': ELO_DEFAULT, 'streak': 0}


async def set_player_tier(bot, guild_id: int, user_id: int, edition: str, gamemode: str, tier_label: str):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, tier_label) VALUES (?,?,?,?,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET tier_label=excluded.tier_label',
            (guild_id, user_id, edition, gamemode, tier_label)
        )
        await db.commit()


def _elo_delta(winner_avg: float, loser_avg: float, k: int = ELO_K_FACTOR) -> int:
    """Standard Elo expected-score formula. Returns the (positive) number of
    points the winning side gains — the losing side drops by the same amount."""
    expected_winner = 1 / (1 + 10 ** ((loser_avg - winner_avg) / 400))
    return max(1, round(k * (1 - expected_winner)))


async def record_match_result(bot, guild_id: int, edition: str, gamemode: str,
                               winner_ids: list[int], loser_ids: list[int],
                               winner_new_tier: str | None, loser_new_tier: str | None) -> int:
    """Applies a win/loss, an Elo adjustment, and a streak update to every
    member of the winning/losing team. If a new tier is supplied it is
    applied to every member of that team (teammates queue individually with
    their own tier, but a team-result tier bump is shared — same as how most
    big-server team ladders handle it). Returns the Elo delta applied."""
    await _ensure_tables(bot)

    winners = [await get_player(bot, guild_id, uid, edition, gamemode) for uid in winner_ids]
    losers = [await get_player(bot, guild_id, uid, edition, gamemode) for uid in loser_ids]
    winner_avg = sum(p['elo'] for p in winners) / len(winners) if winners else ELO_DEFAULT
    loser_avg = sum(p['elo'] for p in losers) / len(losers) if losers else ELO_DEFAULT
    delta = _elo_delta(winner_avg, loser_avg)

    async with aiosqlite.connect(bot.db.db_path) as db:
        for p in winners:
            uid = p['user_id']
            new_streak = p['streak'] + 1 if p['streak'] >= 0 else 1
            new_elo = p['elo'] + delta
            await db.execute(
                'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, wins, elo, streak) '
                'VALUES (?,?,?,?,1,?,?) '
                'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET '
                'wins = wins + 1, elo = ?, streak = ?',
                (guild_id, uid, edition, gamemode, new_elo, new_streak, new_elo, new_streak)
            )
            if winner_new_tier:
                await db.execute(
                    'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                    (winner_new_tier, guild_id, uid, edition, gamemode)
                )
        for p in losers:
            uid = p['user_id']
            new_streak = p['streak'] - 1 if p['streak'] <= 0 else -1
            new_elo = max(0, p['elo'] - delta)
            await db.execute(
                'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, losses, elo, streak) '
                'VALUES (?,?,?,?,1,?,?) '
                'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET '
                'losses = losses + 1, elo = ?, streak = ?',
                (guild_id, uid, edition, gamemode, new_elo, new_streak, new_elo, new_streak)
            )
            if loser_new_tier:
                await db.execute(
                    'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                    (loser_new_tier, guild_id, uid, edition, gamemode)
                )
        await db.commit()
    return delta


# ── Tier roles (auto-assigned Discord role per claimed tier label) ─────────

async def get_tier_roles(bot, guild_id: int) -> dict[str, int]:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT tier_label, role_id FROM comp_tier_roles WHERE guild_id = ?', (guild_id,)) as cur:
            return {r['tier_label']: r['role_id'] for r in await cur.fetchall()}


async def set_tier_role(bot, guild_id: int, tier_label: str, role_id: int):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_tier_roles (guild_id, tier_label, role_id) VALUES (?,?,?) '
            'ON CONFLICT(guild_id, tier_label) DO UPDATE SET role_id=excluded.role_id',
            (guild_id, tier_label, role_id)
        )
        await db.commit()


async def remove_tier_role(bot, guild_id: int, tier_label: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_tier_roles WHERE guild_id=? AND tier_label=?', (guild_id, tier_label))
        await db.commit()


async def sync_tier_roles(bot, guild: discord.Guild, user_ids: list[int]):
    """Gives each member the Discord role mapped to their *highest-value*
    current tier across all edition/gamemode combos and strips every other
    mapped tier role — keeps someone from accumulating every tier role they
    ever touched. Silently skips anyone missing perms/roles/not in guild."""
    mapping = await get_tier_roles(bot, guild.id)
    if not mapping:
        return
    mapped_role_ids = set(mapping.values())
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        for uid in user_ids:
            member = guild.get_member(uid)
            if not member:
                continue
            async with db.execute(
                'SELECT tier_label FROM comp_players WHERE guild_id=? AND user_id=? AND tier_label IS NOT NULL',
                (guild.id, uid)
            ) as cur:
                rows = await cur.fetchall()
            tiers_held = {r['tier_label'] for r in rows}
            target_role_id = None
            for tier_label in tiers_held:
                if tier_label in mapping:
                    target_role_id = mapping[tier_label]
            to_remove = [r for r in member.roles if r.id in mapped_role_ids and r.id != target_role_id]
            try:
                if to_remove:
                    await member.remove_roles(*to_remove, reason='Comp Fight tier role sync')
                if target_role_id and not any(r.id == target_role_id for r in member.roles):
                    role = guild.get_role(target_role_id)
                    if role:
                        await member.add_roles(role, reason='Comp Fight tier role sync')
            except (discord.Forbidden, discord.HTTPException):
                pass


# ── Anti-dodge ───────────────────────────────────────────────────────────

async def record_dodge(bot, guild: discord.Guild, user_id: int, match_id: int | None = None) -> int:
    """Logs a dodge and, if the user has crossed DODGE_THRESHOLD within the
    window, places them on a queueing cooldown. Returns the current dodge
    count within the window (after logging this one)."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_dodge_log (guild_id, user_id, match_id) VALUES (?,?,?)',
            (guild.id, user_id, match_id)
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM comp_dodge_log WHERE guild_id=? AND user_id=? "
            "AND logged_at >= datetime('now', ?)",
            (guild.id, user_id, f'-{DODGE_WINDOW_HOURS} hours')
        ) as cur:
            count = (await cur.fetchone())[0]
        if count >= DODGE_THRESHOLD:
            await db.execute(
                "INSERT INTO comp_cooldowns (guild_id, user_id, until_at) VALUES (?,?, datetime('now', ?)) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET until_at=excluded.until_at",
                (guild.id, user_id, f'+{DODGE_COOLDOWN_MINUTES} minutes')
            )
            await db.commit()
    return count


async def get_cooldown_remaining(bot, guild_id: int, user_id: int) -> int | None:
    """Returns remaining cooldown in whole minutes, or None if not on cooldown."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT until_at FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        until = datetime.fromisoformat(row['until_at'].replace(' ', 'T')).replace(tzinfo=timezone.utc)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            await db.execute('DELETE FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id))
            await db.commit()
            return None
        return max(1, round(remaining / 60))


async def clear_cooldown(bot, guild_id: int, user_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('DELETE FROM comp_cooldowns WHERE guild_id=? AND user_id=?', (guild_id, user_id))
        await db.commit()


# ── Queue ─────────────────────────────────────────────────────────────────

async def get_queue_entry(bot, guild_id: int, user_id: int, edition: str, gamemode: str, team_size: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=? AND team_size=?',
            (guild_id, user_id, edition, gamemode, team_size)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_queue_entries(bot, guild_id: int, user_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND user_id=? ORDER BY queued_at', (guild_id, user_id)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_full_queue(bot, guild_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? ORDER BY edition, gamemode, team_size, queued_at', (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def leave_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str, team_size: int) -> bool:
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute(
            'DELETE FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=? AND team_size=?',
            (guild_id, user_id, edition, gamemode, team_size)
        )
        await db.commit()
        return cur.rowcount > 0


async def purge_queue_by_edition(bot, guild_id: int, edition: str) -> int:
    """Kicks everyone currently queued for an edition out of queue — used
    when staff toggles that edition off, so a closed platform truly
    disappears everywhere instead of leaving stale queue entries behind."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute('DELETE FROM comp_queue WHERE guild_id=? AND edition=?', (guild_id, edition))
        await db.commit()
        return cur.rowcount


async def try_join_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str,
                          team_size: int, tier_label: str):
    """Adds the user to queue for the given mode (team_size). As soon as
    enough players are queued for the same edition + gamemode + mode to fill
    both teams (2 * team_size), the earliest arrivals are pulled out and
    split evenly into Team 1 / Team 2 and a match is created.
    Returns ('queued', None) or ('matched', match_id)."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            'INSERT INTO comp_queue (guild_id, user_id, edition, gamemode, team_size, tier_label) VALUES (?,?,?,?,?,?)',
            (guild_id, user_id, edition, gamemode, team_size, tier_label)
        )
        await db.commit()

        needed = team_size * 2
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND edition=? AND gamemode=? AND team_size=? '
            'ORDER BY queued_at LIMIT ?',
            (guild_id, edition, gamemode, team_size, needed)
        ) as cur:
            waiting = await cur.fetchall()

        if len(waiting) < needed:
            return 'queued', None

        waiting = [dict(r) for r in waiting]
        team1 = waiting[:team_size]
        team2 = waiting[team_size:needed]

        ids_to_remove = [r['id'] for r in waiting]
        await db.executemany('DELETE FROM comp_queue WHERE id = ?', [(i,) for i in ids_to_remove])

        team1_ids = [r['user_id'] for r in team1]
        team2_ids = [r['user_id'] for r in team2]
        team1_tiers = {str(r['user_id']): r['tier_label'] for r in team1}
        team2_tiers = {str(r['user_id']): r['tier_label'] for r in team2}

        cur2 = await db.execute(
            'INSERT INTO comp_matches (guild_id, edition, gamemode, team_size, team1_ids, team1_tiers, '
            'team2_ids, team2_tiers) VALUES (?,?,?,?,?,?,?,?)',
            (guild_id, edition, gamemode, team_size,
             json.dumps(team1_ids), json.dumps(team1_tiers),
             json.dumps(team2_ids), json.dumps(team2_tiers))
        )
        await db.commit()
        return 'matched', cur2.lastrowid


async def get_match(bot, match_id: int) -> dict | None:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_matches WHERE id = ?', (match_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_active_matches(bot, guild_id: int) -> list[dict]:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM comp_matches WHERE guild_id=? AND status IN ('pending','ready') ORDER BY created_at",
            (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_match_ready(bot, match_id: int, user_id: int) -> dict | None:
    match = await get_match(bot, match_id)
    if not match:
        return None
    ready_ids = json.loads(match['ready_ids'])
    if user_id not in ready_ids:
        ready_ids.append(user_id)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('UPDATE comp_matches SET ready_ids = ? WHERE id = ?', (json.dumps(ready_ids), match_id))
        await db.commit()
    if len(ready_ids) >= match['team_size'] * 2 and match['status'] == 'pending':
        async with aiosqlite.connect(bot.db.db_path) as db:
            await db.execute("UPDATE comp_matches SET status='ready' WHERE id=?", (match_id,))
            await db.commit()
    return await get_match(bot, match_id)


async def set_match_message(bot, match_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('UPDATE comp_matches SET channel_id=?, message_id=? WHERE id=?',
                          (channel_id, message_id, match_id))
        await db.commit()


async def cancel_match(bot, match_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute("UPDATE comp_matches SET status='cancelled' WHERE id=?", (match_id,))
        await db.commit()


async def cancel_active_matches_by_edition(bot, guild: discord.Guild, edition: str) -> int:
    """Force-cancels every live match for an edition and edits the posted
    match messages to reflect it — used when staff toggles that edition off,
    so nothing keeps running on a platform that was just closed."""
    matches = await get_active_matches(bot, guild.id)
    targeted = [m for m in matches if m['edition'] == edition]
    for m in targeted:
        await cancel_match(bot, m['id'])
        if m.get('channel_id') and m.get('message_id'):
            channel = guild.get_channel(m['channel_id'])
            if channel:
                try:
                    msg = await channel.fetch_message(m['message_id'])
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.color = RED
                        if not embed.title.startswith('❌'):
                            embed.title = f'❌  {embed.title}'
                        embed.set_field_at(2, name='📶  Status', value=f'Cancelled — **{edition}** was disabled by staff.', inline=False)
                        await msg.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, IndexError):
                    pass
    return len(targeted)


async def complete_match(bot, match_id: int, winner_team: int, score: str, changelog: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            "UPDATE comp_matches SET status='completed', winner_team=?, score=?, changelog=?, "
            "completed_at=datetime('now') WHERE id=?",
            (winner_team, score, changelog, match_id)
        )
        await db.commit()


async def get_match_history(bot, guild_id: int, user_id: int, limit: int = 10) -> list[dict]:
    """Completed matches a user took part in, newest first. Filtered in
    Python since team ids are stored as a JSON list, not a queryable column."""
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM comp_matches WHERE guild_id=? AND status='completed' ORDER BY completed_at DESC LIMIT 200",
            (guild_id,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    out = []
    for m in rows:
        ids = json.loads(m['team1_ids']) + json.loads(m['team2_ids'])
        if user_id in ids:
            out.append(m)
        if len(out) >= limit:
            break
    return out


# ── Leaderboard ──────────────────────────────────────────────────────────

async def get_leaderboard(bot, guild_id: int, edition: str, gamemode: str, limit: int = 10) -> list[dict]:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_players WHERE guild_id=? AND edition=? AND gamemode=? '
            'ORDER BY elo DESC, wins DESC LIMIT ?',
            (guild_id, edition, gamemode, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Season reset ─────────────────────────────────────────────────────────

async def do_season_reset(bot, guild_id: int) -> int:
    """Archives every player's current stats into comp_season_archive, then
    resets wins/losses/elo/streak/tier for a fresh season. Returns the new
    season number."""
    await _ensure_tables(bot)
    settings = await get_comp_settings(bot, guild_id)
    season = settings.get('season_number', 1)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_players WHERE guild_id=?', (guild_id,)) as cur:
            players = [dict(r) for r in await cur.fetchall()]
        for p in players:
            await db.execute(
                'INSERT INTO comp_season_archive (guild_id, season, user_id, edition, gamemode, '
                'tier_label, wins, losses, elo, streak) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (guild_id, season, p['user_id'], p['edition'], p['gamemode'],
                 p['tier_label'], p['wins'], p['losses'], p['elo'], p['streak'])
            )
        await db.execute(
            "UPDATE comp_players SET wins=0, losses=0, elo=?, streak=0, tier_label='Unranked' WHERE guild_id=?",
            (ELO_DEFAULT, guild_id)
        )
        await db.execute(
            'UPDATE comp_settings SET season_number = season_number + 1 WHERE guild_id=?', (guild_id,)
        )
        await db.commit()
    return season + 1


# ── Direct challenges (named 1v1 challenge, outside the queue) ─────────────

async def create_challenge(bot, guild_id: int, challenger_id: int, opponent_id: int,
                            edition: str, gamemode: str, challenger_tier: str) -> int:
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute(
            'INSERT INTO comp_challenges (guild_id, challenger_id, opponent_id, edition, gamemode, challenger_tier) '
            'VALUES (?,?,?,?,?,?)',
            (guild_id, challenger_id, opponent_id, edition, gamemode, challenger_tier)
        )
        await db.commit()
        return cur.lastrowid


async def get_challenge(bot, challenge_id: int) -> dict | None:
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM comp_challenges WHERE id=?', (challenge_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_challenge_message(bot, challenge_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('UPDATE comp_challenges SET channel_id=?, message_id=? WHERE id=?',
                          (channel_id, message_id, challenge_id))
        await db.commit()


async def resolve_challenge(bot, challenge_id: int, status: str, opponent_tier: str | None = None,
                             match_id: int | None = None):
    async with aiosqlite.connect(bot.db.db_path) as db:
        if opponent_tier is not None:
            await db.execute(
                'UPDATE comp_challenges SET status=?, opponent_tier=?, match_id=? WHERE id=?',
                (status, opponent_tier, match_id, challenge_id)
            )
        else:
            await db.execute('UPDATE comp_challenges SET status=? WHERE id=?', (status, challenge_id))
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  EMBEDS
# ═══════════════════════════════════════════════════════════════════════════════

def queue_join_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title=f'⚔️  {guild.name} — Comp Fight Queue',
        description=(
            'Looking for a **Comp Fight**? Hop in the queue below — '
            '**1v1, 2v2, 3v3 or 4v4**, your pick.\n\n'
            '**How it works:**\n'
            '1️⃣ Click **Join Queue**, pick your platform, mode, and game mode\n'
            '2️⃣ Tell us your current claimed tier\n'
            '3️⃣ The moment enough players queue for the same mode, both '
            'teams are matched — everyone gets posted together\n'
            '4️⃣ All players hit **Ready**, play the set, then staff logs '
            'the result and updates tiers\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.set_footer(text=f'{guild.name} • Comp Fight System')
    return e


def match_found_embed(guild: discord.Guild, match: dict, gamemode_emoji: str) -> discord.Embed:
    short = 'Bedrock' if match['edition'] == 'Bedrock Edition' else 'Java'
    mode = mode_label(match['team_size'])
    team1_ids = json.loads(match['team1_ids'])
    team2_ids = json.loads(match['team2_ids'])
    team1_tiers = json.loads(match['team1_tiers'])
    team2_tiers = json.loads(match['team2_tiers'])

    team1_lines = '\n'.join(f'<@{uid}> — `{team1_tiers.get(str(uid), "Unranked")}`' for uid in team1_ids)
    team2_lines = '\n'.join(f'<@{uid}> — `{team2_tiers.get(str(uid), "Unranked")}`' for uid in team2_ids)
    vs_line = f'{" ".join(f"<@{u}>" for u in team1_ids)} **VS** {" ".join(f"<@{u}>" for u in team2_ids)}'

    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} — {mode} Comp Match Found',
        description=(
            f'{vs_line}\n\n'
            f'**Platform:** {short} Edition   •   **Mode:** {mode}\n'
            'Every player must click **✅ Ready** below. Once the whole lobby is '
            'ready, hop in-game and play your set — staff will log the result here once it\'s done.'
        ),
        color=BLUE,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name='🔹  Team 1', value=team1_lines or '_—_', inline=True)
    e.add_field(name='🔸  Team 2', value=team2_lines or '_—_', inline=True)
    e.add_field(name='📶  Ready Status', value=f'⏳ 0/{match["team_size"] * 2} ready — waiting on everyone...', inline=False)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
    return e


def result_embed(guild: discord.Guild, match: dict, winner_team: int, score: str,
                  gamemode_emoji: str, changelog: str, elo_delta: int | None = None) -> discord.Embed:
    mode = mode_label(match['team_size'])
    team1_ids = json.loads(match['team1_ids'])
    team2_ids = json.loads(match['team2_ids'])
    winner_ids = team1_ids if winner_team == 1 else team2_ids
    loser_ids = team2_ids if winner_team == 1 else team1_ids

    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} — {mode} Comp Result',
        color=GREEN,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_author(name=f'Team {winner_team} defeated Team {2 if winner_team == 1 else 1}')
    e.add_field(name='📊  Score', value=f'**{score}**', inline=True)
    e.add_field(name='🏆  Winning Team', value=', '.join(f'<@{u}>' for u in winner_ids), inline=True)
    e.add_field(name='💀  Losing Team', value=', '.join(f'<@{u}>' for u in loser_ids), inline=True)
    if elo_delta:
        e.add_field(name='📈  ELO Change', value=f'Winners **+{elo_delta}**  •  Losers **-{elo_delta}**', inline=False)
    if changelog:
        e.add_field(name='📝  Changelog', value=changelog, inline=False)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
    return e


def streak_badge(streak: int) -> str:
    if streak >= STREAK_FIRE_THRESHOLD:
        return f' 🔥{streak}'
    if streak >= 1:
        return f' (W{streak})'
    if streak <= -STREAK_FIRE_THRESHOLD:
        return f' 🧊{abs(streak)}'
    if streak <= -1:
        return f' (L{abs(streak)})'
    return ''


def leaderboard_embed(guild: discord.Guild, edition: str, gamemode: str, rows: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title=f'🏅  Leaderboard — {gamemode} ({edition})',
        color=ORANGE,
        timestamp=datetime.now(timezone.utc)
    )
    if not rows:
        e.description = '_No ranked players yet for this mode._'
    else:
        medals = ['🥇', '🥈', '🥉']
        lines = []
        for i, p in enumerate(rows):
            medal = medals[i] if i < 3 else f'`#{i+1}`'
            lines.append(
                f'{medal} <@{p["user_id"]}> — **{p["elo"]} ELO** • `{p["tier_label"]}` • '
                f'{p["wins"]}W/{p["losses"]}L{streak_badge(p["streak"])}'
            )
        e.description = '\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Comp Fight Leaderboard')
    return e


def history_embed(guild: discord.Guild, member: discord.abc.User, matches: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title=f'📜  Match History — {member.display_name}',
        color=BLUE,
        timestamp=datetime.now(timezone.utc)
    )
    if not matches:
        e.description = '_No completed matches yet._'
    else:
        lines = []
        for m in matches:
            team1_ids = json.loads(m['team1_ids'])
            team2_ids = json.loads(m['team2_ids'])
            on_team1 = member.id in team1_ids
            won = (m['winner_team'] == 1 and on_team1) or (m['winner_team'] == 2 and not on_team1)
            result = '🟢 Won' if won else '🔴 Lost'
            opp_ids = team2_ids if on_team1 else team1_ids
            opp = ', '.join(f'<@{u}>' for u in opp_ids) or '_—_'
            lines.append(
                f'**#{m["id"]}** {m["gamemode"]} ({mode_label(m["team_size"])}) — {result} • '
                f'`{m["score"]}` vs {opp}'
            )
        e.description = '\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Last {len(matches)} completed match(es)')
    return e


def challenge_embed(guild: discord.Guild, challenge: dict, gamemode_emoji: str) -> discord.Embed:
    short = 'Bedrock' if challenge['edition'] == 'Bedrock Edition' else 'Java'
    e = discord.Embed(
        title=f'{gamemode_emoji}  {challenge["gamemode"]} Comp Challenge',
        description=(
            f'<@{challenge["challenger_id"]}> has challenged <@{challenge["opponent_id"]}> for a Comp Fight!\n\n'
            f'**Platform:** {short} Edition\n'
            f'**{challenge["challenger_tier"]}** claimed by <@{challenge["challenger_id"]}>\n\n'
            f'<@{challenge["opponent_id"]}>, click **Accept** below to lock in the match, or **Decline** to pass.'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text=f'{guild.name} • Comp Fight • Challenge #{challenge["id"]}')
    return e


def queue_status_embed(guild: discord.Guild, entries: list[dict]) -> discord.Embed:
    e = discord.Embed(
        title='📡  Live Comp Queue',
        color=ORANGE,
        timestamp=datetime.now(timezone.utc)
    )
    if not entries:
        e.description = '_Nobody is currently queued. Be the first — click **Join Queue** above!_'
    else:
        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            key = f'{entry["edition"]} • {entry["gamemode"]} • {mode_label(entry["team_size"])}'
            grouped.setdefault(key, []).append(entry)
        lines = []
        for key, group in grouped.items():
            team_size = group[0]['team_size']
            names = ', '.join(f'<@{g["user_id"]}> (`{g["tier_label"]}`)' for g in group)
            lines.append(f'**{key}**  —  {len(group)}/{team_size * 2} queued\n{names}')
        e.description = '\n\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Updates live as players queue')
    return e


def comp_admin_embed(guild: discord.Guild, settings: dict, java_gm: list, bedrock_gm: list,
                      active_count: int, queued_count: int, tier_role_count: int = 0) -> discord.Embed:
    java = '✅ Open' if settings.get('java_enabled', 1) else '❌ Closed'
    bedrock = '✅ Open' if settings.get('bedrock_enabled', 1) else '❌ Closed'

    def ch_text(ch_id):
        return f'<#{ch_id}>' if ch_id else '_Not set_'

    e = discord.Embed(
        title='🛠️  Comp Fight — Admin Panel',
        description=(
            f'Full control over the Comp Fight system on **{guild.name}**.\n'
            'Supports **1v1 / 2v2 / 3v3 / 4v4** on both editions.\n'
            'Use the buttons below to manage every part of it.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━'
        ),
        color=PURPLE_DARK,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)

    e.add_field(name='🟩  Java Edition', value=java, inline=True)
    e.add_field(name='🟦  Bedrock Edition', value=bedrock, inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.add_field(name='🟩  Java Gamemodes', value=f'{len(java_gm)}/25 configured', inline=True)
    e.add_field(name='🟦  Bedrock Gamemodes', value=f'{len(bedrock_gm)}/25 configured', inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.add_field(name='⚔️  Active Matches', value=str(active_count), inline=True)
    e.add_field(name='⏳  Players Queued', value=str(queued_count), inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)

    ping_role_id = settings.get('ping_role_id')
    e.add_field(
        name='🔔  Match Ping Role',
        value=f'<@&{ping_role_id}>' if ping_role_id else '_Not set — no role pinged on new matches_',
        inline=False
    )
    e.add_field(
        name='📡  Comp Fight Channels',
        value=(
            f'**Queue:** {ch_text(settings.get("queue_channel_id"))}\n'
            f'**Challenge/Match:** {ch_text(settings.get("challenge_channel_id"))}\n'
            f'**Result:** {ch_text(settings.get("result_channel_id"))}\n'
            f'**Logs:** {ch_text(settings.get("log_channel_id"))}'
        ),
        inline=False
    )
    e.add_field(
        name='🧩  Modes',
        value='  '.join(f'{MODE_META[m]["emoji"]} {m}' for m in MODES),
        inline=False
    )
    e.add_field(name='📅  Season', value=f'Season **{settings.get("season_number", 1)}**', inline=True)
    e.add_field(name='🎭  Tier Roles', value=f'{tier_role_count} mapped', inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.set_footer(text='Mine Front • Comp Fight Admin Panel • Toggling an edition off closes its queue & matches instantly')
    return e


def gamemode_manage_embed(edition: str, gamemodes: list[tuple[str, str]]) -> discord.Embed:
    lines = '\n'.join(f'{emoji} **{name}**' for name, emoji in gamemodes) or '_No gamemodes configured._'
    e = discord.Embed(
        title=f'🎮  Manage {edition} Comp Gamemodes',
        description=(
            f'{lines}\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            'Add a new gamemode, remove one from the dropdown, or reset back '
            'to the built-in default list.\n'
            '_Gamemodes are shared across all modes (1v1–4v4) on this edition._'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_footer(text=f'Mine Front • {len(gamemodes)}/25 gamemodes')
    return e


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC QUEUE PANEL  (persistent — Join Queue / Leave Queue)
# ═══════════════════════════════════════════════════════════════════════════════

async def _refresh_queue_status(bot, guild: discord.Guild):
    """Edits (or posts) the single live 'who's queued' embed in the queue
    channel so it always reflects the current queue without spamming."""
    settings = await get_comp_settings(bot, guild.id)
    ch_id = settings.get('queue_channel_id')
    if not ch_id:
        return
    channel = guild.get_channel(ch_id)
    if not channel:
        return
    entries = await get_full_queue(bot, guild.id)
    embed = queue_status_embed(guild, entries)

    msg_id = settings.get('queue_status_message_id')
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        msg = await channel.send(embed=embed)
        await set_queue_status_message(bot, guild.id, msg.id)
    except discord.HTTPException:
        pass


async def _post_match(bot, guild: discord.Guild, match_id: int):
    match = await get_match(bot, match_id)
    if not match:
        return
    settings = await get_comp_settings(bot, guild.id)
    ch_id = settings.get('challenge_channel_id') or settings.get('queue_channel_id')
    channel = guild.get_channel(ch_id) if ch_id else None
    if not channel:
        return

    gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
    emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')

    embed = match_found_embed(guild, match, emoji)
    view = CompMatchView(bot, match_id)
    ping_role_id = settings.get('ping_role_id')
    all_ids = json.loads(match['team1_ids']) + json.loads(match['team2_ids'])
    content = ' '.join(f'<@{uid}>' for uid in all_ids)
    if ping_role_id:
        content += f' <@&{ping_role_id}>'

    try:
        msg = await channel.send(
            content=content, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True)
        )
        await set_match_message(bot, match_id, channel.id, msg.id)
    except discord.HTTPException:
        return

    # Best-effort DM to every matched player — channel ping isn't always
    # seen fast enough, so this backs it up. Failures (DMs closed) are fine.
    for uid in all_ids:
        member = guild.get_member(uid)
        if not member:
            continue
        try:
            dm_embed = E.base(
                '⚔️  Comp Fight Match Found!',
                f'Your **{match["gamemode"]}** ({mode_label(match["team_size"])}) match on '
                f'**{"Bedrock" if match["edition"] == "Bedrock Edition" else "Java"} Edition** is ready in '
                f'**{guild.name}**.\n\n[Jump to the match]({msg.jump_url})',
                color=BLUE
            )
            await member.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


class CompJoinModal(discord.ui.Modal):
    def __init__(self, bot, edition: str, mode: str, gamemode: str):
        super().__init__(title=f'⚔️  Join {gamemode} ({mode}) Queue', timeout=300)
        self.bot = bot
        self.edition = edition
        self.mode = mode
        self.team_size = MODE_META[mode]['team_size']
        self.gamemode = gamemode
        self.tier = discord.ui.TextInput(
            label='Your Current / Claimed Tier',
            placeholder='e.g. HT3, LT5, or "Unranked"',
            required=True,
            max_length=50,
        )
        self.add_item(self.tier)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        guild = interaction.guild
        tier_label = self.tier.value.strip() or 'Unranked'

        # An edition can be disabled between opening this modal and
        # submitting it — re-check so nobody sneaks into a closed queue.
        settings = await get_comp_settings(bot, guild.id)
        field = 'bedrock_enabled' if self.edition == 'Bedrock Edition' else 'java_enabled'
        if not bool(settings.get(field, 1)):
            return await interaction.followup.send(
                embed=E.error(f'**{self.edition}** Comp Fight is currently closed.'), ephemeral=True)

        remaining = await get_cooldown_remaining(bot, guild.id, interaction.user.id)
        if remaining:
            return await interaction.followup.send(
                embed=E.error(f'You dodged too many matches recently — you\'re on a queue cooldown for '
                               f'**{remaining} more minute(s)**.'), ephemeral=True)

        existing = await get_queue_entry(bot, guild.id, interaction.user.id, self.edition, self.gamemode, self.team_size)
        if existing:
            return await interaction.followup.send(
                embed=E.error(f'You are already queued for **{self.gamemode}** ({self.mode}).'), ephemeral=True)

        await set_player_tier(bot, guild.id, interaction.user.id, self.edition, self.gamemode, tier_label)
        await sync_tier_roles(bot, guild, [interaction.user.id])
        status, match_id = await try_join_queue(
            bot, guild.id, interaction.user.id, self.edition, self.gamemode, self.team_size, tier_label)

        if status == 'matched':
            await _post_match(bot, guild, match_id)
            await _refresh_queue_status(bot, guild)
            settings = await get_comp_settings(bot, guild.id)
            ch_id = settings.get('challenge_channel_id') or settings.get('queue_channel_id')
            ch_text = f'<#{ch_id}>' if ch_id else 'the match channel'
            await interaction.followup.send(
                embed=E.success(f'Lobby filled! Your **{self.gamemode}** ({self.mode}) match has been posted in {ch_text}.'),
                ephemeral=True)
        else:
            await _refresh_queue_status(bot, guild)
            await interaction.followup.send(
                embed=E.success(
                    f'You\'ve joined the **{self.gamemode}** ({self.mode}) queue at tier **{tier_label}**. '
                    'You\'ll be pinged the moment the lobby fills up.'
                ), ephemeral=True)


class CompGamemodeSelectView(discord.ui.View):
    def __init__(self, bot, edition: str, mode: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition
        self.mode = mode
        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, emoji in gamemodes][:25]
        self.gamemode_select.options = options
        self.gamemode_select.placeholder = f'Select the {short} {mode} game mode to queue for…'

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = select.values[0]
        await interaction.response.send_modal(CompJoinModal(self.bot, self.edition, self.mode, gamemode))


class CompModeSelectView(discord.ui.View):
    """Second step of Join Queue — pick 1v1 / 2v2 / 3v3 / 4v4."""

    def __init__(self, bot, edition: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition
        for mode in MODES:
            meta = MODE_META[mode]
            button = discord.ui.Button(label=mode, emoji=meta['emoji'], style=meta['style'], row=0)
            button.callback = self._callback_for(mode)
            self.add_item(button)

    def _callback_for(self, mode: str):
        async def callback(interaction: discord.Interaction):
            await self._pick(interaction, mode)
        return callback

    async def _pick(self, interaction: discord.Interaction, mode: str):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, self.edition)
        if not gamemodes:
            return await interaction.response.send_message(
                embed=E.error(f'No gamemodes are configured for {self.edition} yet. Ask an admin to add some via /compadminpanel.'),
                ephemeral=True)
        short = 'Bedrock' if self.edition == 'Bedrock Edition' else 'Java'
        embed = discord.Embed(
            title=f'{MODE_META[mode]["emoji"]}  {short} {mode} — Select Game Mode',
            description='Choose the game mode you want to queue for:',
            color=PURPLE,
        )
        await interaction.response.send_message(
            embed=embed, view=CompGamemodeSelectView(self.bot, self.edition, mode, gamemodes), ephemeral=True)


class CompPlatformSelectView(discord.ui.View):
    def __init__(self, bot, java_enabled: bool = True, bedrock_enabled: bool = True):
        super().__init__(timeout=120)
        self.bot = bot
        if not bedrock_enabled:
            self.remove_item(self.bedrock)
        if not java_enabled:
            self.remove_item(self.java)

    async def _pick(self, interaction: discord.Interaction, edition: str):
        # Re-check in case staff toggled the edition off after this view was posted.
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        field = 'bedrock_enabled' if edition == 'Bedrock Edition' else 'java_enabled'
        if not bool(settings.get(field, 1)):
            return await interaction.response.send_message(
                embed=E.error(f'**{edition}** Comp Fight is currently closed.'), ephemeral=True)
        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        platform_emoji = '🟢' if edition == 'Bedrock Edition' else '🍲'
        embed = discord.Embed(
            title=f'{platform_emoji}  {short} — Select Mode',
            description='Which mode do you want to queue for — **1v1**, **2v2**, **3v3**, or **4v4**?',
            color=PURPLE,
        )
        await interaction.response.send_message(embed=embed, view=CompModeSelectView(self.bot, edition), ephemeral=True)

    @discord.ui.button(label='Bedrock', emoji='🟢', style=discord.ButtonStyle.success)
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 'Bedrock Edition')

    @discord.ui.button(label='Java', emoji='🍲', style=discord.ButtonStyle.primary)
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 'Java Edition')


class LeaveQueueSelectView(discord.ui.View):
    def __init__(self, bot, entries: list[dict]):
        super().__init__(timeout=120)
        self.bot = bot
        self.leave_select.options = [
            discord.SelectOption(
                label=f'{e["edition"]} • {e["gamemode"]} • {mode_label(e["team_size"])}',
                value=f'{e["edition"]}||{e["gamemode"]}||{e["team_size"]}'
            )
            for e in entries[:25]
        ]

    @discord.ui.select(placeholder='Select a queue to leave…')
    async def leave_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        edition, gamemode, team_size = select.values[0].split('||', 2)
        ok = await leave_queue(self.bot, interaction.guild_id, interaction.user.id, edition, gamemode, int(team_size))
        await _refresh_queue_status(self.bot, interaction.guild)
        if ok:
            await interaction.response.send_message(
                embed=E.success(f'Left the **{gamemode}** ({mode_label(int(team_size))}) queue.'), ephemeral=True)
        else:
            await interaction.response.send_message(embed=E.error('You were not in that queue.'), ephemeral=True)


class CompQueuePanelView(discord.ui.View):
    """Public, persistent panel posted via /comppanel."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Join Queue', emoji='⚔️', style=discord.ButtonStyle.primary, custom_id='comp:queue:join')
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_on = bool(settings.get('java_enabled', 1))
        bedrock_on = bool(settings.get('bedrock_enabled', 1))
        if not java_on and not bedrock_on:
            return await interaction.response.send_message(
                embed=E.error('Comp Fight queueing is currently closed for both editions.'), ephemeral=True)
        embed = discord.Embed(
            title='⚔️  Comp Fight — Select Platform',
            description='Which platform do you want to queue on — **Bedrock** or **Java**?',
            color=PURPLE,
        )
        await interaction.response.send_message(
            embed=embed, view=CompPlatformSelectView(self.bot, java_on, bedrock_on), ephemeral=True)

    @discord.ui.button(label='Leave Queue', emoji='🚪', style=discord.ButtonStyle.secondary, custom_id='comp:queue:leave')
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = await get_user_queue_entries(self.bot, interaction.guild_id, interaction.user.id)
        if not entries:
            return await interaction.response.send_message(
                embed=E.error('You are not currently in any Comp Fight queue.'), ephemeral=True)
        await interaction.response.send_message(
            embed=E.base('🚪  Leave Queue', 'Pick which queue to leave:', color=ORANGE),
            view=LeaveQueueSelectView(self.bot, entries), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MATCH VIEW  (Ready / Cancel / Stat buttons on the posted match embed)
# ═══════════════════════════════════════════════════════════════════════════════

class CompMatchView(discord.ui.View):
    """Not persistent across bot restarts by design (matches are short-lived
    and staff can re-post via /compadminpanel > Active Matches if needed)."""

    def __init__(self, bot, match_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.match_id = match_id

    def _is_participant(self, user_id: int, match: dict) -> bool:
        return user_id in json.loads(match['team1_ids']) + json.loads(match['team2_ids'])

    @discord.ui.button(label='Ready', emoji='✅', style=discord.ButtonStyle.success, custom_id='comp:match:ready')
    async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match or match['status'] not in ('pending', 'ready'):
            return await interaction.response.send_message(embed=E.error('This match is no longer active.'), ephemeral=True)
        if not self._is_participant(interaction.user.id, match):
            return await interaction.response.send_message(
                embed=E.error('Only the matched players can mark themselves ready.'), ephemeral=True)

        already = interaction.user.id in json.loads(match['ready_ids'])
        match = await set_match_ready(self.bot, self.match_id, interaction.user.id)
        total = match['team_size'] * 2
        ready_ids = json.loads(match['ready_ids'])

        if match['status'] == 'ready':
            status_text = f'🟢 All {total} players ready! Play your set — staff will log the result shortly.'
        else:
            status_text = f'⏳ {len(ready_ids)}/{total} ready — waiting on {total - len(ready_ids)} more...'

        embed = interaction.message.embeds[0]
        embed.set_field_at(2, name='📶  Ready Status', value=status_text, inline=False)
        await interaction.response.edit_message(embed=embed)
        if already:
            await interaction.followup.send(embed=E.base('✅  Already marked ready.', color=GREY), ephemeral=True)

    @discord.ui.button(label='Cancel Match', emoji='❌', style=discord.ButtonStyle.danger, custom_id='comp:match:cancel')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match or match['status'] in ('completed', 'cancelled'):
            return await interaction.response.send_message(embed=E.error('This match is no longer active.'), ephemeral=True)
        from cogs.access import is_admin_or_owner
        is_participant = self._is_participant(interaction.user.id, match)
        if not is_participant and not await is_admin_or_owner(self.bot, interaction):
            return await interaction.response.send_message(
                embed=E.error('Only the matched players or staff can cancel this match.'), ephemeral=True)

        dodge_note = ''
        # A participant bailing before the whole lobby was ready counts as a
        # dodge; staff force-cancels and post-ready cancels don't.
        if is_participant and match['status'] == 'pending':
            count = await record_dodge(self.bot, interaction.guild, interaction.user.id, self.match_id)
            if count >= DODGE_THRESHOLD:
                dodge_note = (f'\n⚠️ {interaction.user.mention} has dodged **{count}** matches in the last '
                               f'{DODGE_WINDOW_HOURS}h and is now on a **{DODGE_COOLDOWN_MINUTES}-minute** queue cooldown.')

        await cancel_match(self.bot, self.match_id)
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = RED
        embed.title = f'❌  {embed.title}'
        embed.set_field_at(2, name='📶  Status', value=f'Cancelled by {interaction.user.mention}.{dodge_note}', inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='Stats', emoji='📊', style=discord.ButtonStyle.secondary, custom_id='comp:match:stats')
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match:
            return await interaction.response.send_message(embed=E.error('Match not found.'), ephemeral=True)

        async def team_block(ids):
            lines = []
            for uid in ids:
                p = await get_player(self.bot, match['guild_id'], uid, match['edition'], match['gamemode'])
                lines.append(f'<@{uid}> — **{p["tier_label"]}** ({p["wins"]}W/{p["losses"]}L) '
                              f'`{p["elo"]} ELO`{streak_badge(p["streak"])}')
            return '\n'.join(lines) or '_—_'

        team1_ids = json.loads(match['team1_ids'])
        team2_ids = json.loads(match['team2_ids'])
        e = E.base(f'📊  {match["gamemode"]} — {mode_label(match["team_size"])} Stats', color=BLUE)
        e.add_field(name='🔹  Team 1', value=await team_block(team1_ids), inline=True)
        e.add_field(name='🔸  Team 2', value=await team_block(team2_ids), inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  DIRECT CHALLENGE  (named 1v1 challenge, outside the auto-match queue)
# ═══════════════════════════════════════════════════════════════════════════════

class ChallengeOpponentTierModal(discord.ui.Modal):
    """Shown to the opponent when they hit Accept — locks in their tier and
    turns the challenge into a real match."""

    def __init__(self, bot, challenge: dict):
        super().__init__(title='⚔️  Accept Comp Challenge', timeout=300)
        self.bot = bot
        self.challenge = challenge
        self.tier = discord.ui.TextInput(
            label='Your Current / Claimed Tier',
            placeholder='e.g. HT3, LT5, or "Unranked"',
            required=True, max_length=50,
        )
        self.add_item(self.tier)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot, guild, challenge = self.bot, interaction.guild, self.challenge
        opponent_tier = self.tier.value.strip() or 'Unranked'

        await set_player_tier(bot, guild.id, challenge['opponent_id'], challenge['edition'], challenge['gamemode'], opponent_tier)

        team1_ids = [challenge['challenger_id']]
        team2_ids = [challenge['opponent_id']]
        team1_tiers = {str(challenge['challenger_id']): challenge['challenger_tier']}
        team2_tiers = {str(challenge['opponent_id']): opponent_tier}
        async with aiosqlite.connect(bot.db.db_path) as db:
            cur = await db.execute(
                'INSERT INTO comp_matches (guild_id, edition, gamemode, team_size, team1_ids, team1_tiers, '
                'team2_ids, team2_tiers) VALUES (?,?,?,1,?,?,?,?)',
                (guild.id, challenge['edition'], challenge['gamemode'],
                 json.dumps(team1_ids), json.dumps(team1_tiers), json.dumps(team2_ids), json.dumps(team2_tiers))
            )
            await db.commit()
            match_id = cur.lastrowid

        await resolve_challenge(bot, challenge['id'], 'accepted', opponent_tier, match_id)
        await _post_match(bot, guild, match_id)

        if challenge.get('channel_id') and challenge.get('message_id'):
            ch = guild.get_channel(challenge['channel_id'])
            if ch:
                try:
                    msg = await ch.fetch_message(challenge['message_id'])
                    embed = msg.embeds[0]
                    embed.color = GREEN
                    embed.title = f'✅  {embed.title} — Accepted'
                    await msg.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, IndexError):
                    pass

        await interaction.followup.send(embed=E.success('Challenge accepted! Your match has been posted.'), ephemeral=True)


class ChallengeActionView(discord.ui.View):
    """Posted publicly on a direct-challenge embed. Accept/Decline are
    opponent-only; the Stat buttons work for anyone."""

    def __init__(self, bot, challenge_id: int, challenger_name: str, opponent_name: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.challenge_id = challenge_id
        self.stat_challenger.label = f'{challenger_name[:20]} Stat'
        self.stat_opponent.label = f'{opponent_name[:20]} Stat'

    async def _get(self):
        return await get_challenge(self.bot, self.challenge_id)

    @discord.ui.button(label='Accept', emoji='✅', style=discord.ButtonStyle.success, row=0)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = await self._get()
        if not challenge or challenge['status'] != 'pending':
            return await interaction.response.send_message(embed=E.error('This challenge is no longer active.'), ephemeral=True)
        if interaction.user.id != challenge['opponent_id']:
            return await interaction.response.send_message(
                embed=E.error('Only the challenged player can accept this.'), ephemeral=True)
        await interaction.response.send_modal(ChallengeOpponentTierModal(self.bot, challenge))

    @discord.ui.button(label='Decline', emoji='❌', style=discord.ButtonStyle.danger, row=0)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = await self._get()
        if not challenge or challenge['status'] != 'pending':
            return await interaction.response.send_message(embed=E.error('This challenge is no longer active.'), ephemeral=True)
        if interaction.user.id != challenge['opponent_id']:
            return await interaction.response.send_message(
                embed=E.error('Only the challenged player can decline this.'), ephemeral=True)
        await resolve_challenge(self.bot, challenge['id'], 'declined')
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = RED
        embed.title = f'❌  {embed.title} — Declined'
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='Challenger Stat', emoji='📊', style=discord.ButtonStyle.secondary, row=1)
    async def stat_challenger(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = await self._get()
        if not challenge:
            return await interaction.response.send_message(embed=E.error('Challenge not found.'), ephemeral=True)
        p = await get_player(self.bot, challenge['guild_id'], challenge['challenger_id'], challenge['edition'], challenge['gamemode'])
        e = E.base(f'📊  <@{challenge["challenger_id"]}> — {challenge["gamemode"]}',
                    f'**Tier:** {p["tier_label"]}\n**Record:** {p["wins"]}W/{p["losses"]}L\n'
                    f'**ELO:** {p["elo"]}{streak_badge(p["streak"])}', color=BLUE)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label='Opponent Stat', emoji='📊', style=discord.ButtonStyle.secondary, row=1)
    async def stat_opponent(self, interaction: discord.Interaction, button: discord.ui.Button):
        challenge = await self._get()
        if not challenge:
            return await interaction.response.send_message(embed=E.error('Challenge not found.'), ephemeral=True)
        p = await get_player(self.bot, challenge['guild_id'], challenge['opponent_id'], challenge['edition'], challenge['gamemode'])
        e = E.base(f'📊  <@{challenge["opponent_id"]}> — {challenge["gamemode"]}',
                    f'**Tier:** {p["tier_label"]}\n**Record:** {p["wins"]}W/{p["losses"]}L\n'
                    f'**ELO:** {p["elo"]}{streak_badge(p["streak"])}', color=BLUE)
        await interaction.response.send_message(embed=e, ephemeral=True)


class ChallengerTierModal(discord.ui.Modal):
    """Shown to the challenger via /compchallenge — locks in their tier and
    posts the public challenge for the opponent to accept/decline."""

    def __init__(self, bot, opponent: discord.Member, edition: str, gamemode: str):
        super().__init__(title=f'⚔️  Challenge {opponent.display_name}', timeout=300)
        self.bot = bot
        self.opponent = opponent
        self.edition = edition
        self.gamemode = gamemode
        self.tier = discord.ui.TextInput(
            label='Your Current / Claimed Tier',
            placeholder='e.g. HT3, LT5, or "Unranked"',
            required=True, max_length=50,
        )
        self.add_item(self.tier)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot, guild = self.bot, interaction.guild
        tier_label = self.tier.value.strip() or 'Unranked'

        await set_player_tier(bot, guild.id, interaction.user.id, self.edition, self.gamemode, tier_label)
        challenge_id = await create_challenge(bot, guild.id, interaction.user.id, self.opponent.id,
                                               self.edition, self.gamemode, tier_label)
        challenge = await get_challenge(bot, challenge_id)

        settings = await get_comp_settings(bot, guild.id)
        ch_id = settings.get('challenge_channel_id') or settings.get('queue_channel_id')
        channel = guild.get_channel(ch_id) if ch_id else None
        if not channel:
            return await interaction.followup.send(
                embed=E.error('No Challenge or Queue channel is configured — ask an admin to set one via /compadminpanel.'),
                ephemeral=True)

        gamemodes = await get_comp_gamemodes(bot, guild.id, self.edition)
        emoji = next((em for n, em in gamemodes if n == self.gamemode), '⚔️')
        embed = challenge_embed(guild, challenge, emoji)
        view = ChallengeActionView(bot, challenge_id, interaction.user.display_name, self.opponent.display_name)
        try:
            msg = await channel.send(content=f'{self.opponent.mention}', embed=embed, view=view,
                                      allowed_mentions=discord.AllowedMentions(users=True))
            await set_challenge_message(bot, challenge_id, channel.id, msg.id)
        except discord.HTTPException:
            pass

        try:
            await self.opponent.send(embed=E.base(
                '⚔️  You\'ve Been Challenged!',
                f'{interaction.user.display_name} challenged you to a **{self.gamemode}** Comp Fight in **{guild.name}**.\n'
                f'Head to {channel.mention} to Accept or Decline.', color=PURPLE))
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.followup.send(
            embed=E.success(f'Challenge sent to {self.opponent.mention} in {channel.mention}.'), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN — Post Result flow
# ═══════════════════════════════════════════════════════════════════════════════

class CompResultModal(discord.ui.Modal):
    def __init__(self, bot, match: dict, winner_team: int):
        team_txt = f'Team {winner_team}'
        super().__init__(title=f'📊  Post Comp Result — {team_txt} Won', timeout=300)
        self.bot = bot
        self.match = match
        self.winner_team = winner_team
        team_size = match['team_size']

        self.score = discord.ui.TextInput(
            label='Score', placeholder='e.g. 3-0, 3-1, 3-2', required=True, max_length=20)
        winner_label = "Winning Team's New Tier (blank=unchanged)" if team_size > 1 else "Winner's New Tier (blank = unchanged)"
        self.winner_tier = discord.ui.TextInput(
            label=winner_label,
            placeholder='e.g. High B Tier [HT3]', required=False, max_length=100)
        self.loser_tier = discord.ui.TextInput(
            label="Loser's New Tier (blank = unchanged)",
            placeholder='e.g. Low C Tier [LC4]', required=False, max_length=100)
        self.note = discord.ui.TextInput(
            label='Changelog Note (optional)', style=discord.TextStyle.paragraph,
            placeholder='e.g. Winning team : +High B Tier [HT3]', required=False, max_length=300)

        self.add_item(self.score)
        self.add_item(self.winner_tier)
        self.add_item(self.loser_tier)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        guild = interaction.guild
        match = self.match

        team1_ids = json.loads(match['team1_ids'])
        team2_ids = json.loads(match['team2_ids'])
        winner_ids = team1_ids if self.winner_team == 1 else team2_ids
        loser_ids = team2_ids if self.winner_team == 1 else team1_ids

        winner_tier = self.winner_tier.value.strip() or None
        loser_tier = self.loser_tier.value.strip() or None
        note = self.note.value.strip()
        if not note:
            parts = []
            if winner_tier:
                parts.append(f'{" ".join(f"<@{u}>" for u in winner_ids)} : → **{winner_tier}**')
            if loser_tier:
                parts.append(f'{" ".join(f"<@{u}>" for u in loser_ids)} : → **{loser_tier}**')
            note = '\n'.join(parts)

        elo_delta = await record_match_result(bot, match['guild_id'], match['edition'], match['gamemode'],
                                               winner_ids, loser_ids, winner_tier, loser_tier)
        await complete_match(bot, match['id'], self.winner_team, self.score.value.strip(), note)
        match = await get_match(bot, match['id'])
        await sync_tier_roles(bot, guild, winner_ids + loser_ids)

        settings = await get_comp_settings(bot, guild.id)
        gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
        emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')

        embed = result_embed(guild, match, self.winner_team, self.score.value.strip(), emoji, note, elo_delta)

        posted = False
        ch_id = settings.get('result_channel_id')
        if ch_id:
            channel = guild.get_channel(ch_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                    posted = True
                except discord.HTTPException:
                    pass

        # Update the original match message so it reflects the final result.
        if match.get('channel_id') and match.get('message_id'):
            src_channel = guild.get_channel(match['channel_id'])
            if src_channel:
                try:
                    msg = await src_channel.fetch_message(match['message_id'])
                    result_note = embed.copy()
                    await msg.edit(embed=result_note, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        log_id = settings.get('log_channel_id')
        if log_id:
            log_channel = guild.get_channel(log_id)
            if log_channel:
                try:
                    await log_channel.send(embed=E.base(
                        '📋  Comp Result Logged',
                        f'{interaction.user.mention} posted the result for **{match["gamemode"]}** '
                        f'({mode_label(match["team_size"])}) Match #{match["id"]}: '
                        f'Team {self.winner_team} defeated Team {2 if self.winner_team == 1 else 1} '
                        f'({self.score.value.strip()}).',
                        color=PURPLE))
                except discord.HTTPException:
                    pass

        note_suffix = '' if posted else '\n⚠️ No result channel configured — set one via /compadminpanel > Set Channels.'
        await interaction.followup.send(
            embed=E.success(f'Result posted for **{match["gamemode"]}** Match #{match["id"]}.{note_suffix}'),
            ephemeral=True)


def _team_display(guild: discord.Guild, ids: list[int]) -> str:
    names = []
    for uid in ids:
        m = guild.get_member(uid)
        names.append(m.display_name if m else str(uid))
    return '+'.join(names)


class WinnerPickView(discord.ui.View):
    def __init__(self, bot, match: dict, guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.match = match
        team1_ids = json.loads(match['team1_ids'])
        team2_ids = json.loads(match['team2_ids'])
        self.p1_button.label = f'Team 1 Won ({_team_display(guild, team1_ids)})'[:80]
        self.p2_button.label = f'Team 2 Won ({_team_display(guild, team2_ids)})'[:80]

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p1_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CompResultModal(self.bot, self.match, 1))

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p2_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CompResultModal(self.bot, self.match, 2))


class ActiveMatchSelectView(discord.ui.View):
    def __init__(self, bot, matches: list[dict], guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild = guild
        options = []
        for m in matches[:25]:
            t1 = _team_display(guild, json.loads(m['team1_ids']))
            t2 = _team_display(guild, json.loads(m['team2_ids']))
            label = f'#{m["id"]} [{mode_label(m["team_size"])}] {t1} vs {t2} — {m["gamemode"]}'[:100]
            options.append(discord.SelectOption(label=label, value=str(m['id'])))
        self.match_select.options = options or [discord.SelectOption(label='No active matches', value='__none__')]
        if not options:
            self.match_select.disabled = True

    @discord.ui.select(placeholder='Select a match to post the result for…')
    async def match_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        match_id = int(select.values[0])
        match = await get_match(self.bot, match_id)
        if not match:
            return await interaction.response.send_message(embed=E.error('Match not found.'), ephemeral=True)
        t1 = _team_display(self.guild, json.loads(match['team1_ids']))
        t2 = _team_display(self.guild, json.loads(match['team2_ids']))
        await interaction.response.send_message(
            embed=E.base('🏆  Who Won?', f'**Team 1** ({t1}) vs **Team 2** ({t2}) — pick the winning team:', color=PURPLE),
            view=WinnerPickView(self.bot, match, self.guild), ephemeral=True)


class ActiveMatchCancelSelectView(discord.ui.View):
    def __init__(self, bot, matches: list[dict], guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        options = []
        for m in matches[:25]:
            t1 = _team_display(guild, json.loads(m['team1_ids']))
            t2 = _team_display(guild, json.loads(m['team2_ids']))
            label = f'#{m["id"]} [{mode_label(m["team_size"])}] {t1} vs {t2} — {m["gamemode"]}'[:100]
            options.append(discord.SelectOption(label=label, value=str(m['id'])))
        self.cancel_select.options = options or [discord.SelectOption(label='No active matches', value='__none__')]
        if not options:
            self.cancel_select.disabled = True

    @discord.ui.select(placeholder='Select a match to force-cancel…')
    async def cancel_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        match_id = int(select.values[0])
        await cancel_match(self.bot, match_id)
        await interaction.response.send_message(embed=E.success(f'Match #{match_id} cancelled.'), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL — channels / gamemodes / toggles / ping role
# ═══════════════════════════════════════════════════════════════════════════════

class AddCompGamemodeModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f'➕  Add {edition} Gamemode', timeout=120)
        self.bot = bot
        self.edition = edition
        self.name_input = discord.ui.TextInput(label='Gamemode Name', placeholder='e.g. Boxing', required=True, max_length=50)
        self.emoji_input = discord.ui.TextInput(label='Emoji', placeholder='e.g. 🥊', required=False, max_length=100)
        self.add_item(self.name_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await add_comp_gamemode(self.bot, interaction.guild_id, self.edition,
                                           self.name_input.value.strip(), self.emoji_input.value.strip() or '🎮')
        await interaction.followup.send(embed=(E.success(msg) if ok else E.error(msg)), ephemeral=True)


class CompGamemodeResetConfirmView(discord.ui.View):
    def __init__(self, bot, edition: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.edition = edition

    @discord.ui.button(label='Confirm Reset', style=discord.ButtonStyle.danger, emoji='🔄')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reset_comp_gamemodes(self.bot, interaction.guild_id, self.edition)
        await interaction.response.send_message(embed=E.success(f'{self.edition} comp gamemodes reset to default.'), ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)
        self.stop()


class CompGamemodeManageView(discord.ui.View):
    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=180)
        self.bot = bot
        self.edition = edition
        if gamemodes:
            self.remove_select.options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, emoji in gamemodes[:25]]
            self.remove_select.placeholder = f'Select a {edition} gamemode to remove…'
        else:
            self.remove_select.disabled = True
            self.remove_select.options = [discord.SelectOption(label='No gamemodes configured', value='__none__')]

    @discord.ui.select(row=0)
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        name = select.values[0]
        await remove_comp_gamemode(self.bot, interaction.guild_id, self.edition, name)
        await interaction.response.send_message(embed=E.success(f'Removed **{name}** from {self.edition}.'), ephemeral=True)

    @discord.ui.button(label='Add Gamemode', emoji='➕', style=discord.ButtonStyle.success, row=1)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddCompGamemodeModal(self.bot, self.edition))

    @discord.ui.button(label='Reset to Default', emoji='🔄', style=discord.ButtonStyle.danger, row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Reset', f'Reset {self.edition} comp gamemodes to the built-in default list?', color=ORANGE),
            view=CompGamemodeResetConfirmView(self.bot, self.edition), ephemeral=True)


class CompChannelsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    async def _save(self, interaction, field, label, select):
        channel = select.values[0] if select.values else None
        await set_comp_channel(self.bot, interaction.guild_id, field, channel.id if channel else None)
        mention = channel.mention if channel else 'Not set'
        await interaction.response.send_message(embed=E.success(f'{label} channel set to {mention}.'), ephemeral=True)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='📡  Select the Queue channel…', row=0)
    async def queue_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'queue_channel_id', 'Queue', select)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='⚔️  Select the Challenge/Match channel…', row=1)
    async def challenge_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'challenge_channel_id', 'Challenge/Match', select)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='🏆  Select the Result channel…', row=2)
    async def result_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'result_channel_id', 'Result', select)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder='📋  Select the Logs channel…', row=3)
    async def logs_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self._save(interaction, 'log_channel_id', 'Logs', select)

    @discord.ui.button(label='Clear All', emoji='🧹', style=discord.ButtonStyle.danger, row=4)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        for field in COMP_CHANNEL_FIELDS:
            await set_comp_channel(self.bot, interaction.guild_id, field, None)
        await interaction.response.send_message(embed=E.success('All Comp Fight channels cleared.'), ephemeral=True)


class CompPingRoleView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='🔔  Select the role to ping on new matches…', row=0)
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        await set_comp_ping_role(self.bot, interaction.guild_id, role.id if role else None)
        await interaction.response.send_message(
            embed=E.success(f'Comp match ping role set to {role.mention if role else "Not set"}.'), ephemeral=True)

    @discord.ui.button(label='Clear Ping Role', emoji='🧹', style=discord.ButtonStyle.danger, row=1)
    async def clear_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_comp_ping_role(self.bot, interaction.guild_id, None)
        await interaction.response.send_message(embed=E.success('Comp match ping role cleared.'), ephemeral=True)


class AddTierRoleModal(discord.ui.Modal):
    def __init__(self, bot):
        super().__init__(title='🎭  Map a Tier → Role', timeout=180)
        self.bot = bot
        self.tier_input = discord.ui.TextInput(
            label='Exact Tier Label', placeholder='e.g. High B Tier [HT3]', required=True, max_length=100)
        self.add_item(self.tier_input)

    async def on_submit(self, interaction: discord.Interaction):
        embed = E.base('🎭  Pick the Role', f'Which role should be auto-assigned for tier **{self.tier_input.value.strip()}**?', color=PURPLE)
        await interaction.response.send_message(embed=embed, view=TierRolePickView(self.bot, self.tier_input.value.strip()), ephemeral=True)


class TierRolePickView(discord.ui.View):
    def __init__(self, bot, tier_label: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.tier_label = tier_label

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Select the role for this tier…')
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        await set_tier_role(self.bot, interaction.guild_id, self.tier_label, role.id)
        await interaction.response.send_message(
            embed=E.success(f'**{self.tier_label}** will now auto-assign {role.mention}.'), ephemeral=True)


class RemoveTierRoleSelectView(discord.ui.View):
    def __init__(self, bot, mapping: dict[str, int]):
        super().__init__(timeout=180)
        self.bot = bot
        options = [discord.SelectOption(label=tier[:100], value=tier) for tier in list(mapping.keys())[:25]]
        self.remove_select.options = options or [discord.SelectOption(label='No tier roles mapped', value='__none__')]
        if not options:
            self.remove_select.disabled = True

    @discord.ui.select(placeholder='Select a tier-role mapping to remove…')
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await remove_tier_role(self.bot, interaction.guild_id, select.values[0])
        await interaction.response.send_message(embed=E.success(f'Removed the role mapping for **{select.values[0]}**.'), ephemeral=True)


class CompTierRolesView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.button(label='Add Mapping', emoji='➕', style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddTierRoleModal(self.bot))

    @discord.ui.button(label='Remove Mapping', emoji='🗑️', style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        mapping = await get_tier_roles(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=E.base('🗑️  Remove Mapping', color=ORANGE),
                                                  view=RemoveTierRoleSelectView(self.bot, mapping), ephemeral=True)


class SeasonResetConfirmView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label='Confirm Season Reset', style=discord.ButtonStyle.danger, emoji='📅')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_season = await do_season_reset(self.bot, interaction.guild_id)
        await interaction.followup.send(
            embed=E.success(f'Season reset complete — everyone\'s stats were archived and **Season {new_season}** has begun.'),
            ephemeral=True)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=E.base('✖️  Cancelled', color=GREY), ephemeral=True)
        self.stop()


class CompAdminPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def _refresh(self, interaction: discord.Interaction):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        active = await get_active_matches(self.bot, interaction.guild_id)
        queue = await get_full_queue(self.bot, interaction.guild_id)
        tier_roles = await get_tier_roles(self.bot, interaction.guild_id)
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, len(active), len(queue), len(tier_roles))
        await interaction.response.edit_message(embed=embed, view=self)

    async def _toggle_edition(self, interaction: discord.Interaction, field: str, edition: str):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        new_state = not bool(settings.get(field, 1))
        await set_comp_toggle(self.bot, interaction.guild_id, field, new_state)

        if not new_state:
            # Edition just got closed — rip it out of the queue and shut
            # down anything currently in progress for it, everywhere.
            await purge_queue_by_edition(self.bot, interaction.guild_id, edition)
            await cancel_active_matches_by_edition(self.bot, interaction.guild, edition)
            await _refresh_queue_status(self.bot, interaction.guild)

        await self._refresh(interaction)

    # Row 0 — toggles
    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success, row=0)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_edition(interaction, 'java_enabled', 'Java Edition')

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary, row=0)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_edition(interaction, 'bedrock_enabled', 'Bedrock Edition')

    @discord.ui.button(label='Refresh', emoji='🔄', style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._refresh(interaction)

    # Row 1 — gamemodes
    @discord.ui.button(label='Java Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        await interaction.response.send_message(embed=gamemode_manage_embed('Java Edition', gamemodes),
                                                 view=CompGamemodeManageView(self.bot, 'Java Edition', gamemodes), ephemeral=True)

    @discord.ui.button(label='Bedrock Gamemodes', emoji='🎮', style=discord.ButtonStyle.secondary, row=1)
    async def manage_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        await interaction.response.send_message(embed=gamemode_manage_embed('Bedrock Edition', gamemodes),
                                                 view=CompGamemodeManageView(self.bot, 'Bedrock Edition', gamemodes), ephemeral=True)

    # Row 2 — channels + ping role
    @discord.ui.button(label='Set Channels', emoji='📡', style=discord.ButtonStyle.primary, row=2)
    async def set_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('📡  Comp Fight Channels',
                          'Pick the channel for each purpose:\n\n'
                          '• **Queue** — public panel + live queue status\n'
                          '• **Challenge/Match** — where matched lobbies get posted (falls back to Queue if unset)\n'
                          '• **Result** — where final results get posted\n'
                          '• **Logs** — staff activity logs',
                          color=PURPLE),
            view=CompChannelsView(self.bot), ephemeral=True)

    @discord.ui.button(label='Set Ping Role', emoji='🔔', style=discord.ButtonStyle.secondary, row=2)
    async def set_ping_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('🔔  Match Ping Role', 'Pick the role pinged whenever a lobby gets matched.', color=PURPLE),
            view=CompPingRoleView(self.bot), ephemeral=True)

    # Row 3 — results / matches
    @discord.ui.button(label='Post Result', emoji='🏆', style=discord.ButtonStyle.success, row=3)
    async def post_result(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_active_matches(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=E.base('🏆  Post Comp Result', 'Select the match to log a result for:', color=PURPLE),
            view=ActiveMatchSelectView(self.bot, matches, interaction.guild), ephemeral=True)

    @discord.ui.button(label='Force Cancel Match', emoji='🛑', style=discord.ButtonStyle.danger, row=3)
    async def force_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        matches = await get_active_matches(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=E.base('🛑  Force Cancel a Match', 'Select the match to cancel:', color=ORANGE),
            view=ActiveMatchCancelSelectView(self.bot, matches, interaction.guild), ephemeral=True)

    @discord.ui.button(label='View Queue', emoji='📋', style=discord.ButtonStyle.secondary, row=3)
    async def view_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        entries = await get_full_queue(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=queue_status_embed(interaction.guild, entries), ephemeral=True)

    # Row 4 — tier roles / season
    @discord.ui.button(label='Tier Roles', emoji='🎭', style=discord.ButtonStyle.primary, row=4)
    async def tier_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        mapping = await get_tier_roles(self.bot, interaction.guild_id)
        lines = '\n'.join(f'**{tier}** → <@&{role_id}>' for tier, role_id in mapping.items()) or '_No mappings yet._'
        await interaction.response.send_message(
            embed=E.base('🎭  Auto Tier Roles',
                          f'{lines}\n\n_A member auto-gets the role for their highest current tier and loses any other mapped role._',
                          color=PURPLE),
            view=CompTierRolesView(self.bot), ephemeral=True)

    @discord.ui.button(label='Season Reset', emoji='📅', style=discord.ButtonStyle.danger, row=4)
    async def season_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=E.base('⚠️  Confirm Season Reset',
                          f'This archives **every** player\'s stats (wins/losses/ELO/streak/tier) from '
                          f'Season **{settings.get("season_number", 1)}** and resets everyone to Unranked / {ELO_DEFAULT} ELO '
                          f'for a fresh season. This cannot be undone from here.',
                          color=ORANGE),
            view=SeasonResetConfirmView(self.bot), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class CompFight(commands.Cog, name='CompFight'):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(CompQueuePanelView(bot))

    @app_commands.command(name='comppanel', description='(Admin/Owner only) Post the Comp Fight queue panel.')
    async def comppanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        embed = queue_join_embed(interaction.guild)
        # Sent as a plain channel message (not interaction.response) so Discord
        # doesn't tag it with "Username used /comppanel" above the panel.
        await interaction.channel.send(embed=embed, view=CompQueuePanelView(self.bot))
        await interaction.response.send_message(embed=E.success('Comp Fight panel posted.'), ephemeral=True)
        await _refresh_queue_status(self.bot, interaction.guild)

    @app_commands.command(name='compadminpanel', description='(Admin/Owner only) Full admin panel for the Comp Fight system.')
    async def compadminpanel(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        java_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Java Edition')
        bedrock_gm = await get_comp_gamemodes(self.bot, interaction.guild_id, 'Bedrock Edition')
        active = await get_active_matches(self.bot, interaction.guild_id)
        queue = await get_full_queue(self.bot, interaction.guild_id)
        tier_roles = await get_tier_roles(self.bot, interaction.guild_id)
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, len(active), len(queue), len(tier_roles))
        await interaction.response.send_message(embed=embed, view=CompAdminPanelView(self.bot), ephemeral=True)

    @app_commands.command(name='compstats', description='Check your (or another player\'s) Comp Fight tiers & record.')
    @app_commands.describe(edition='Java or Bedrock Edition', gamemode='Gamemode name', member='Whose stats to check')
    async def compstats(self, interaction: discord.Interaction, edition: str, gamemode: str, member: discord.Member = None):
        if edition not in EDITIONS:
            return await interaction.response.send_message(
                embed=E.error(f'Edition must be one of: {", ".join(EDITIONS)}'), ephemeral=True)
        member = member or interaction.user
        player = await get_player(self.bot, interaction.guild_id, member.id, edition, gamemode)
        e = E.base(f'📊  {member.display_name} — {gamemode}',
                    f'**Tier:** {player["tier_label"]}\n'
                    f'**Record:** {player["wins"]}W / {player["losses"]}L\n'
                    f'**ELO:** {player["elo"]}{streak_badge(player["streak"])}',
                    color=BLUE)
        e.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @compstats.autocomplete('edition')
    async def edition_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=e, value=e) for e in EDITIONS if current.lower() in e.lower()][:25]

    @compstats.autocomplete('gamemode')
    async def gamemode_autocomplete(self, interaction: discord.Interaction, current: str):
        edition = interaction.namespace.edition if interaction.namespace.edition in EDITIONS else 'Bedrock Edition'
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        return [app_commands.Choice(name=n, value=n) for n, _ in gamemodes if current.lower() in n.lower()][:25]

    @app_commands.command(name='compleaderboard', description='Top Comp Fight players for a gamemode by ELO.')
    @app_commands.describe(edition='Java or Bedrock Edition', gamemode='Gamemode name', top='How many to show (default 10, max 25)')
    async def compleaderboard(self, interaction: discord.Interaction, edition: str, gamemode: str, top: int = 10):
        if edition not in EDITIONS:
            return await interaction.response.send_message(
                embed=E.error(f'Edition must be one of: {", ".join(EDITIONS)}'), ephemeral=True)
        top = max(1, min(top, 25))
        rows = await get_leaderboard(self.bot, interaction.guild_id, edition, gamemode, top)
        await interaction.response.send_message(embed=leaderboard_embed(interaction.guild, edition, gamemode, rows))

    @compleaderboard.autocomplete('edition')
    async def leaderboard_edition_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=e, value=e) for e in EDITIONS if current.lower() in e.lower()][:25]

    @compleaderboard.autocomplete('gamemode')
    async def leaderboard_gamemode_autocomplete(self, interaction: discord.Interaction, current: str):
        edition = interaction.namespace.edition if interaction.namespace.edition in EDITIONS else 'Bedrock Edition'
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        return [app_commands.Choice(name=n, value=n) for n, _ in gamemodes if current.lower() in n.lower()][:25]

    @app_commands.command(name='comphistory', description='See recent Comp Fight match history for yourself or another player.')
    @app_commands.describe(member='Whose history to check', amount='How many recent matches (default 10, max 25)')
    async def comphistory(self, interaction: discord.Interaction, member: discord.Member = None, amount: int = 10):
        member = member or interaction.user
        amount = max(1, min(amount, 25))
        matches = await get_match_history(self.bot, interaction.guild_id, member.id, amount)
        await interaction.response.send_message(embed=history_embed(interaction.guild, member, matches), ephemeral=True)

    @app_commands.command(name='compchallenge', description='Directly challenge another player to a Comp Fight (1v1).')
    @app_commands.describe(opponent='Who you want to challenge', edition='Java or Bedrock Edition', gamemode='Gamemode name')
    async def compchallenge(self, interaction: discord.Interaction, opponent: discord.Member, edition: str, gamemode: str):
        if edition not in EDITIONS:
            return await interaction.response.send_message(
                embed=E.error(f'Edition must be one of: {", ".join(EDITIONS)}'), ephemeral=True)
        if opponent.id == interaction.user.id:
            return await interaction.response.send_message(embed=E.error('You can\'t challenge yourself.'), ephemeral=True)
        if opponent.bot:
            return await interaction.response.send_message(embed=E.error('You can\'t challenge a bot.'), ephemeral=True)

        settings = await get_comp_settings(self.bot, interaction.guild_id)
        field = 'bedrock_enabled' if edition == 'Bedrock Edition' else 'java_enabled'
        if not bool(settings.get(field, 1)):
            return await interaction.response.send_message(embed=E.error(f'**{edition}** Comp Fight is currently closed.'), ephemeral=True)

        remaining = await get_cooldown_remaining(self.bot, interaction.guild_id, interaction.user.id)
        if remaining:
            return await interaction.response.send_message(
                embed=E.error(f'You\'re on a queue/challenge cooldown for **{remaining} more minute(s)** due to recent dodges.'),
                ephemeral=True)

        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        if not any(n == gamemode for n, _ in gamemodes):
            return await interaction.response.send_message(
                embed=E.error(f'**{gamemode}** isn\'t a configured gamemode for {edition}.'), ephemeral=True)

        await interaction.response.send_modal(ChallengerTierModal(self.bot, opponent, edition, gamemode))

    @compchallenge.autocomplete('edition')
    async def challenge_edition_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=e, value=e) for e in EDITIONS if current.lower() in e.lower()][:25]

    @compchallenge.autocomplete('gamemode')
    async def challenge_gamemode_autocomplete(self, interaction: discord.Interaction, current: str):
        edition = interaction.namespace.edition if interaction.namespace.edition in EDITIONS else 'Bedrock Edition'
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        return [app_commands.Choice(name=n, value=n) for n, _ in gamemodes if current.lower() in n.lower()][:25]

    @app_commands.command(name='compclearcooldown', description='(Admin/Owner only) Clear a player\'s anti-dodge queue cooldown.')
    @app_commands.describe(member='The player to clear the cooldown for')
    async def compclearcooldown(self, interaction: discord.Interaction, member: discord.Member):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await clear_cooldown(self.bot, interaction.guild_id, member.id)
        await interaction.response.send_message(embed=E.success(f'Cleared {member.mention}\'s queue cooldown.'), ephemeral=True)


async def setup(bot):
    await bot.add_cog(CompFight(bot))
