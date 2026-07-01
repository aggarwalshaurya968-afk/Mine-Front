import discord
from discord.ext import commands
from discord import app_commands
import logging

logger = logging.getLogger("TierPanel")


# =========================================================
# HELPERS
# =========================================================
def is_ticket_channel(channel: discord.TextChannel):
    return channel.name.startswith("tier-") or channel.name.startswith("ticket-")


# =========================================================
# TICKET CONTROL VIEW
# =========================================================
class TierTicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        emoji="👑",
        custom_id="tier_claim"
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_ticket_channel(interaction.channel):
                return await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)

            # DB optional fallback safe
            ticket = None
            try:
                ticket = await self.bot.db.get_ticket_by_channel(interaction.channel.id)
            except:
                pass

            if ticket and ticket.get("claimed_by"):
                return await interaction.response.send_message("⚠️ Already claimed", ephemeral=True)

            try:
                await self.bot.db.claim_ticket(interaction.channel.id, interaction.user.id)
            except:
                logger.exception("DB claim failed (ignored)")

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
        custom_id="tier_close"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not is_ticket_channel(interaction.channel):
                return await interaction.response.send_message("❌ Not a ticket", ephemeral=True)

            await interaction.response.send_message("🔒 Closing ticket...", ephemeral=True)
            await interaction.channel.delete()

        except Exception:
            logger.exception("Close error")


# =========================================================
# MODAL
# =========================================================
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
            if not category:
                category = await guild.create_category("Tickets")

            channel = await guild.create_text_channel(
                name=f"tier-{user.name}",
                category=category
            )

            await channel.set_permissions(guild.default_role, view_channel=False)
            await channel.set_permissions(user, view_channel=True, send_messages=True)

            # DB safe (not required for working)
            try:
                await self.bot.db.create_ticket(
                    user_id=user.id,
                    channel_id=channel.id,
                    ticket_type=f"tier-{self.edition}",
                    claimed_by=None
                )
            except:
                logger.exception("DB create ignored")

            embed = discord.Embed(
                title="🎫 Tier Ticket Created",
                description=f"""
👤 **User:** {user.mention}
🎮 **Edition:** {self.edition}

📌 Support will assist you soon.
Use buttons below to manage ticket.
""",
                color=discord.Color.blurple()
            )

            view = TierTicketControlView(self.bot)

            await channel.send(embed=embed, view=view)

            await interaction.followup.send(
                f"✅ Ticket created: {channel.mention}",
                ephemeral=True
            )

        except Exception:
            logger.exception("Modal error")
            try:
                await interaction.followup.send("❌ Error creating ticket", ephemeral=True)
            except:
                pass


# =========================================================
# PANEL VIEW
# =========================================================
class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Java Edition",
        style=discord.ButtonStyle.success,
        emoji="🟩",
        custom_id="tier_java"
    )
    async def java(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Java Edition")
        )

    @discord.ui.button(
        label="Bedrock Edition",
        style=discord.ButtonStyle.primary,
        emoji="🟦",
        custom_id="tier_bedrock"
    )
    async def bedrock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Bedrock Edition")
        )


# =========================================================
# COG
# =========================================================
class TierPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TierPanelView(bot))
        bot.add_view(TierTicketControlView(bot))

    @app_commands.command(name="tierpanel", description="Open Tier Panel")
    async def tierpanel(self, interaction: discord.Interaction):

        embed = discord.Embed(
            title="🎮 Tier Tester Apply",
            description="Choose your edition below to apply",
            color=discord.Color.green()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
