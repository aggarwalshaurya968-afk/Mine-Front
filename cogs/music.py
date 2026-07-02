from __future__ import annotations
import asyncio
import logging
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import imageio_ffmpeg

from config import PURPLE, GREEN, RED, ORANGE, BLUE
import utils.embeds as E

logger = logging.getLogger('TicketBot.music')

# Bundled ffmpeg binary (proven to work — no dependency on system PATH)
import shutil

FFMPEG_EXECUTABLE = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -loglevel error"
}

YTDL_OPTIONS = {
    "format": "bestaudio/best/bestaudio[ext=m4a]",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


# ═══════════════════════════════════════════════════════════════════════════════
#  TRACK / QUEUE STATE
# ═══════════════════════════════════════════════════════════════════════════════

class Track:
    def __init__(self, data: dict, requester: discord.Member):
        self.title = data.get('title', 'Unknown')
        self.url = data.get('url')
        self.webpage_url = data.get('webpage_url', '')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail')
        self.requester = requester

    @property
    def duration_str(self) -> str:
        if not self.duration:
            return 'Live'
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


class GuildMusicState:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Track | None = None
        self.volume: float = 0.5
        self.loop: bool = False
        self.text_channel: discord.abc.Messageable | None = None

     def play_next(self):
        if self.loop and self.current:
            self.queue.appendleft(self.current)

        if not self.queue:
            self.current = None
            return

        self.current = self.queue.popleft()

        logger.info(f"Using FFmpeg: {FFMPEG_EXECUTABLE}")
        logger.info(f"Playing URL: {self.current.url}")

        except Exception:
            logger.error('Failed to create audio source', exc_info=True)
            if self.text_channel:
                asyncio.run_coroutine_threadsafe(
                    self.text_channel.send(embed=E.error('Failed to play track (ffmpeg error). Skipping.')),
                    self.bot.loop
                )
            self.bot.loop.call_soon_threadsafe(self.play_next)
            return

        def _after(err):
            if err:
                logger.error(f'Playback error: {err}')
                if self.text_channel:
                    asyncio.run_coroutine_threadsafe(
                        self.text_channel.send(embed=E.error(f'Playback stopped due to an error: `{err}`')),
                        self.bot.loop
                    )
            self.bot.loop.call_soon_threadsafe(self.play_next)

        self.voice_client.play(source, after=_after)
        if self.text_channel:
            asyncio.run_coroutine_threadsafe(
                self.text_channel.send(embed=_now_playing_embed(self.current)),
                self.bot.loop
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  EMBED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _now_playing_embed(track: Track) -> discord.Embed:
    e = discord.Embed(
        title='🎶  Now Playing',
        description=f'**[{track.title}]({track.webpage_url})**',
        color=PURPLE
    )
    e.add_field(name='⏱️  Duration', value=track.duration_str, inline=True)
    e.add_field(name='🙋  Requested By', value=track.requester.mention, inline=True)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    e.set_footer(text='Mine Front Music')
    return e


def _queued_embed(track: Track, position: int) -> discord.Embed:
    e = discord.Embed(
        title='➕  Added to Queue',
        description=f'**[{track.title}]({track.webpage_url})**',
        color=GREEN
    )
    e.add_field(name='⏱️  Duration', value=track.duration_str, inline=True)
    e.add_field(name='📍  Position', value=str(position), inline=True)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


def _queue_list_embed(state: GuildMusicState) -> discord.Embed:
    e = discord.Embed(title='📜  Music Queue', color=BLUE)
    if state.current:
        e.add_field(
            name='🎶  Now Playing',
            value=f'[{state.current.title}]({state.current.webpage_url}) • `{state.current.duration_str}`',
            inline=False
        )
    if not state.queue:
        e.add_field(name='Up Next', value='Queue is empty.', inline=False)
    else:
        lines = [
            f'`{i+1}.` [{t.title}]({t.webpage_url}) • `{t.duration_str}` — {t.requester.mention}'
            for i, t in enumerate(list(state.queue)[:10])
        ]
        e.add_field(name='Up Next', value='\n'.join(lines), inline=False)
        if len(state.queue) > 10:
            e.set_footer(text=f'+{len(state.queue) - 10} more track(s) in queue')
    return e


# ═══════════════════════════════════════════════════════════════════════════════
#  YTDL EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

async def extract(query: str, loop: asyncio.AbstractEventLoop) -> dict:
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(query, download=False)
    )

    if "entries" in data:
        data = data["entries"][0]

    if "url" not in data:
        data = ytdl.extract_info(data["webpage_url"], download=False)

    return data


# ═══════════════════════════════════════════════════════════════════════════════
#  COG
# ═══════════════════════════════════════════════════════════════════════════════

class MusicCog(commands.Cog, name='Music'):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.states[guild_id]

    async def _ensure_voice(self, interaction: discord.Interaction, state: GuildMusicState) -> bool:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send(
                embed=E.error('You must be in a voice channel to use this command.'), ephemeral=True)
            return False

        channel = interaction.user.voice.channel
        if state.voice_client is None or not state.voice_client.is_connected():
            state.voice_client = await channel.connect()
        elif state.voice_client.channel != channel:
            await state.voice_client.move_to(channel)
        return True

    # ─────────────────────── /join ───────────────────────────────────────
    @app_commands.command(name='join', description='Join your current voice channel.')
    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        if not await self._ensure_voice(interaction, state):
            return

        await interaction.followup.send(
            embed=E.success(f'✅  Joined {state.voice_client.channel.mention}.'), ephemeral=True)

    # ─────────────────────── /play ───────────────────────────────────────
    @app_commands.command(name='play', description='Play a song or add it to the queue.')
    @app_commands.describe(query='Song name or URL')
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        state = self.get_state(interaction.guild_id)
        state.text_channel = interaction.channel

        if not await self._ensure_voice(interaction, state):
            return

        try:
            data = await extract(query, self.bot.loop)
        except Exception:
            logger.error('yt-dlp extraction failed', exc_info=True)
            return await interaction.followup.send(embed=E.error('Could not find or load that track.'))

        track = Track(data, interaction.user)
        state.queue.append(track)

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            await interaction.followup.send(embed=_queued_embed(track, len(state.queue)))
        else:
            await interaction.followup.send(embed=E.success('Starting playback...'))
            state.play_next()

    # ─────────────────────── /pause ──────────────────────────────────────
    @app_commands.command(name='pause', description='Pause the current track.')
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_playing():
            return await interaction.response.send_message(embed=E.error('Nothing is playing.'), ephemeral=True)
        state.voice_client.pause()
        await interaction.response.send_message(embed=E.success('⏸️  Paused.'))

    # ─────────────────────── /resume ─────────────────────────────────────
    @app_commands.command(name='resume', description='Resume the paused track.')
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not state.voice_client.is_paused():
            return await interaction.response.send_message(embed=E.error('Nothing is paused.'), ephemeral=True)
        state.voice_client.resume()
        await interaction.response.send_message(embed=E.success('▶️  Resumed.'))

    # ─────────────────────── /skip ───────────────────────────────────────
    @app_commands.command(name='skip', description='Skip the current track.')
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            return await interaction.response.send_message(embed=E.error('Nothing is playing.'), ephemeral=True)
        state.voice_client.stop()
        await interaction.response.send_message(embed=E.success('⏭️  Skipped.'))

    # ─────────────────────── /stop ───────────────────────────────────────
    @app_commands.command(name='stop', description='Stop playback, clear the queue, and disconnect.')
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        state.current = None
        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None
        await interaction.response.send_message(embed=E.success('⏹️  Stopped and left the voice channel.'))

    # ─────────────────────── /queue ──────────────────────────────────────
    @app_commands.command(name='queue', description='Show the current music queue.')
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        await interaction.response.send_message(embed=_queue_list_embed(state))

    # ─────────────────────── /nowplaying ─────────────────────────────────
    @app_commands.command(name='nowplaying', description='Show the currently playing track.')
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            return await interaction.response.send_message(embed=E.error('Nothing is playing.'), ephemeral=True)
        await interaction.response.send_message(embed=_now_playing_embed(state.current))

    # ─────────────────────── /volume ─────────────────────────────────────
    @app_commands.command(name='volume', description='(Currently disabled) Playback volume control.')
    @app_commands.describe(level='Volume percentage')
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        await interaction.response.send_message(
            embed=E.error('Volume control is temporarily disabled for playback stability.'), ephemeral=True)

    # ─────────────────────── /loop ───────────────────────────────────────
    @app_commands.command(name='loop', description='Toggle looping the current track.')
    async def loop_cmd(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.loop = not state.loop
        status = 'enabled 🔁' if state.loop else 'disabled'
        await interaction.response.send_message(embed=E.success(f'Loop {status}.'))

    # ─────────────────────── /leave ──────────────────────────────────────
    @app_commands.command(name='leave', description='Disconnect the bot from the voice channel.')
    async def leave(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.voice_client:
            return await interaction.response.send_message(embed=E.error('I am not in a voice channel.'), ephemeral=True)
        state.queue.clear()
        state.current = None
        await state.voice_client.disconnect()
        state.voice_client = None
        await interaction.response.send_message(embed=E.success('👋  Disconnected.'))


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
