import os
import sys
import asyncio
import logging
import discord
from discord.ext import commands
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('TicketBot')


class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None,
            case_insensitive=True
        )
        self.db = Database()

    async def setup_hook(self):
        await self.db.init()
        logger.info('Database initialized.')

       for cog in ['cogs.tickets', 'cogs.admin', 'cogs.tier_test']:
            try:
                await self.load_extension(cog)
                logger.info(f'Loaded cog: {cog}')
            except Exception as e:
                logger.exception(f'Failed to load cog {cog}: {e}')

        try:
            synced = await self.tree.sync()
            logger.info(f'Synced {len(synced)} slash command(s) globally.')
        except Exception as e:
            logger.exception(f'Failed to sync commands: {e}')

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info(f'Serving {len(self.guilds)} guild(s).')
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name='🎫  Mine Front Support '
            ),
            status=discord.Status.online
        )

    async def on_guild_join(self, guild: discord.Guild):
        await self.db.setup_guild(guild.id)
        logger.info(f'Joined guild: {guild.name} ({guild.id})')

    async def on_application_command_error(self, interaction: discord.Interaction, error: Exception):
        if isinstance(error, discord.app_commands.MissingPermissions):
            await interaction.response.send_message(
                '❌ You do not have permission to use this command.', ephemeral=True
            )
        else:
            logger.exception(f'Unhandled command error: {error}')


def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        logger.critical('DISCORD_TOKEN environment variable is not set! Exiting.')
        sys.exit(1)

    bot = TicketBot()

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.critical('Invalid Discord token. Please check your DISCORD_TOKEN secret.')
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info('Shutting down...')


if __name__ == '__main__':
    main()
