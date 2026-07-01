from __future__ import annotations
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import PURPLE
import utils.embeds as E
from cogs.tickets import create_ticket, TicketControlView

logger = logging.getLogger('TicketBot.tier_test')


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL  (same field style as the main ticket modal)
# ═══════════════════════════════════════════════════════════════════════════════

class TierApplicationModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f'🎮  {edition} Tier Test', timeout=300)
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
        self.region = discord.ui.TextInput(
            label='Region',
            placeholder='e.g. EU, NA, ASIA',
            required=True,
            max_length=50,
        )

        self.add_item(self.ign)
        self.add_item(self.age)
        self.add_item(self.region)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {
            'Minecraft IGN': self.ign.value,
            'Age': self.age.value,
            'Region': self.region.value,
        }
        # Reuses the exact same ticket-creation pipeline as /newticket:
        # same channel setup, same welcome embed style, same DB row,
        # same logging, same auto-transcript-on-close.
        await create_ticket(self.bot, interaction, f'Tier Test • {self.edition}', answers)


# ═══════════════════════════════════════════════════════════════════════════════
#  PANEL VIEW
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _open(self, interaction: discord.Interaction, edition: str):
        db = self.bot.db

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
        title='🎮 Mine Front Tier Testing',
        description=(
            f'Welcome to **{guild.name}** Tier Testing!\n\n'
            'Choose your edition below to apply for a tier test.\n'
            'Our team will get back to you as soon as possible.\n\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n'
            '🟩 **Java Edition** — Apply for a Java tier test\n'
            '🟦 **Bedrock Edition** — Apply for a Bedrock tier test\n'
            '━━━━━━━━━━━━━━━━━━━━━━\n\n'
            '> ⚠️ One ticket per user · Please be patient'
        ),
        color=PURPLE,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
        e.set_author(name=f'{guild.name} Tier Testing', icon_url=guild.icon.url)
    else:
        e.set_author(name=f'{guild.name} Tier Testing')
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

    @app_commands.command(name='tierpanel', description='Post the tier tester application panel.')
    @app_commands.default_permissions(administrator=True)
    async def tierpanel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=tier_panel_embed(interaction.guild),
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
