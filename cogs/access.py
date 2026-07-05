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
#  Sirf bot ka REAL owner (jo Discord Developer Portal me application ka
#  owner hai — discord.py isko khud verify karta hai, koi role/permission
#  spoof nahi kar sakta) hi kisi user ko premium access grant/revoke kar
#  sakta hai. Server admin, manage-server wale, ya koi bhi normal member
#  isko kabhi bhi use nahi kar payega, chahe unke paas Administrator
#  permission hi kyun na ho — check Discord permissions par nahi, sirf
#  bot application owner ki asli identity par based hai.
#
#  Doosre cogs/commands me kisi feature ko "premium-only" banane ke liye
#  bas neeche diya hua `has_access()` helper import karke use karo:
#
#      from cogs.access import has_access
#      if not await has_access(bot, interaction.user.id):
#          return await interaction.response.send_message(
#              embed=E.error("Ye feature sirf premium access wale users ke liye hai."),
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
    # is_owner() discord.py ke andar hi application_info() fetch karke
    # asli owner (ya team owner) ki ID cache karta hai — ye kisi role,
    # server permission, ya config file se override nahi hota.
    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            embed=E.error(
                '❌ Ye command sirf bot ke **real owner** hi use kar sakte hain.\n'
                'Server admin ya koi bhi normal member ise use nahi kar sakta.'
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
            embed=E.success(f'✅ {user.mention} ko premium access de diya gaya hai.' + (f'\n📝 Note: {note}' if note else '')),
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
                embed=E.success(f'✅ {user.mention} ka premium access hata diya gaya hai.'),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=E.error(f'{user.mention} ke paas pehle se koi premium access nahi tha.'),
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
                embed=E.base('Premium Access List', 'Abhi tak kisi ko bhi access nahi diya gaya hai.'),
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
            embed=(E.success(f'{target.mention} ke paas premium access **hai**.')
                   if allowed else
                   E.error(f'{target.mention} ke paas premium access **nahi** hai.')),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AccessCog(bot))
