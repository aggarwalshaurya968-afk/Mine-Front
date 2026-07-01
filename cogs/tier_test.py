from __future__ import annotations
import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE
import utils.embeds as E
# Reusing the SAME pipeline the support tickets use — this is what makes
# log channel, transcript channel, roles, claim/close/reopen/delete all
# work automatically for Tier Tester applications too. Nothing duplicated.
from cogs.tickets import create_ticket, TicketControlView

logger = logging.getLogger('TicketBot.tier_test')


# ═══════════════════════════════════════════════════════════════════════════════
#  TIER SETTINGS STORAGE
#  Self-contained table, created/managed only here — does not touch database.py
# ═══════════════════════════════════════════════════════════════════════════════

async def _ensure_tier_table(bot):
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tier_settings (
                guild_id         INTEGER PRIMARY KEY,
                java_enabled     INTEGER DEFAULT 1,
                bedrock_enabled  INTEGER DEFAULT 1
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
    return {'guild_id': guild_id, 'java_enabled': 1, 'bedrock_enabled': 1}


async def set_tier_toggle(bot, guild_id: int, field: str, enabled: bool):
    await _ensure_tier_table(bot)
    async with aiosqlite.connect(bot.db.db_path) as db:
        await db.execute('INSERT OR IGNORE INTO tier_settings (guild_id) VALUES (?)', (guild_id,))
        await db.execute(
            f'UPDATE tier_settings SET {field} = ? WHERE guild_id = ?',
            (1 if enabled else 0, guild_id)
        )
        await db.commit()


def tier_settings_embed(guild: discord.Guild, settings: dict) -> discord.Embed:
    java = '✅ Enabled' if settings.get('java_enabled', 1) else '❌ Disabled'
    bedrock = '✅ Enabled' if settings.get('bedrock_enabled', 1) else '❌ Disabled'
    e = discord.Embed(
        title='🎮  Tier Tester Settings',
        description=f'Managing Tier Tester applications for **{guild.name}**\n\n'
                    'Toggle which editions can currently apply to become a Tier Tester.',
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name='🟩 Java Edition', value=java, inline=True)
    e.add_field(name='🟦 Bedrock Edition', value=bedrock, inline=True)
    e.set_footer(text='Mine Front Ticket System  •  Tier Tester Settings')
    return e


class TierSettingsView(discord.ui.View):
    def __init__(self, bot, settings: dict):
        super().__init__(timeout=120)
        self.bot = bot
        self.settings = settings

    @discord.ui.button(label='Toggle Java', emoji='🟩', style=discord.ButtonStyle.success)
    async def toggle_java(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = not bool(self.settings.get('java_enabled', 1))
        await set_tier_toggle(self.bot, interaction.guild_id, 'java_enabled', new_val)
        self.settings['java_enabled'] = int(new_val)
        status = '✅ Enabled' if new_val else '❌ Disabled'
        await interaction.response.send_message(
            embed=E.success(f'Java Edition Tier Tester applications: **{status}**'), ephemeral=True)

    @discord.ui.button(label='Toggle Bedrock', emoji='🟦', style=discord.ButtonStyle.primary)
    async def toggle_bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = not bool(self.settings.get('bedrock_enabled', 1))
        await set_tier_toggle(self.bot, interaction.guild_id, 'bedrock_enabled', new_val)
        self.settings['bedrock_enabled'] = int(new_val)
        status = '✅ Enabled' if new_val else '❌ Disabled'
        await interaction.response.send_message(
            embed=E.success(f'Bedrock Edition Tier Tester applications: **{status}**'), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL — Tier TESTER staff application (not "get my tier tested")
#  Same visual field style as the main ticket modal.
# ═══════════════════════════════════════════════════════════════════════════════

class TierApplicationModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f'🎮  {edition} Tier Tester App', timeout=300)
        self.bot = bot
        self.edition = edition

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
            'Age': self.age.value,
            'Why do you want to become a Tier Tester?': self.why.value,
            'Previous Experience': self.experience.value,
        }
        # Reuses the exact same ticket-creation pipeline as /newticket:
        # same channel setup, same welcome embed style, same DB row,
        # same log-channel logging, same auto-transcript-on-close,
        # same Claim/Close/Reopen/Transcript/Delete buttons.
        await create_ticket(self.bot, interaction, f'Tier Tester App • {self.edition}', answers)


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL VIEW
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _open(self, interaction: discord.Interaction, edition: str):
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
        cooldown = settings.get('cooldown_seconds', 300) if settings else 300
        remaining = await db.check_cooldown(interaction.guild_id, interaction.user.id, cooldown)
        if remaining > 0:
            return await interaction.response.send_message(
                embed=E.error(f'Please wait **{remaining}s** before opening another ticket.'),
                ephemeral=True
            )

        await interaction.response.send_modal(TierApplicationModal(self.bot, edition))

    @discord.ui.button(label='Java Edition', emoji='🟩',
                       style=discord.ButtonStyle.success, custom_id='tier:java')
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, 'Java Edition')

    @discord.ui.button(label='Bedrock Edition', emoji='🟦',
                       style=discord.ButtonStyle.primary, custom_id='tier:bedrock')
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, 'Bedrock Edition')


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL EMBED  (same visual style as the support ticket panel)
# ═══════════════════════════════════════════════════════════════════════════════

def tier_panel_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title='🎮 Mine Front Tier Tester Applications',
        description=(
            f'Want to become a **Tier Tester** at **{guild.name}**?\n\n'
            'Choose your edition below and fill out the application.\n'
            'Our team will review it and get back to you soon.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            '🟩 **Java Edition** — Apply to test Java tiers\n'
            '🟦 **Bedrock Edition** — Apply to test Bedrock tiers\n'
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
    e.set_footer(text=f'{guild.name} • Premium Support System')
    return e


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanel(commands.Cog, name='TierTest'):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TierPanelView(bot))
        # NOTE: TicketControlView is already registered persistently by TicketsCog,
        # so it is not re-registered here to avoid touching that flow.

    @app_commands.command(name='tierpanel', description='Post the Tier Tester application panel.')
    @app_commands.default_permissions(administrator=True)
    async def tierpanel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=tier_panel_embed(interaction.guild),
            view=TierPanelView(self.bot)
        )

    # /tiersettings lives here too — it uses the SAME guild_settings (log channel,
    # transcript channel, roles, cooldown) as your support tickets, because
    # create_ticket() above is the shared function. This command only controls
    # whether the Java/Bedrock application buttons are open or closed.
    @app_commands.command(name='tiersettings', description='Manage Tier Tester application toggles (Java/Bedrock on-off).')
    @app_commands.default_permissions(administrator=True)
    async def tiersettings(self, interaction: discord.Interaction):
        settings = await get_tier_settings(self.bot, interaction.guild_id)
        await interaction.response.send_message(
            embed=tier_settings_embed(interaction.guild, settings),
            view=TierSettingsView(self.bot, settings),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
