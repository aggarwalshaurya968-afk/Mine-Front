import asyncio
import functools
import itertools
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp as youtube_dl

FFMPEG_PATH = "ffmpeg"

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamdelay_max 5",
    "options": "-vn",
}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

youtube_dl.utils.bug_reports_message = lambda: ""
ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title", "Unknown")
        self.url = data.get("webpage_url", "")
        self.duration = data.get("duration", 0)
        self.thumbnail = data.get("thumbnail")
        self.requester = data.get("requester")

    @classmethod
    async def create_source(cls, search: str, *, loop: asyncio.AbstractEventLoop, requester=None):
        loop = loop or asyncio.get_event_loop()
        partial = functools.partial(ytdl.extract_info, search, download=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise commands.CommandError("Kuch nahi mila is query ke liye ❌")

        if "entries" in data:
            data = data["entries"][0]

        data["requester"] = requester
        filename = data["url"]
        return cls(
            discord.FFmpegPCMAudio(filename, executable=FFMPEG_PATH, **FFMPEG_OPTIONS),
            data=data,
        )

    @staticmethod
    def format_duration(seconds):
        if not seconds:
            return "Live/Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


class GuildMusicState:
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = asyncio.Queue()
        self.next_event = asyncio.Event()
        self.current: YTDLSource | None = None
        self.volume = 0.5
        self.loop_song = False
        self.text_channel: discord.abc.Messageable | None = None
        self.player_task = bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()
        while True:
            self.next_event.clear()

            if not self.loop_song or self.current is None:
                try:
                    async with asyncio.timeout(300):
                        source = await self.queue.get()
                except (asyncio.TimeoutError, TimeoutError):
                    vc = self.get_voice_client()
                    if vc:
                        await vc.disconnect()
                    return
                self.current = source

            vc = self.get_voice_client()
            if vc is None:
                return

            self.current.volume = self.volume
            vc.play(
                self.current,
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next_event.set),
            )

            if self.text_channel:
                embed = discord.Embed(
                    title="🎶 Ab baj raha hai",
                    description=f"[{self.current.title}]({self.current.url})",
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Duration", value=YTDLSource.format_duration(self.current.duration))
                if self.current.requester:
                    embed.set_footer(text=f"Requested by {self.current.requester}")
                if self.current.thumbnail:
                    embed.set_thumbnail(url=self.current.thumbnail)
                await self.text_channel.send(embed=embed)

            await self.next_event.wait()

    def get_voice_client(self):
        guild = self.bot.get_guild(self.guild_id)
        return guild.voice_client if guild else None

    def destroy(self):
        self.player_task.cancel()


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.states[guild_id]

    def cog_unload(self):
        for state in self.states.values():
            state.destroy()

    async def ensure_voice(self, ctx: commands.Context) -> bool:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ Pehle kisi voice channel me join karo!")
            return False

        if ctx.voice_client is None:
            await ctx.author.voice.channel.connect()
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.send("❌ Main already ek dusre voice channel me hu!")
            return False
        return True

    @commands.hybrid_command(name="join", description="Bot ko apne voice channel me bulao")
    async def join(self, ctx: commands.Context):
        if await self.ensure_voice(ctx):
            await ctx.send(f"✅ Joined **{ctx.author.voice.channel.name}**")

    @commands.hybrid_command(name="leave", description="Bot ko voice channel se nikalo")
    async def leave(self, ctx: commands.Context):
        if ctx.voice_client is None:
            return await ctx.send("❌ Main kisi voice channel me hu hi nahi!")
        state = self.states.pop(ctx.guild.id, None)
        if state:
            state.destroy()
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Voice channel se nikal gaya!")

    @commands.hybrid_command(name="play", description="Gaana bajao (naam ya YouTube link)")
    @app_commands.describe(search="Song ka naam ya YouTube URL")
    async def play(self, ctx: commands.Context, *, search: str):
        if not await self.ensure_voice(ctx):
            return

        await ctx.defer() if ctx.interaction else None
        state = self.get_state(ctx.guild.id)
        state.text_channel = ctx.channel

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(
                    search, loop=self.bot.loop, requester=ctx.author.display_name
                )
            except Exception as e:
                return await ctx.send(f"❌ Error aaya gaana dhoondhte waqt: `{e}`")

            await state.queue.put(source)

            if ctx.voice_client and ctx.voice_client.is_playing():
                await ctx.send(f"➕ Queue me add ho gaya: **{source.title}**")
            else:
                await ctx.send(f"🔎 Loading: **{source.title}**")

    @commands.hybrid_command(name="pause", description="Gaana pause karo")
    async def pause(self, ctx: commands.Context):
        vc = ctx.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("⏸️ Pause kar diya!")
        else:
            await ctx.send("❌ Abhi kuch bhi baj nahi raha.")

    @commands.hybrid_command(name="resume", description="Gaana resume karo")
    async def resume(self, ctx: commands.Context):
        vc = ctx.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("▶️ Resume kar diya!")
        else:
            await ctx.send("❌ Gaana paused nahi hai.")

    @commands.hybrid_command(name="skip", description="Current gaana skip karo")
    async def skip(self, ctx: commands.Context):
        vc = ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.send("⏭️ Skip kar diya!")
        else:
            await ctx.send("❌ Skip karne ke liye kuch bhi nahi baj raha.")

    @commands.hybrid_command(name="stop", description="Music rok kar queue clear karo")
    async def stop(self, ctx: commands.Context):
        state = self.states.get(ctx.guild.id)
        vc = ctx.voice_client
        if state:
            while not state.queue.empty():
                state.queue.get_nowait()
        if vc:
            vc.stop()
        await ctx.send("⏹️ Music stop aur queue clear kar diya!")

    @commands.hybrid_command(name="queue", description="Queue dekho")
    async def queue_cmd(self, ctx: commands.Context):
        state = self.states.get(ctx.guild.id)
        if not state or state.queue.empty():
            return await ctx.send("📭 Queue khaali hai.")

        upcoming = list(itertools.islice(state.queue._queue, 10))
        desc = "\n".join(f"**{i+1}.** {song.title}" for i, song in enumerate(upcoming))
        embed = discord.Embed(title="🎵 Queue", description=desc, color=discord.Color.green())
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nowplaying", description="Abhi kya baj raha hai dekho")
    async def nowplaying(self, ctx: commands.Context):
        state = self.states.get(ctx.guild.id)
        if not state or not state.current:
            return await ctx.send("❌ Abhi kuch bhi baj nahi raha.")

        song = state.current
        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"[{song.title}]({song.url})",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Duration", value=YTDLSource.format_duration(song.duration))
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="volume", description="Volume set karo (0-100)")
    @app_commands.describe(level="0 se 100 ke beech")
    async def volume(self, ctx: commands.Context, level: int):
        if not 0 <= level <= 100:
            return await ctx.send("❌ Volume 0 se 100 ke beech hona chahiye.")

        state = self.get_state(ctx.guild.id)
        state.volume = level / 100
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = state.volume
        await ctx.send(f"🔊 Volume {level}% set kar diya!")

    @commands.hybrid_command(name="loop", description="Current gaana loop on/off karo")
    async def loop(self, ctx: commands.Context):
        state = self.get_state(ctx.guild.id)
        state.loop_song = not state.loop_song
        status = "ON 🔁" if state.loop_song else "OFF"
        await ctx.send(f"Loop mode: **{status}**")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            return
        vc = member.guild.voice_client
        if vc and len(vc.channel.members) == 1:
            state = self.states.pop(member.guild.id, None)
            if state:
                state.destroy()
            await vc.disconnect()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
  
