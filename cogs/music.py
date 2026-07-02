from __future__ import annotations

import asyncio
import logging
import shutil
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import imageio_ffmpeg

from config import PURPLE, GREEN, BLUE
import utils.embeds as E

logger = logging.getLogger("TicketBot.music")

FFMPEG_EXECUTABLE = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()

FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "extract_flat": False,
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Track:
    def __init__(self, data: dict, requester: discord.Member):
        self.title = data.get("title", "Unknown")
        self.webpage_url = data.get("webpage_url") or data.get("original_url") or ""
        self.duration = data.get("duration", 0)
        self.thumbnail = data.get("thumbnail")
        self.requester = requester

    @property
    def duration_str(self):
        if not self.duration:
            return "Live"

        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)

        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"

        return f"{m:02d}:{s:02d}"


class GuildMusicState:
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.voice_client: discord.Voice
      
def _now_playing_embed(track: Track) -> discord.Embed:
    e = discord.Embed(
        title="🎶 Now Playing",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=PURPLE
    )

    e.add_field(
        name="⏱️ Duration",
        value=track.duration_str,
        inline=True
    )

    e.add_field(
        name="🙋 Requested By",
        value=track.requester.mention,
        inline=True
    )

    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)

    return e


def _queued_embed(track: Track, position: int) -> discord.Embed:
    e = discord.Embed(
        title="➕ Added to Queue",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=GREEN
    )

    e.add_field(
        name="⏱️ Duration",
        value=track.duration_str,
        inline=True
    )

    e.add_field(
        name="📍 Position",
        value=str(position),
        inline=True
    )

    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)

    return e


def _queue_list_embed(state: GuildMusicState) -> discord.Embed:
    e = discord.Embed(
        title="📜 Music Queue",
        color=BLUE
    )

    if state.current:
        e.add_field(
            name="🎶 Now Playing",
            value=f"[{state.current.title}]({state.current.webpage_url}) • `{state.current.duration_str}`",
            inline=False
        )

    if state.queue:
        text = "\n".join(
            f"`{i+1}.` {song.title}"
            for i, song in enumerate(state.queue)
        )

        e.add_field(
            name="Up Next",
            value=text,
            inline=False
        )
    else:
        e.add_field(
            name="Up Next",
            value="Queue is empty.",
            inline=False
        )

    return e


async def extract(query: str, loop):
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(query, download=False)
    )

    if "entries" in data:
        data = data["entries"][0]

    return data
  class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states = {}

    def get_state(self, guild_id: int):
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.states[guild_id]

    async def ensure_voice(self, interaction: discord.Interaction, state: GuildMusicState):
        if interaction.user.voice is None:
            await interaction.followup.send(
                embed=E.error("Join a voice channel first."),
                ephemeral=True
            )
            return False

        channel = interaction.user.voice.channel

        if state.voice_client is None:
            state.voice_client = await channel.connect()

        elif state.voice_client.channel != channel:
            await state.voice_client.move_to(channel)

        state.text_channel = interaction.channel
        return True

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):

        await interaction.response.defer(ephemeral=True)

        state = self.get_state(interaction.guild.id)

        if not await self.ensure_voice(interaction, state):
            return

        await interaction.followup.send(
            embed=E.success("Joined voice channel.")
        )

    @app_commands.command(name="play", description="Play music")
    async def play(self, interaction: discord.Interaction, query: str):

        await interaction.response.defer()

        state = self.get_state(interaction.guild.id)

        if not await self.ensure_voice(interaction, state):
            return

        try:
            data = await extract(query, self.bot.loop)
        except Exception:
            return await interaction.followup.send(
                embed=E.error("Couldn't find that song.")
            )

        track = Track(data, interaction.user)

        state.queue.append(track)

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            await interaction.followup.send(
                embed=_queued_embed(track, len(state.queue))
            )
            return

        await interaction.followup.send(
            embed=E.success("Starting playback...")
        )

        state.play_next()

    @app_commands.command(name="pause", description="Pause music")
    async def pause(self, interaction: discord.Interaction):

        state =
          @app_commands.command(name="skip", description="Skip current song")
    async def skip(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        if not state.voice_client or not (
            state.voice_client.is_playing()
            or state.voice_client.is_paused()
        ):
            return await interaction.response.send_message(
                embed=E.error("Nothing is playing."),
                ephemeral=True
            )

        state.voice_client.stop()

        await interaction.response.send_message(
            embed=E.success("Skipped.")
        )

    @app_commands.command(name="stop", description="Stop music")
    async def stop(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        state.queue.clear()
        state.current = None

        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None

        await interaction.response.send_message(
            embed=E.success("Stopped playback.")
        )

    @app_commands.command(name="queue", description="Show queue")
    async def queue(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        await interaction.response.send_message(
            embed=_queue_list_embed(state)
        )

    @app_commands.command(name="nowplaying", description="Current song")
    async def nowplaying(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        if not state.current:
            return await interaction.response.send_message(
                embed=E.error("Nothing is playing."),
                ephemeral=True
            )

        await interaction.response.send_message(
            embed=_now_playing_embed(state.current)
        )

    @app_commands.command(name="loop", description="Toggle loop")
    async def loop(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        state.loop = not state.loop

        await interaction.response.send_message(
            embed=E.success(
                f"Loop {'enabled' if state.loop else 'disabled'}."
            )
        )

    @app_commands.command(name="leave", description="Leave voice channel")
    async def leave(self, interaction: discord.Interaction):

        state = self.get_state(interaction.guild.id)

        state.queue.clear()
        state.current = None

        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None

        await interaction.response.send_message(
            embed=E.success("Disconnected.")
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
      
