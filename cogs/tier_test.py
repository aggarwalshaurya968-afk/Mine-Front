import discord
from discord.ext import commands
from discord import app_commands

class TierTest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="tierpanel",
        description="Send the Tier Tester panel."
    )
    async def tierpanel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚔️ Tier Tester Applications",
            description="Select the option below to apply for Java or Bedrock Tier Testing.",
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(TierTest(bot))
