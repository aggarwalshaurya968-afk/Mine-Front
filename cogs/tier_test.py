import discord
from discord.ext import commands
from discord import app_commands
class TierPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Java Edition",
        emoji="🟩",
        style=discord.ButtonStyle.success,
        custom_id="tier_java"
    )
    async def java_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Java Edition")
        )

    @discord.ui.button(
        label="Bedrock Edition",
        emoji="🟦",
        style=discord.ButtonStyle.primary,
        custom_id="tier_bedrock"
    )
    async def bedrock_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_modal(
            TierApplicationModal(self.bot, "Bedrock Edition")
        )
class TierPanel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="tierpanel",
        description="Send the Tier Tester panel."
    )
    async def tierpanel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎮 Tier Tester Application",
            description="Click the button below to apply for Tier Tester.",
            color=discord.Color.green()
        )

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView(self.bot)
        )


async def setup(bot):
    await bot.add_cog(TierPanel(bot))
