from __future__ import annotations
import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE, PURPLE_DARK, ORANGE, GREY
import utils.embeds as E
# Reusing the SAME pipeline the support tickets use — this is what makes
# log channel, transcript channel, roles, claim/close/reopen/delete all
# work automatically for Tier Tester applications too. Nothing duplicated.
from cogs.tickets import create_ticket, TicketControlView

logger = logging.getLogger('TicketBot.tier_test')

# Path to the panel banner image. Place your image file at this exact path
# inside your bot project: Mine-Front-main/assets/tier_testing_banner.png
BANNER_PATH = 'assets/tier_testing_banner.png'
BANNER_FILENAME = 'tier_testing_banner.png'

# ═══════════════════════════════════════════════════════════════════════════════
#  GAMEMODES — separate list per edition. Pick your edition first, then only
#  that edition's gamemodes show up in the dropdown.
#
#  These lists are only used as the DEFAULT seed the first time a server's
#  gamemodes are looked up. After that, each server's list lives in the
#  tier_gamemodes table and can be freely edited from /tieradminpanel without
#  ever touching this file again.
# ═══════════════════════════════════════════════════════════════════════════════

# Java Edition gamemodes
JAVA_GAMEMODES = [
    ('Overall (All Game Modes)', '🌐'),
    ('Nethpot', '🧪'),
    ('Axe', '🪓'),
    ('Dia Pot', '💎'),
    ('Mace', '<:z_mace:1523281275173998602>'),
    ('Spear Mace', '🔱'),
    ('Cart PvP', '🛒'),
    ('Build UHC', '<:z_builduhc:1523281258728394843>'),
    ('Crystal', '<:z_crystalpvp:1523281271122559076>'),
    ('SMP PvP', '🌍'),
    ('UHC', '❤️'),
    ('Boxing', '<:z_boxing:1523281247328276540>'),
    ('No Debuff', '<:z_nodebuff:1523281283420258304>'),
    ('MLG Rush', '<:z_mlgrush:1523281279578017812>'),
    ('BedFight', '<:z_bedfight:1523281245184725154>'),
    ('SkyWars', '<:z_skywars:1523281287392133310>'),
    ('MidFight', '<:z_midfight:1348716069673762826>'),
    ('Battle Rush', '<:z_battlerush:1523281243242762330>'),
]

# Bedrock Edition gamemodes — using your server's exact custom emojis.
BEDROCK_GAMEMODES = [
    ('Boxing', '<:z_boxing:1523281247328276540>'),
    ('MLG Rush', '<:z_mlgrush:1523281279578017812>'),
    ('No Debuff', '<:z_nodebuff:1523281283420258304>'),
    ('BedFight', '<:z_bedfight:1523281245184725154>'),
    ('Build UHC', '<:z_builduhc:1523281258728394843>'),
    ('SkyWars', '<:z_skywars:1523281287392133310>'),
    ('MidFight', '<:z_midfight:1523281277393043626>'),
    ('Battle Rush', '<:z_battlerush:1523281243242762330>'),
    ('Bridge', '<:z_bridge:1523281249194610800>'),
    ('Build', '<:z_build:1523281251484700753>'),
    ('Cave UHC', '<:z_caveuhc:1523281260871553176>'),
    ('Mace', '<:z_mace:1523281275173998602>'),
]

GAMEMODES_BY_EDITION = {
    'Java Edition': JAVA_GAMEMODES,
    'Bedrock Edition': BEDROCK_GAMEMODES,
}

EDITIONS = ('Java Edition', 'Bedrock Edition')


# ═══════════════════════════════════════════════════════════════════════════════
#  STORAGE — self-contained tables, created/managed only here.
#  Does not touch database.py or any table used by the ticket system.
# ═══════════════════════════════════════════════════════════════════════════════

