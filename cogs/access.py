from __future__ import annotations
import logging
import datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import utils.embeds as E

logger = logging.getLogger('TicketBot.access')

# ═══════════════════════════════════════════════════════════════════════════
#  /access  —  PREMIUM ACCESS SYSTEM
#
#  Only the bot's REAL owner (the actual owner of the application in the
#  Discord Developer Portal — discord.py verifies this itself, it cannot
#  be spoofed by any role or permission) can grant or revoke premium
#  access for a user. Server admins, people with Manage Server, or any
#  regular member can never use this, no matter what Discord permissions
#  they have — the check is based on the bot application's real owner
#  identity, not on server permissions.
#
#  To gate any feature in another cog/command as "premium-only", just
#  import the `has_access()` helper below and use it like this:
#
#      from cogs.access import has_access
#      if not await has_access(bot, interaction.user.id):
#          return await interaction.response.send_message(
#              embed=E.error("This feature is only available to premium access users."),
#              ephemeral=True
#          )
# ═══════════════════════════════════════════════════════════════════════════


async def _ensure_table(bot: commands.Bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS premium_access (
                user_id     INTEGER PRIMARY KEY,
                granted_by  INTEGER NOT NULL,
                granted_at  TEXT    DEFAULT (datetime('now')),
                note        TEXT    DEFAULT ''
            )
        ''')
        await db.commit()


async def has_access(bot: commands.Bot, user_id: int) -> bool:
    """Return True if the given user currently has premium access."""
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        async with db.execute(
            'SELECT 1 FROM premium_access WHERE user_id = ?', (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def grant_access(bot: commands.Bot, user_id: int, granted_by: int, note: str = ''):
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT OR REPLACE INTO premium_access (user_id, granted_by, granted_at, note) '
            "VALUES (?, ?, datetime('now'), ?)",
            (user_id, granted_by, note)
        )
        await db.commit()


async def revoke_access(bot: commands.Bot, user_id: int) -> bool:
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        cur = await db.execute('DELETE FROM premium_access WHERE user_id = ?', (user_id,))
        await db.commit()
        return cur.rowcount > 0


async def list_access(bot: commands.Bot) -> list[dict]:
    await _ensure_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM premium_access ORDER BY granted_at DESC'
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


class AccessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await _ensure_table(self.bot)

    # ─────────────────────────── Owner Gate ──────────────────────────────
    # is_owner() fetches application_info() internally and caches the
    # real owner's (or team owner's) ID — this cannot be overridden by
    # any role, server permission, or config file.
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            embed=E.error(
                '❌ This command can only be used by the **real owner** of the bot.\n'
                'Server admins or any regular member cannot use this.'
            ),
            ephemeral=True
        )
        return False

    access_group = app_commands.Group(
        name='access',
        description='Premium access system (bot owner only).'
    )

    # ─────────────────────────── /access grant ───────────────────────────
    @access_group.command(name='grant', description='(Owner only) Grant premium access to a user.')
    @app_commands.describe(user='User to grant access to', note='Optional note (reason/plan/etc.)')
    async def grant(self, interaction: discord.Interaction, user: discord.User, note: str = ''):
        if not await self._require_owner(interaction):
            return

        await grant_access(self.bot, user.id, interaction.user.id, note)
        logger.info(f'Premium access granted to {user} ({user.id}) by owner {interaction.user} ({interaction.user.id})')

        await interaction.response.send_message(
            embed=E.success(f'✅ {user.mention} has been granted premium access.' + (f'\n📝 Note: {note}' if note else '')),
            ephemeral=True
        )

    # ─────────────────────────── /access revoke ──────────────────────────
    @access_group.command(name='revoke', description='(Owner only) Revoke premium access from a user.')
    @app_commands.describe(user='User to revoke access from')
    async def revoke(self, interaction: discord.Interaction, user: discord.User):
        if not await self._require_owner(interaction):
            return

        removed = await revoke_access(self.bot, user.id)
        logger.info(f'Premium access revoke attempt for {user} ({user.id}) by owner {interaction.user} ({interaction.user.id}) — removed={removed}')

        if removed:
            await interaction.response.send_message(
                embed=E.success(f'✅ Premium access has been removed from {user.mention}.'),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=E.error(f'{user.mention} did not have premium access to begin with.'),
                ephemeral=True
            )

    # ─────────────────────────── /access list ────────────────────────────
    @access_group.command(name='list', description='(Owner only) List all users with premium access.')
    async def list_(self, interaction: discord.Interaction):
        if not await self._require_owner(interaction):
            return

        rows = await list_access(self.bot)
        if not rows:
            return await interaction.response.send_message(
                embed=E.base('Premium Access List', 'No one has been granted access yet.'),
                ephemeral=True
            )

        lines = []
        for r in rows:
            lines.append(f"<@{r['user_id']}> — granted by <@{r['granted_by']}> on {r['granted_at']}" + (f" — _{r['note']}_" if r['note'] else ''))

        await interaction.response.send_message(
            embed=E.base('Premium Access List', '\n'.join(lines)),
            ephemeral=True
        )

    # ─────────────────────────── /access check ───────────────────────────
    @access_group.command(name='check', description='Check if a user currently has premium access.')
    @app_commands.describe(user='User to check (defaults to yourself)')
    async def check(self, interaction: discord.Interaction, user: discord.User = None):
        target = user or interaction.user
        allowed = await has_access(self.bot, target.id)

        await interaction.response.send_message(
            embed=(E.success(f'{target.mention} **has** premium access.')
                   if allowed else
                   E.error(f'{target.mention} does **not** have premium access.')),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AccessCog(bot))
        
