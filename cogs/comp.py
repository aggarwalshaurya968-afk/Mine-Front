from __future__ import annotations
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
                queue_status_message_id INTEGER
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
                channel_id     INTEGER,
                message_id     INTEGER,
                player1_id     INTEGER NOT NULL,
                player1_tier   TEXT DEFAULT 'Unranked',
                player1_ready  INTEGER DEFAULT 0,
                player2_id     INTEGER NOT NULL,
                player2_tier   TEXT DEFAULT 'Unranked',
                player2_ready  INTEGER DEFAULT 0,
                status         TEXT DEFAULT 'pending',
                winner_id      INTEGER,
                score          TEXT,
                changelog      TEXT,
                created_at     TEXT DEFAULT (datetime('now')),
                completed_at   TEXT
            )
        ''')
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


# ── Players (tier + record, per edition + gamemode) ─────────────────────────

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
            'gamemode': gamemode, 'tier_label': 'Unranked', 'wins': 0, 'losses': 0}


async def set_player_tier(bot, guild_id: int, user_id: int, edition: str, gamemode: str, tier_label: str):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, tier_label) VALUES (?,?,?,?,?) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET tier_label=excluded.tier_label',
            (guild_id, user_id, edition, gamemode, tier_label)
        )
        await db.commit()


async def record_match_result(bot, guild_id: int, edition: str, gamemode: str,
                               winner_id: int, loser_id: int,
                               winner_new_tier: str | None, loser_new_tier: str | None):
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, wins) VALUES (?,?,?,?,1) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET wins = wins + 1',
            (guild_id, winner_id, edition, gamemode)
        )
        await db.execute(
            'INSERT INTO comp_players (guild_id, user_id, edition, gamemode, losses) VALUES (?,?,?,?,1) '
            'ON CONFLICT(guild_id, user_id, edition, gamemode) DO UPDATE SET losses = losses + 1',
            (guild_id, loser_id, edition, gamemode)
        )
        if winner_new_tier:
            await db.execute(
                'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                (winner_new_tier, guild_id, winner_id, edition, gamemode)
            )
        if loser_new_tier:
            await db.execute(
                'UPDATE comp_players SET tier_label = ? WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
                (loser_new_tier, guild_id, loser_id, edition, gamemode)
            )
        await db.commit()


# ── Queue ─────────────────────────────────────────────────────────────────

async def get_queue_entry(bot, guild_id: int, user_id: int, edition: str, gamemode: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
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
            'SELECT * FROM comp_queue WHERE guild_id=? ORDER BY edition, gamemode, queued_at', (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def leave_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str) -> bool:
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute(
            'DELETE FROM comp_queue WHERE guild_id=? AND user_id=? AND edition=? AND gamemode=?',
            (guild_id, user_id, edition, gamemode)
        )
        await db.commit()
        return cur.rowcount > 0


async def try_join_queue(bot, guild_id: int, user_id: int, edition: str, gamemode: str, tier_label: str):
    """Adds the user to queue; if a waiting opponent for the same edition +
    gamemode already exists, pairs them instead and returns the new match's
    row id. Returns ('queued', None) or ('matched', match_id)."""
    await _ensure_tables(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM comp_queue WHERE guild_id=? AND edition=? AND gamemode=? AND user_id != ? '
            'ORDER BY queued_at LIMIT 1',
            (guild_id, edition, gamemode, user_id)
        ) as cur:
            opponent = await cur.fetchone()

        if opponent is None:
            await db.execute(
                'INSERT INTO comp_queue (guild_id, user_id, edition, gamemode, tier_label) VALUES (?,?,?,?,?)',
                (guild_id, user_id, edition, gamemode, tier_label)
            )
            await db.commit()
            return 'queued', None

        await db.execute('DELETE FROM comp_queue WHERE id = ?', (opponent['id'],))
        cur2 = await db.execute(
            'INSERT INTO comp_matches (guild_id, edition, gamemode, player1_id, player1_tier, '
            'player2_id, player2_tier) VALUES (?,?,?,?,?,?,?)',
            (guild_id, edition, gamemode, opponent['user_id'], opponent['tier_label'], user_id, tier_label)
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


async def set_match_ready(bot, match_id: int, slot: int):
    field = 'player1_ready' if slot == 1 else 'player2_ready'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(f'UPDATE comp_matches SET {field} = 1 WHERE id = ?', (match_id,))
        await db.commit()
    match = await get_match(bot, match_id)
    if match and match['player1_ready'] and match['player2_ready'] and match['status'] == 'pending':
        async with aiosqlite.connect(bot.db.db_path) as db:
            await db.execute("UPDATE comp_matches SET status='ready' WHERE id=?", (match_id,))
            await db.commit()
        match['status'] = 'ready'
    return match


async def set_match_message(bot, match_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('UPDATE comp_matches SET channel_id=?, message_id=? WHERE id=?',
                          (channel_id, message_id, match_id))
        await db.commit()


async def cancel_match(bot, match_id: int):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute("UPDATE comp_matches SET status='cancelled' WHERE id=?", (match_id,))
        await db.commit()


async def complete_match(bot, match_id: int, winner_id: int, score: str, changelog: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            "UPDATE comp_matches SET status='completed', winner_id=?, score=?, changelog=?, "
            "completed_at=datetime('now') WHERE id=?",
            (winner_id, score, changelog, match_id)
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  EMBEDS
# ═══════════════════════════════════════════════════════════════════════════════

def queue_join_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title=f'⚔️  {guild.name} — Comp Fight Queue',
        description=(
            'Looking for a **1v1 Comp Fight**? Hop in the queue below.\n\n'
            '**How it works:**\n'
            '1️⃣ Click **Join Queue**, pick your platform and game mode\n'
            '2️⃣ Tell us your current claimed tier\n'
            '3️⃣ The moment another player queues for the same mode, you\'re '
            'matched — both names get posted together\n'
            '4️⃣ Both players hit **Ready**, play the set, then staff logs '
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


def match_found_embed(guild: discord.Guild, match: dict, p1: discord.abc.User, p2: discord.abc.User,
                       gamemode_emoji: str) -> discord.Embed:
    short = 'Bedrock' if match['edition'] == 'Bedrock Edition' else 'Java'
    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} Comp Match Found',
        description=(
            f'{p1.mention} **VS** {p2.mention}\n\n'
            f'**Platform:** {short} Edition\n'
            'Both players must click **✅ Ready** below. Once both are ready, '
            'hop in-game and play your set — staff will log the result here once it\'s done.'
        ),
        color=BLUE,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name=f'🔹  {p1.display_name}', value=f'Tier: **{match["player1_tier"]}**', inline=True)
    e.add_field(name=f'🔸  {p2.display_name}', value=f'Tier: **{match["player2_tier"]}**', inline=True)
    e.add_field(name='📶  Ready Status', value='⏳ Waiting on both players...', inline=False)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
    return e


def result_embed(guild: discord.Guild, match: dict, winner: discord.abc.User, loser: discord.abc.User,
                  score: str, gamemode_emoji: str, changelog: str) -> discord.Embed:
    e = discord.Embed(
        title=f'{gamemode_emoji}  {match["gamemode"]} Comp Result',
        color=GREEN,
        timestamp=datetime.now(timezone.utc)
    )
    e.set_author(name=f'{winner.display_name} defeated {loser.display_name}',
                 icon_url=winner.display_avatar.url)
    e.add_field(name='📊  Score', value=f'**{score}**', inline=True)
    e.add_field(name='🏆  Winner', value=winner.mention, inline=True)
    e.add_field(name='💀  Loser', value=loser.mention, inline=True)
    if changelog:
        e.add_field(name='📝  Changelog', value=changelog, inline=False)
    e.set_footer(text=f'{guild.name} • Comp Fight • Match #{match["id"]}')
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
            key = f'{entry["edition"]} • {entry["gamemode"]}'
            grouped.setdefault(key, []).append(entry)
        lines = []
        for key, group in grouped.items():
            names = ', '.join(f'<@{g["user_id"]}> (`{g["tier_label"]}`)' for g in group)
            lines.append(f'**{key}**\n{names}')
        e.description = '\n\n'.join(lines)
    e.set_footer(text=f'{guild.name} • Updates live as players queue')
    return e


def comp_admin_embed(guild: discord.Guild, settings: dict, java_gm: list, bedrock_gm: list,
                      active_count: int, queued_count: int) -> discord.Embed:
    java = '✅ Open' if settings.get('java_enabled', 1) else '❌ Closed'
    bedrock = '✅ Open' if settings.get('bedrock_enabled', 1) else '❌ Closed'

    def ch_text(ch_id):
        return f'<#{ch_id}>' if ch_id else '_Not set_'

    e = discord.Embed(
        title='🛠️  Comp Fight — Admin Panel',
        description=(
            f'Full control over the Comp Fight 1v1 system on **{guild.name}**.\n'
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
    e.set_footer(text='Mine Front • Comp Fight Admin Panel')
    return e


def gamemode_manage_embed(edition: str, gamemodes: list[tuple[str, str]]) -> discord.Embed:
    lines = '\n'.join(f'{emoji} **{name}**' for name, emoji in gamemodes) or '_No gamemodes configured._'
    e = discord.Embed(
        title=f'🎮  Manage {edition} Comp Gamemodes',
        description=(
            f'{lines}\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            'Add a new gamemode, remove one from the dropdown, or reset back '
            'to the built-in default list.'
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

    p1 = guild.get_member(match['player1_id']) or discord.Object(id=match['player1_id'])
    p2 = guild.get_member(match['player2_id']) or discord.Object(id=match['player2_id'])
    gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
    emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')

    embed = match_found_embed(guild, match, p1, p2, emoji)
    view = CompMatchView(bot, match_id)
    ping_role_id = settings.get('ping_role_id')
    content = f'<@{match["player1_id"]}> <@{match["player2_id"]}>'
    if ping_role_id:
        content += f' <@&{ping_role_id}>'

    try:
        msg = await channel.send(
            content=content, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True)
        )
        await set_match_message(bot, match_id, channel.id, msg.id)
    except discord.HTTPException:
        pass


class CompJoinModal(discord.ui.Modal):
    def __init__(self, bot, edition: str, gamemode: str):
        super().__init__(title=f'⚔️  Join {gamemode} Queue', timeout=300)
        self.bot = bot
        self.edition = edition
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

        existing = await get_queue_entry(bot, guild.id, interaction.user.id, self.edition, self.gamemode)
        if existing:
            return await interaction.followup.send(
                embed=E.error(f'You are already queued for **{self.gamemode}**.'), ephemeral=True)

        await set_player_tier(bot, guild.id, interaction.user.id, self.edition, self.gamemode, tier_label)
        status, match_id = await try_join_queue(bot, guild.id, interaction.user.id, self.edition, self.gamemode, tier_label)

        if status == 'matched':
            await _post_match(bot, guild, match_id)
            await _refresh_queue_status(bot, guild)
            settings = await get_comp_settings(bot, guild.id)
            ch_id = settings.get('challenge_channel_id') or settings.get('queue_channel_id')
            ch_text = f'<#{ch_id}>' if ch_id else 'the match channel'
            await interaction.followup.send(
                embed=E.success(f'Opponent found! Your **{self.gamemode}** match has been posted in {ch_text}.'),
                ephemeral=True)
        else:
            await _refresh_queue_status(bot, guild)
            await interaction.followup.send(
                embed=E.success(
                    f'You\'ve joined the **{self.gamemode}** queue at tier **{tier_label}**. '
                    'You\'ll be pinged the moment an opponent queues up.'
                ), ephemeral=True)


class CompGamemodeSelectView(discord.ui.View):
    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition
        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        options = [discord.SelectOption(label=name, value=name, emoji=emoji) for name, emoji in gamemodes][:25]
        self.gamemode_select.options = options
        self.gamemode_select.placeholder = f'Select the {short} game mode to queue for…'

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = select.values[0]
        await interaction.response.send_modal(CompJoinModal(self.bot, self.edition, gamemode))


class CompPlatformSelectView(discord.ui.View):
    def __init__(self, bot, java_enabled: bool = True, bedrock_enabled: bool = True):
        super().__init__(timeout=120)
        self.bot = bot
        if not bedrock_enabled:
            self.remove_item(self.bedrock)
        if not java_enabled:
            self.remove_item(self.java)

    async def _pick(self, interaction: discord.Interaction, edition: str):
        gamemodes = await get_comp_gamemodes(self.bot, interaction.guild_id, edition)
        if not gamemodes:
            return await interaction.response.send_message(
                embed=E.error(f'No gamemodes are configured for {edition} yet. Ask an admin to add some via /compadminpanel.'),
                ephemeral=True)
        short = 'Bedrock' if edition == 'Bedrock Edition' else 'Java'
        platform_emoji = '🟢' if edition == 'Bedrock Edition' else '🍲'
        embed = discord.Embed(
            title=f'{platform_emoji}  {short} — Select Game Mode',
            description='Choose the game mode you want to queue for:',
            color=PURPLE,
        )
        await interaction.response.send_message(embed=embed, view=CompGamemodeSelectView(self.bot, edition, gamemodes), ephemeral=True)

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
            discord.SelectOption(label=f'{e["edition"]} • {e["gamemode"]}', value=f'{e["edition"]}||{e["gamemode"]}')
            for e in entries[:25]
        ]

    @discord.ui.select(placeholder='Select a queue to leave…')
    async def leave_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        edition, gamemode = select.values[0].split('||', 1)
        ok = await leave_queue(self.bot, interaction.guild_id, interaction.user.id, edition, gamemode)
        await _refresh_queue_status(self.bot, interaction.guild)
        if ok:
            await interaction.response.send_message(embed=E.success(f'Left the **{gamemode}** queue.'), ephemeral=True)
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

    async def _slot_for(self, interaction: discord.Interaction, match: dict) -> int | None:
        if interaction.user.id == match['player1_id']:
            return 1
        if interaction.user.id == match['player2_id']:
            return 2
        return None

    @discord.ui.button(label='Ready', emoji='✅', style=discord.ButtonStyle.success, custom_id='comp:match:ready')
    async def ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match or match['status'] not in ('pending', 'ready'):
            return await interaction.response.send_message(embed=E.error('This match is no longer active.'), ephemeral=True)
        slot = await self._slot_for(interaction, match)
        if slot is None:
            return await interaction.response.send_message(
                embed=E.error('Only the two matched players can mark themselves ready.'), ephemeral=True)

        match = await set_match_ready(self.bot, self.match_id, slot)
        p1 = interaction.guild.get_member(match['player1_id'])
        p2 = interaction.guild.get_member(match['player2_id'])
        status_text = (
            '🟢 Both players ready! Play your set — staff will log the result shortly.'
            if match['status'] == 'ready' else
            f'⏳ Waiting on {"Player 2" if slot == 1 else "Player 1"}...'
        )
        embed = interaction.message.embeds[0]
        embed.set_field_at(2, name='📶  Ready Status', value=status_text, inline=False)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label='Cancel Match', emoji='❌', style=discord.ButtonStyle.danger, custom_id='comp:match:cancel')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match or match['status'] in ('completed', 'cancelled'):
            return await interaction.response.send_message(embed=E.error('This match is no longer active.'), ephemeral=True)
        slot = await self._slot_for(interaction, match)
        from cogs.access import is_admin_or_owner
        if slot is None and not await is_admin_or_owner(self.bot, interaction):
            return await interaction.response.send_message(
                embed=E.error('Only the matched players or staff can cancel this match.'), ephemeral=True)

        await cancel_match(self.bot, self.match_id)
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = RED
        embed.title = f'❌  {embed.title}'
        embed.set_field_at(2, name='📶  Status', value=f'Cancelled by {interaction.user.mention}.', inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='Stats', emoji='📊', style=discord.ButtonStyle.secondary, custom_id='comp:match:stats')
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = await get_match(self.bot, self.match_id)
        if not match:
            return await interaction.response.send_message(embed=E.error('Match not found.'), ephemeral=True)
        p1 = await get_player(self.bot, match['guild_id'], match['player1_id'], match['edition'], match['gamemode'])
        p2 = await get_player(self.bot, match['guild_id'], match['player2_id'], match['edition'], match['gamemode'])
        e = E.base(f'📊  {match["gamemode"]} Stats', color=BLUE)
        e.add_field(name=f'<@{match["player1_id"]}>', value=f'Tier: **{p1["tier_label"]}**\nW/L: **{p1["wins"]}/{p1["losses"]}**', inline=True)
        e.add_field(name=f'<@{match["player2_id"]}>', value=f'Tier: **{p2["tier_label"]}**\nW/L: **{p2["wins"]}/{p2["losses"]}**', inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN — Post Result flow
# ═══════════════════════════════════════════════════════════════════════════════

class CompResultModal(discord.ui.Modal):
    def __init__(self, bot, match: dict, winner_id: int, loser_id: int):
        super().__init__(title='📊  Post Comp Result', timeout=300)
        self.bot = bot
        self.match = match
        self.winner_id = winner_id
        self.loser_id = loser_id

        self.score = discord.ui.TextInput(
            label='Score', placeholder='e.g. 3-0, 3-1, 3-2', required=True, max_length=20)
        self.winner_tier = discord.ui.TextInput(
            label="Winner's New Tier (blank = unchanged)",
            placeholder='e.g. High B Tier [HT3]', required=False, max_length=100)
        self.loser_tier = discord.ui.TextInput(
            label="Loser's New Tier (blank = unchanged)",
            placeholder='e.g. Low C Tier [LC4]', required=False, max_length=100)
        self.note = discord.ui.TextInput(
            label='Changelog Note (optional)', style=discord.TextStyle.paragraph,
            placeholder='e.g. Winner : +High B Tier [HT3]', required=False, max_length=300)

        self.add_item(self.score)
        self.add_item(self.winner_tier)
        self.add_item(self.loser_tier)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        guild = interaction.guild
        match = self.match

        winner_tier = self.winner_tier.value.strip() or None
        loser_tier = self.loser_tier.value.strip() or None
        note = self.note.value.strip()
        if not note:
            parts = []
            if winner_tier:
                parts.append(f'<@{self.winner_id}> : → **{winner_tier}**')
            if loser_tier:
                parts.append(f'<@{self.loser_id}> : → **{loser_tier}**')
            note = '\n'.join(parts)

        await record_match_result(bot, match['guild_id'], match['edition'], match['gamemode'],
                                   self.winner_id, self.loser_id, winner_tier, loser_tier)
        await complete_match(bot, match['id'], self.winner_id, self.score.value.strip(), note)

        settings = await get_comp_settings(bot, guild.id)
        gamemodes = await get_comp_gamemodes(bot, guild.id, match['edition'])
        emoji = next((em for n, em in gamemodes if n == match['gamemode']), '⚔️')

        winner = guild.get_member(self.winner_id) or discord.Object(id=self.winner_id)
        loser = guild.get_member(self.loser_id) or discord.Object(id=self.loser_id)
        embed = result_embed(guild, match, winner, loser, self.score.value.strip(), emoji, note)

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
                        f'Match #{match["id"]}: <@{self.winner_id}> defeated <@{self.loser_id}> ({self.score.value.strip()}).',
                        color=PURPLE))
                except discord.HTTPException:
                    pass

        note_suffix = '' if posted else '\n⚠️ No result channel configured — set one via /compadminpanel > Set Channels.'
        await interaction.followup.send(
            embed=E.success(f'Result posted for **{match["gamemode"]}** Match #{match["id"]}.{note_suffix}'),
            ephemeral=True)


class WinnerPickView(discord.ui.View):
    def __init__(self, bot, match: dict, p1_name: str, p2_name: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.match = match
        self.p1_button.label = f'{p1_name} Won'
        self.p2_button.label = f'{p2_name} Won'

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p1_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            CompResultModal(self.bot, self.match, self.match['player1_id'], self.match['player2_id']))

    @discord.ui.button(style=discord.ButtonStyle.success, emoji='🏆')
    async def p2_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            CompResultModal(self.bot, self.match, self.match['player2_id'], self.match['player1_id']))


class ActiveMatchSelectView(discord.ui.View):
    def __init__(self, bot, matches: list[dict], guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        self.guild = guild
        options = []
        for m in matches[:25]:
            p1 = guild.get_member(m['player1_id'])
            p2 = guild.get_member(m['player2_id'])
            p1n = p1.display_name if p1 else str(m['player1_id'])
            p2n = p2.display_name if p2 else str(m['player2_id'])
            label = f'#{m["id"]} {p1n} vs {p2n} — {m["gamemode"]}'[:100]
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
        p1 = self.guild.get_member(match['player1_id'])
        p2 = self.guild.get_member(match['player2_id'])
        p1n = p1.display_name if p1 else str(match['player1_id'])
        p2n = p2.display_name if p2 else str(match['player2_id'])
        await interaction.response.send_message(
            embed=E.base('🏆  Who Won?', f'**{p1n}** vs **{p2n}** — pick the winner:', color=PURPLE),
            view=WinnerPickView(self.bot, match, p1n, p2n), ephemeral=True)


class ActiveMatchCancelSelectView(discord.ui.View):
    def __init__(self, bot, matches: list[dict], guild: discord.Guild):
        super().__init__(timeout=120)
        self.bot = bot
        options = []
        for m in matches[:25]:
            p1 = guild.get_member(m['player1_id'])
            p2 = guild.get_member(m['player2_id'])
            p1n = p1.display_name if p1 else str(m['player1_id'])
            p2n = p2.display_name if p2 else str(m['player2_id'])
            label = f'#{m["id"]} {p1n} vs {p2n} — {m["gamemode"]}'[:100]
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
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, len(active), len(queue))
        await interaction.response.edit_message(embed=embed, view=self)

    # Row 0 — toggles
    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success, row=0)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        await set_comp_toggle(self.bot, interaction.guild_id, 'java_enabled', not bool(settings.get('java_enabled', 1)))
        await self._refresh(interaction)

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary, row=0)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await get_comp_settings(self.bot, interaction.guild_id)
        await set_comp_toggle(self.bot, interaction.guild_id, 'bedrock_enabled', not bool(settings.get('bedrock_enabled', 1)))
        await self._refresh(interaction)

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
                          '• **Challenge/Match** — where matched pairs get posted (falls back to Queue if unset)\n'
                          '• **Result** — where final results get posted\n'
                          '• **Logs** — staff activity logs',
                          color=PURPLE),
            view=CompChannelsView(self.bot), ephemeral=True)

    @discord.ui.button(label='Set Ping Role', emoji='🔔', style=discord.ButtonStyle.secondary, row=2)
    async def set_ping_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=E.base('🔔  Match Ping Role', 'Pick the role pinged whenever two players get matched.', color=PURPLE),
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
        await interaction.response.send_message(embed=embed, view=CompQueuePanelView(self.bot))
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
        embed = comp_admin_embed(interaction.guild, settings, java_gm, bedrock_gm, len(active), len(queue))
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
                    f'**Tier:** {player["tier_label"]}\n**Record:** {player["wins"]}W / {player["losses"]}L',
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


async def setup(bot):
    await bot.add_cog(CompFight(bot))
