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
