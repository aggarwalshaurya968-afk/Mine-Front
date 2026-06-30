from __future__ import annotations
import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from config import PURPLE, PURPLE_DARK
import utils.embeds as E

logger = logging.getLogger('TicketBot.tiertest')


# ═══════════════════════════════════════════════════════════════════════════════
#  MODAL
# ═══════════════════════════════════════════════════════════════════════════════

class TierApplyModal(discord.ui.Modal, title='⚔️  Tier Tester Application'):
    ign = discord.ui.TextInput(
        label='IGN',
        placeholder='Type your message here...',
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    device = discord.ui.TextInput(
        label='Which device tester do you want to become?',
        placeholder='Type your message here...',
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    version = discord.ui.TextInput(
        label='Which version tester you want to become',
        placeholder='Java or bedrock/MCPE?',
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )
    gamemode = discord.ui.TextInput(
        label='Main game mode you wanted to be a tester on?',
        placeholder='Type your message here...',
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        embed = discord.Embed(
            title='⚔️  New Tier Tester Application',
            color=PURPLE,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name='👤  Applicant', value=interaction.user.mention, inline=True)
        embed.add_field(name='🎮  IGN', value=self.ign.value, inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=True)
        embed.add_field(name='📱  Device Tester', value=self.device.value, inline=False)
        embed.add_field(name='🧩  Version Tester', value=self.version.value, inline=False)
        embed.add_field(name='⚔️  Main Game Mode', value=self.gamemode.value, inline=False)
        embed.set_footer(text=f'User ID: {interaction.user.id}')

        channel_id = await db.get_tier_channel(interaction.guild_id)
        target = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

        if not target:
            return await interaction.followup.send(
                embed=E.error('Submission channel is not set up correctly. Please contact an admin.'),
                ephemeral=True
            )

        try:
            await target.send(embed=embed)
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error('I do not have permission to send messages in the submission channel.'),
                ephemeral=True
            )

        await db.log_tier_application(interaction.guild_id, interaction.user.id)

        await interaction.followup.send(
            embed=E.success('Your tier tester application has been submitted! Please wait for a response.'),
            ephemeral=True
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  VIEW (persistent)
# ═══════════════════════════════════════════════════════════════════════════════

class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Tier tester apply', emoji='⚔️',
                       style=discord.ButtonStyle.secondary, custom_id='tiertest:apply')
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db

        if await db.has_applied_recently(interaction.guild_id, interaction.user.id, hours=24):
            return await interaction.response.send_message(
                embed=E.error('You have already applied recently. Please wait before applying again.'),
                ephemeral=True
            )

        await interaction.response.send_modal(TierApplyModal(self.bot))


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class TierTestCog(commands.Cog, name='TierTest'):
    def __init__(self, bot):
        self.bot = bot
        # Re-register persistent view on startup so the button keeps working after a restart
        bot.add_view(TierPanelView(bot))

    @app_commands.command(name='tierpanel', description='Post the tier tester application panel.')
    @app_commands.describe(
        channel='Channel to post the panel in (default: current channel)',
        submit_channel='Channel where applications get submitted (default: same channel as the panel)'
    )
    @app_commands.default_permissions(administrator=True)
    async def tierpanel(self, interaction: discord.Interaction,
                        channel: discord.TextChannel | None = None,
                        submit_channel: discord.TextChannel | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        target = channel or interaction.channel
        sub_target = submit_channel or target

        await self.bot.db.set_tier_channel(guild.id, sub_target.id)

        embed = discord.Embed(
            title='⚔️  Tier Testing Application',
            description=(
                f'Welcome to **{guild.name}** Tier Testing!\n\n'
                'Passionate about PvP and want to help the community grow?\n'
                'Click the button below to apply as a Tier Tester.\n\n'
                '━━━━━━━━━━━━━━━━━━━━━━\n'
                '🎯  Experienced in multiple gamemodes\n'
                '🛡️  Responsible & trustworthy\n'
                '⭐  Fair evaluation guaranteed\n'
                '━━━━━━━━━━━━━━━━━━━━━━'
            ),
            color=PURPLE_DARK,
            timestamp=datetime.now(timezone.utc)
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            embed.set_author(name=f'{guild.name} Tier Testing', icon_url=guild.icon.url)
        else:
            embed.set_author(name=f'{guild.name} Tier Testing')
        embed.set_footer(text=f'{guild.name} • Tier Testing System')

        view = TierPanelView(self.bot)

        try:
            await target.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=E.error(f'I cannot send messages in {target.mention}.'), ephemeral=True
            )

        await interaction.followup.send(
            embed=E.success(
                f'Tier tester panel has been posted in {target.mention}!\n'
                f'Applications will be submitted to {sub_target.mention}.'
            ),
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(TierTestCog(bot))
