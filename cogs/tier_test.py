import discord
from discord.ext import commands
from discord import app_commands
import logging

logger = logging.getLogger("TierPanel")


# =========================
# SAFE HELPERS
# =========================
def is_ticket_channel(channel: discord.TextChannel):
    return channel.name.startswith("tier-")


# =========================
# VIEW (CLAIM / CLOSE)
# =========================
class TierTicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        emoji="👑",
        custom_id="tier_claim_btn"
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_ticket_channel(interaction.channel):
                return await interaction.response.send_message(
                    "❌ Not a ticket channel", ephemeral=True
                )

            ticket = None
            try:
                ticket = await self.bot.db.get_ticket_by_channel(interaction.channel.id)
            except Exception:
                logger.exception("DB get_ticket failed")

            if ticket and ticket.get("claimed_by"):
                return await interaction.response.send_message(
                    "⚠️ Already claimed", ephemeral=True
                )

            try:
                await self.bot.db.claim_ticket(interaction.channel.id, interaction.user.id)
            except Exception:
                logger.exception("DB claim failed")

            await interaction.response.send_message(
                f"👑 Claimed by {interaction.user.mention}"
            )

        except Exception:
            logger.exception("Claim error")
            await interaction.response.send_message("❌ Claim failed", ephemeral=True)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="tier_close_btn"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_ticket_channel(interaction.channel):
                return await interaction.response.send_message(
                    "❌ Not a ticket channel", ephemeral=True
                )

            await interaction.response.send_message("🔒 Closing ticket...", ephemeral=True)
            await interaction.channel.delete()

        except Exception:
            logger.exception("Close error")


# =========================
# MODAL
# =========================
class TierApplicationModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f"{edition} Application")
        self.bot = bot
        self.edition = edition

        self.ign = discord.ui.TextInput(label="Minecraft IGN", required=True)
        self.age = discord.ui.TextInput(label="Age", required=True, max_length=3)
        self.region = discord.ui.TextInput(label="Region", required=True)

        self.add_item(self.ign)
        self.add_item(self.age)
        self.add_item(self.region)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        category = discord.utils.get(guild.categories, name="Tickets")
        if not category:
            category = await guild.create_category("Tickets")

        channel = await guild.create_text_channel(
            name=f"tier-{user.name}",
            category=category
        )

        await channel.set_permissions(guild.default_role, view_channel=False)
        await channel.set_permissions(user, view_channel=True, send_messages=True)

        # SAFE DB (NO CRASH)
        try:
            await self.bot.db.create_ticket(
                user_id=user.id,
                channel_id=channel.id,
                ticket_type=f"tier-{self.edition.lower()}",
                claimed_by=None
            )
        except Exception:
            logger.exception("DB create_ticket failed")

        view = TierTicketControlView(self.bot)

        await channel.send(
            f"🎫 **Tier Ticket Created**\n"
            f"👤 {user.mention}\n"
            f"🎮 Edition: {self.edition}\n\n"
            f"Use buttons below to manage ticket.",
            view=view
        )

        await interaction.followup.send(
            f"✅ Ticket created: {channel.mention}",
            ephemeral=True
        )


# =========================
# PANEL VIEW
# =========================
class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Java Edition",
        style=discord.ButtonStyle.success,
        emoji="🟩",
        custom_id="tier_java_btn"
    )
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Java Edition")
        )

    @discord.ui.button(
        label="Bedrock Edition",
        style=discord.ButtonStyle.primary,
        emoji="🟦",
        custom_id="tier_bedrock_btn"
    )
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Bedrock Edition")
        )


# =========================
# COG
# =========================
class TierPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # persistent views (IMPORTANT)
        bot.add_view(TierPanelView(bot))
        bot.add_view(TierTicketControlView(bot))

    @app_commands.command(name="tierpanel", description="Open Tier Ticket Panel")
    async def tierpanel(self, interaction: discord.Interaction):

        embed = discord.Embed(
            title="🎮 Tier Tester Panel",
            description="Select Java or Bedrock to create a ticket",
            color=discord.Color.blurple()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
