import discord
from discord.ext import commands
from discord import app_commands
import logging

logger = logging.getLogger("TierPanel")


# =========================
# BUTTON VIEW (declare first to avoid crash)
# =========================
class TierTicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="👑")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            ticket = await self.bot.db.get_ticket_by_channel(interaction.channel.id)

            if not ticket:
                return await interaction.response.send_message("❌ Not a ticket", ephemeral=True)

            if ticket.get("claimed_by"):
                return await interaction.response.send_message("⚠️ Already claimed", ephemeral=True)

            await self.bot.db.claim_ticket(interaction.channel.id, interaction.user.id)

            await interaction.response.send_message(
                f"👑 Claimed by {interaction.user.mention}"
            )

        except Exception:
            logger.exception("Claim failed")
            await interaction.response.send_message("❌ Claim failed", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message("🔒 Closing ticket...", ephemeral=True)
            await interaction.channel.delete()
        except Exception:
            logger.exception("Close failed")


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
        self.region = discord.ui.TextInput(label="Region", required=True, max_length=50)

        self.add_item(self.ign)
        self.add_item(self.age)
        self.add_item(self.region)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)

            guild = interaction.guild
            user = interaction.user

            category = discord.utils.get(guild.categories, name="Tickets")
            if category is None:
                category = await guild.create_category("Tickets")

            channel = await guild.create_text_channel(
                name=f"tier-{user.name}",
                category=category
            )

            await channel.set_permissions(guild.default_role, view_channel=False)
            await channel.set_permissions(user, view_channel=True, send_messages=True)

            # SAFE DB CALL
            try:
                await self.bot.db.create_ticket(
                    user_id=user.id,
                    channel_id=channel.id,
                    ticket_type=f"tier-{self.edition.lower()}",
                    claimed_by=None
                )
            except Exception:
                logger.exception("DB error ignored")

            view = TierTicketControlView(self.bot)

            await channel.send(
                f"🎫 **Tier Ticket Created**\n"
                f"👤 {user.mention}\n"
                f"🎮 Edition: {self.edition}\n\n"
                f"Use buttons or /claim",
                view=view
            )

            await interaction.followup.send(
                f"✅ Ticket created: {channel.mention}",
                ephemeral=True
            )

        except Exception:
            logger.exception("Modal submit failed")
            try:
                await interaction.followup.send(
                    "❌ Something went wrong while creating ticket",
                    ephemeral=True
                )
            except:
                pass


# =========================
# PANEL VIEW
# =========================
class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Java Edition", style=discord.ButtonStyle.success, emoji="🟩")
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Java Edition")
        )

    @discord.ui.button(label="Bedrock Edition", style=discord.ButtonStyle.primary, emoji="🟦")
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

    @app_commands.command(name="tierpanel", description="Send Tier Panel")
    async def tierpanel(self, interaction: discord.Interaction):

        embed = discord.Embed(
            title="🎮 Tier Applications",
            description="Click below to apply for Java or Bedrock",
            color=discord.Color.blurple()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
