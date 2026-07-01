import discord
from discord.ext import commands
from discord import app_commands


class TierApplicationModal(discord.ui.Modal):
    def __init__(self, bot, edition: str):
        super().__init__(title=f"{edition} Application")
        self.bot = bot
        self.edition = edition

        self.ign = discord.ui.TextInput(
            label="Minecraft IGN",
            placeholder="Enter your Minecraft username",
            required=True,
            max_length=32
        )

        self.age = discord.ui.TextInput(
            label="Age",
            placeholder="Enter your age",
            required=True,
            max_length=3
        )

        self.region = discord.ui.TextInput(
            label="Region",
            placeholder="Country / Region",
            required=True,
            max_length=50
        )

        self.add_item(self.ign)
        self.add_item(self.age)
        self.add_item(self.region)

async def on_submit(self, interaction: discord.Interaction):
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

    # DB ENTRY (IMPORTANT)
    await self.bot.db.create_ticket(
        user_id=user.id,
        channel_id=channel.id,
        ticket_type=f"tier-{self.edition.lower()}",
        claimed_by=None
    )

    await channel.send(
        f"🎫 **Tier Ticket Created**\n"
        f"User: {user.mention}\n"
        f"Edition: {self.edition}\n\n"
        f"Use `/claim` to claim this ticket."
    )

    await interaction.response.send_message(
        f"✅ Ticket created: {channel.mention}",
        ephemeral=True
    )

        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets")

        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category
        )

        await channel.set_permissions(guild.default_role, view_channel=False)
        await channel.set_permissions(user, view_channel=True, send_messages=True)

        await channel.send(
            f"🎫 Ticket created for {user.mention}\n"
            f"IGN: {self.ign.value}\n"
            f"Age: {self.age.value}\n"
            f"Region: {self.region.value}"
        )

        await interaction.response.send_message(
            f"✅ Ticket created: {channel.mention}",
            ephemeral=True
        )


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


class TierPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.add_view(TierPanelView(bot))

    @app_commands.command(
        name="tierpanel",
        description="Send the Tier Tester panel."
    )
    @app_commands.default_permissions(administrator=True)
    async def tierpanel(self, interaction: discord.Interaction):

        embed = discord.Embed(
            title="🎮 Tier Tester Applications",
            description="Choose your edition below to apply.",
            color=discord.Color.blurple()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