async def _ensure_tier_table(bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tier_settings (
                guild_id              INTEGER PRIMARY KEY,
                java_enabled          INTEGER DEFAULT 1,
                bedrock_enabled       INTEGER DEFAULT 1,
                banner_url            TEXT,
                tier_cooldown_seconds INTEGER
            )
        ''')
        # Migration safety net in case an older version of this bot already
        # created tier_settings without the newer columns.
        for col, coltype in (('banner_url', 'TEXT'), ('tier_cooldown_seconds', 'INTEGER')):
            try:
                await db.execute(f'ALTER TABLE tier_settings ADD COLUMN {col} {coltype}')
            except Exception:
                pass  # column already exists

        await db.execute('''
            CREATE TABLE IF NOT EXISTS tier_gamemodes (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                edition  TEXT NOT NULL,
                name     TEXT NOT NULL,
                emoji    TEXT NOT NULL
            )
        ''')
        await db.commit()


async def get_tier_settings(bot, guild_id: int) -> dict:
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT * FROM tier_settings WHERE guild_id = ?', (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.commit()
    return {
        'guild_id': guild_id,
        'java_enabled': 1,
        'bedrock_enabled': 1,
        'banner_url': None,
        'tier_cooldown_seconds': None,
    }


async def set_tier_toggle(bot, guild_id: int, field: str, enabled: bool):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(
            f'UPDATE tier_settings SET {field} = ? WHERE guild_id = ?',
            (1 if enabled else 0, guild_id)
        )
        await db.commit()


async def set_banner_url(bot, guild_id: int, url: str | None):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET banner_url = ? WHERE guild_id = ?', (url, guild_id))
        await db.commit()


async def set_tier_cooldown(bot, guild_id: int, seconds: int | None):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute('UPDATE tier_settings SET tier_cooldown_seconds = ? WHERE guild_id = ?', (seconds, guild_id))
        await db.commit()


async def get_gamemodes(bot, guild_id: int, edition: str) -> list[tuple[str, str]]:
    """Returns this guild's gamemode list for an edition. The first time it's
    called for a guild, it seeds the table with the built-in defaults above,
    so every server starts out with the same list but can customize it
    independently from then on."""
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            'SELECT name, emoji FROM tier_gamemodes WHERE guild_id = ? AND edition = ? ORDER BY id',
            (guild_id, edition)
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [(r['name'], r['emoji']) for r in rows]

        defaults = GAMEMODES_BY_EDITION.get(edition, [])
        if defaults:
            await db.executemany(
                'INSERT INTO tier_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
                [(guild_id, edition, name, emoji) for name, emoji in defaults]
            )
            await db.commit()
        return list(defaults)


async def add_gamemode(bot, guild_id: int, edition: str, name: str, emoji: str) -> tuple[bool, str]:
    current = await get_gamemodes(bot, guild_id, edition)
    if len(current) >= 25:
        return False, 'You can have a maximum of **25** gamemodes per edition (Discord dropdown limit).'
    if any(n.lower() == name.lower() for n, _ in current):
        return False, f'**{name}** already exists in {edition}.'
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'INSERT INTO tier_gamemodes (guild_id, edition, name, emoji) VALUES (?, ?, ?, ?)',
            (guild_id, edition, name, emoji)
        )
        await db.commit()
    return True, f'Added **{emoji} {name}** to {edition}.'


async def remove_gamemode(bot, guild_id: int, edition: str, name: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'DELETE FROM tier_gamemodes WHERE guild_id = ? AND edition = ? AND name = ?',
            (guild_id, edition, name)
        )
        await db.commit()


async def reset_gamemodes(bot, guild_id: int, edition: str):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute(
            'DELETE FROM tier_gamemodes WHERE guild_id = ? AND edition = ?',
            (guild_id, edition)
        )
        await db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL — Tier TESTER staff application (not "get my tier tested")
#  Same visual field style as the main ticket modal.
# ═══════════════════════════════════════════════════════════════════════════════

class TierApplicationModal(discord.ui.Modal):
    def __init__(self, bot, edition: str, gamemode: str):
        super().__init__(title=f'🎮  {edition} Tier Tester App', timeout=300)
        self.bot = bot
        self.edition = edition
        self.gamemode = gamemode

        self.ign = discord.ui.TextInput(
            label='Minecraft IGN',
            placeholder='Your exact in-game username',
            required=True,
            max_length=32,
        )
        self.age = discord.ui.TextInput(
            label='Age',
            placeholder='Your age',
            required=True,
            max_length=3,
        )
        self.why = discord.ui.TextInput(
            label='Why do you want to become a Tier Tester?',
            style=discord.TextStyle.paragraph,
            placeholder='Tell us your motivation…',
            required=True,
            max_length=500,
        )
        self.experience = discord.ui.TextInput(
            label='Previous testing/staff experience',
            style=discord.TextStyle.paragraph,
            placeholder='List any relevant experience, or write "None"',
            required=True,
            max_length=500,
        )

        self.add_item(self.ign)
        self.add_item(self.age)
        self.add_item(self.why)
        self.add_item(self.experience)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {
            'Minecraft IGN': self.ign.value,
            'Gamemode': self.gamemode,
            'Age': self.age.value,
            'Why do you want to become a Tier Tester?': self.why.value,
            'Previous Experience': self.experience.value,
        }
        # Reuses the exact same ticket-creation pipeline as /newticket:
        # same channel setup, same welcome embed style, same DB row,
        # same log-channel logging, same auto-transcript-on-close,
        # same Claim/Close/Reopen/Transcript/Delete buttons.
        await create_ticket(self.bot, interaction, f'Tier Tester App • {self.gamemode} • {self.edition}', answers)


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL VIEW  (what members see & use to apply)
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _pick_edition(self, interaction: discord.Interaction, edition: str):
        db = self.bot.db

        tier_settings = await get_tier_settings(self.bot, interaction.guild_id)
        field = 'java_enabled' if edition == 'Java Edition' else 'bedrock_enabled'
        if not tier_settings.get(field, 1):
            return await interaction.response.send_message(
                embed=E.error(f'{edition} Tier Tester applications are currently closed.'),
                ephemeral=True
            )

        if await db.is_blacklisted(interaction.guild_id, interaction.user.id):
            return await interaction.response.send_message(
                embed=E.error('You are blacklisted from creating tickets.'), ephemeral=True
            )

        existing = await db.get_open_ticket(interaction.guild_id, interaction.user.id)
        if existing:
            ch = interaction.guild.get_channel(existing['channel_id'])
            msg = (f'You already have an open ticket: {ch.mention}'
                   if ch else 'You already have an open ticket.')
            return await interaction.response.send_message(embed=E.error(msg), ephemeral=True)

        settings = await db.get_settings(interaction.guild_id)
        default_cooldown = settings.get('cooldown_seconds', 300) if settings else 300
        # Tier Tester applications can optionally use their own cooldown,
        # set from /tieradminpanel. Falls back to the ticket system default.
        cooldown = tier_settings.get('tier_cooldown_seconds') or default_cooldown
        remaining = await db.check_cooldown(interaction.guild_id, interaction.user.id, cooldown)
        if remaining > 0:
            return await interaction.response.send_message(
                embed=E.error(f'Please wait **{remaining}s** before opening another ticket.'),
                ephemeral=True
            )

        gamemodes = await get_gamemodes(self.bot, interaction.guild_id, edition)
        if not gamemodes:
            return await interaction.response.send_message(
                embed=E.error(f'No gamemodes are configured for {edition} yet. Ask an admin to add some via /tieradminpanel.'),
                ephemeral=True
            )

        await interaction.response.send_message(
            content=f'**{edition}** selected. Now pick a gamemode:',
            view=GamemodeSelectView(self.bot, edition, gamemodes),
            ephemeral=True,
        )

    @discord.ui.button(label='Java Edition', emoji='🟩',
                       style=discord.ButtonStyle.success, custom_id='tier:java')
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_edition(interaction, 'Java Edition')

    @discord.ui.button(label='Bedrock Edition', emoji='🟦',
                       style=discord.ButtonStyle.primary, custom_id='tier:bedrock')
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_edition(interaction, 'Bedrock Edition')


class GamemodeSelectView(discord.ui.View):
    """Shown ephemerally after the user picks their edition. Only lists the
    gamemodes for that specific edition, pulled live from this guild's
    (possibly customized) list."""

    def __init__(self, bot, edition: str, gamemodes: list[tuple[str, str]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.edition = edition

        self.gamemode_select.options = [
            discord.SelectOption(label=name, value=name, emoji=emoji)
            for name, emoji in gamemodes
        ]
        self.gamemode_select.placeholder = f'Choose a {edition} gamemode…'

    @discord.ui.select()
    async def gamemode_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        gamemode = select.values[0]
        await interaction.response.send_modal(TierApplicationModal(self.bot, self.edition, gamemode))


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL EMBED  (same visual style as the support ticket panel + banner image)
# ═══════════════════════════════════════════════════════════════════════════════

async def tier_panel_embed(bot, guild: discord.Guild) -> discord.Embed:
    settings = await get_tier_settings(bot, guild.id)
    java_gamemodes = await get_gamemodes(bot, guild.id, 'Java Edition')
    bedrock_gamemodes = await get_gamemodes(bot, guild.id, 'Bedrock Edition')

    java_lines = '\n'.join(f'{emoji} **{name}**' for name, emoji in java_gamemodes) or '_None configured_'
    bedrock_lines = '\n'.join(f'{emoji} **{name}**' for name, emoji in bedrock_gamemodes) or '_None configured_'

    java_closed_note = '' if settings.get('java_enabled', 1) else ' *(currently closed)*'
    bedrock_closed_note = '' if settings.get('bedrock_enabled', 1) else ' *(currently closed)*'

    e = discord.Embed(
        title='🎮 Mine Front Tier Tester Applications',
        description=(
            f'Want to become a **Tier Tester** at **{guild.name}**?\n\n'
            'Pick your edition below, then choose the gamemode you want to test '
            'and fill out the application.\n'
            'Our team will review it and get back to you soon.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            f'🟩 **Java Edition gamemodes**{java_closed_note}\n{java_lines}\n\n'
            f'🟦 **Bedrock Edition gamemodes**{bedrock_closed_note}\n{bedrock_lines}\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n\n'
            '> ⚠️ One application per user · Please be patient'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
        e.set_author(name=f'{guild.name} Tier Tester Applications', icon_url=guild.icon.url)
    else:
        e.set_author(name=f'{guild.name} Tier Tester Applications')

    if settings.get('banner_url'):
        e.set_image(url=settings['banner_url'])
    else:
        e.set_image(url=f'attachment://{BANNER_FILENAME}')
    e.set_footer(text=f'{guild.name} • Premium Support System')
    return e


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER ADMIN PANEL — manage EVERY part of the tier tester system from one place
# ═══════════════════════════════════════════════════════════════════════════════

def tier_admin_embed(guild: discord.Guild, settings: dict,
                      java_gamemodes: list, bedrock_gamemodes: list,
                      default_cooldown: int) -> discord.Embed:
    java = '✅ Open' if settings.get('java_enabled', 1) else '❌ Closed'
    bedrock = '✅ Open' if settings.get('bedrock_enabled', 1) else '❌ Closed'
    banner = settings.get('banner_url') or f'Default local image (`{BANNER_PATH}`)'
    custom_cd = settings.get('tier_cooldown_seconds')
    cooldown_text = (f'**{custom_cd}s** (custom override)' if custom_cd
                      else f'**{default_cooldown}s** (using ticket system default)')

    e = discord.Embed(
        title='🛠️  Tier Tester — Admin Panel',
        description=(
            f'Full control over Tier Tester applications on **{guild.name}**.\n'
            'Use the buttons below to manage every part of the panel.\n\n'
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
    e.add_field(name='⏱️  Cooldown', value=cooldown_text, inline=True)
    e.add_field(name='🖼️  Banner', value=banner[:1024], inline=True)
    e.add_field(name='\u200b', value='\u200b', inline=True)
    e.add_field(name='🟩  Java Gamemodes', value=f'{len(java_gamemodes)}/25 configured', inline=True)
    e.add_field(name='🟦  Bedrock Gamemodes', value=f'{len(bedrock_gamemodes)}/25 configured', inline=True)
    e.set_footer(text='Mine Front Ticket System  •  Tier Admin Panel')
    return e


def gamemode_manage_embed(edition: str, gamemodes: list[tuple[str, str]]) -> discord.Embed:
    lines = '\n'.join(f'
