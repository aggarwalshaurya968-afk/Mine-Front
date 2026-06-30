import discord
from discord.ext import commands
from discord import app_commands
import json
import os

CONFIG_FILE = "tier_config.json"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}

    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)


class TierPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


class TierTest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="tiersetup",
        description="Setup Tier Tester System"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def tiersetup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        staff_role: discord.Role,
    ):

        data = load_config()

        data[str(interaction.guild.id)] = {
            "category": category.id,
            "staff_role": staff_role.id
        }

        save_config(data)

        await interaction.response.send_message(
            "✅ Tier Tester setup completed!",
            ephemeral=True
        )

    @app_commands.command(
        name="tierpanel",
        description="Send Tier Tester Panel"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def tierpanel(self, interaction: discord.Interaction):

        embed = discord.Embed(
            title="⚔️ Tier Tester Applications",
            description=(
                "Welcome to the **MineFront Tier Tester Application**.\n\n"
                "Select your Minecraft edition below to apply.\n\n"
                "• 🟩 Java Edition\n"
                "• 🟦 Bedrock Edition\n\n"
                "**Click the button below to continue.**"
            ),
            color=0x00ff88
        )

        embed.set_footer(text="MineFront • Tier Tester System")

        await interaction.response.send_message(
            embed=embed,
            view=TierPanelView()
        )
