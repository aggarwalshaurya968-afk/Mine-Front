from __future__ import annotations
import os
import re
import time
import logging

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import utils.embeds as E
from cogs.access import require_admin_or_owner

logger = logging.getLogger('TicketBot.ai_qa')

# ═══════════════════════════════════════════════════════════════════════════
#  AI Q&A  —  Auto-answers Minecraft related questions in chat using Claude
#
#  • If someone @mentions the bot, ANY question they ask gets answered.
#  • If someone just types a message in an enabled channel that looks like
#    a question AND is Minecraft related (keyword match), the bot answers
#    automatically — no mention needed.
#  • Per-guild toggle + optional channel lock, managed via /aiqa command.
#  • Needs ANTHROPIC_API_KEY set as an environment variable (Railway → 
#    Variables tab, or your local .env file). Nothing else to install —
#    aiohttp already ships with discord.py.
# ═══════════════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

COOLDOWN_SECONDS = 12          # per-user cooldown so one person can't spam it
MAX_REPLY_CHARS = 1900         # keep replies under discord's 2000 char limit

SYSTEM_PROMPT = (
    "You are the in-server AI assistant for a Minecraft community (tier-testing "
    "/ PvP / SMP server). Answer Minecraft questions clearly and briefly — "
    "gameplay mechanics, commands, redstone, plugins, PvP, server setup, "
    "crashes/lag troubleshooting, Java vs Bedrock differences, etc. "
    "Keep answers short (usually under 100 words) and use Discord markdown "
    "(backticks for commands, bullet points for steps). If a question is not "
    "about Minecraft at all, politely say you only help with Minecraft-related "
    "questions here. If you genuinely don't know something server-specific "
    "(like this server's exact rules), say so instead of guessing."
)

MC_KEYWORDS = [
    "minecraft", "mc ", " mc", "server", "java edition", "bedrock",
    "realm", "realms", "nether", "overworld", "the end", "ender dragon",
    "redstone", "mob", "mobs", "block", "blocks", "chunk", "biome",
    "crafting", "enchant", "enchanting", "potion", "brewing", "villager",
    "farm", "farming", "mod ", "mods", "modpack", "plugin", "plugins",
    "datapack", "resource pack", "texture pack", "shader", "shaders",
    "pvp", "bedwars", "bridge", "boxing", "uhc", "skywars", "mlg",
    "tier test", "tier testing", "hypixel", "seed", "sapling", "portal",
    "elytra", "netherite", "diamond", "obsidian", "creeper", "zombie",
    "skeleton", "enderman", "wither", "totem", "shulker", "beacon",
    "command block", "/tp", "/give", "/gamemode", "op ", "lag", "fps",
    "tps", "crash", "optifine", "fabric", "forge", "spigot", "paper",
    "bukkit", "essentialsx", "mcserver", "whitelist", "griefing",
]

QUESTION_HINTS = [
    "?", "kya", "kaise", "kaisy", "kese", "kyu", "kyun", "kaun", "kab",
    "kitna", "kitne", "how", "what", "why", "when", "where", "which",
    "can i", "can you", "is it", "does", "do i", "should i",
]


def _looks_like_question(text: str) -> bool:
    t = text.lower().strip()
    if not t:
        return False
    return any(hint in t for hint in QUESTION_HINTS)


def _is_minecraft_related(text: str) -> bool:
    t = f" {text.lower()} "
    return any(kw in t for kw in MC_KEYWORDS)


class AIQACog(commands.Cog, name="AI Q&A"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._cooldowns: dict[int, float] = {}

    async def cog_load(self):
        await self._ensure_table()
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── DB ────────────────────────────────────────────────────────────────
    async def _ensure_table(self):
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS aiqa_settings (
                    guild_id    INTEGER PRIMARY KEY,
                    enabled     INTEGER DEFAULT 0,
                    channel_id  INTEGER
                )
            ''')
            await db.commit()

    async def _get_settings(self, guild_id: int) -> dict:
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            async with db.execute(
                'SELECT enabled, channel_id FROM aiqa_settings WHERE guild_id = ?',
                (guild_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return {'enabled': False, 'channel_id': None}
        return {'enabled': bool(row[0]), 'channel_id': row[1]}

    async def _set_settings(self, guild_id: int, **kwargs):
        current = await self._get_settings(guild_id)
        current.update(kwargs)
        async with aiosqlite.connect(self.bot.db.db_path) as db:
            await db.execute('''
                INSERT INTO aiqa_settings (guild_id, enabled, channel_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    channel_id = excluded.channel_id
            ''', (guild_id, int(current['enabled']), current['channel_id']))
            await db.commit()

    # ── Claude call ──────────────────────────────────────────────────────
    async def _ask_claude(self, question: str, author_name: str) -> str | None:
        if not ANTHROPIC_API_KEY:
            logger.error("ANTHROPIC_API_KEY is not set — cannot answer AI Q&A questions.")
            return None
        if not self.session:
            return None

        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 400,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": f"({author_name} asks) {question}"}
            ],
        }
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        try:
            async with self.session.post(ANTHROPIC_URL, json=payload, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Anthropic API error {resp.status}: {body}")
                    return None
                data = await resp.json()
        except Exception:
            logger.exception("Anthropic API request failed")
            return None

        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        answer = "\n".join(p for p in parts if p).strip()
        return answer or None

    @staticmethod
    def _chunks(text: str, size: int):
        for i in range(0, len(text), size):
            yield text[i:i + size]

    # ── Listener ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        settings = await self._get_settings(message.guild.id)
        if not settings['enabled']:
            return

        mentioned = self.bot.user in message.mentions
        content = message.content.strip()

        if settings['channel_id'] and message.channel.id != settings['channel_id'] and not mentioned:
            return

        clean = re.sub(rf'<@!?{self.bot.user.id}>', '', content).strip() if mentioned else content
        if not clean:
            return

        if not mentioned:
            if not _looks_like_question(clean):
                return
            if not _is_minecraft_related(clean):
                return

        now = time.monotonic()
        last = self._cooldowns.get(message.author.id, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        self._cooldowns[message.author.id] = now

        async with message.channel.typing():
            answer = await self._ask_claude(clean, message.author.display_name)

        if not answer:
            if mentioned:
                await message.reply(
                    embed=E.error("I couldn't reach the AI service right now. Try again in a bit."),
                    mention_author=False
                )
            return

        first = True
        for chunk in self._chunks(answer, MAX_REPLY_CHARS):
            if first:
                await message.reply(chunk, mention_author=False)
                first = False
            else:
                await message.channel.send(chunk)

    # ── /aiqa ────────────────────────────────────────────────────────────
    aiqa = app_commands.Group(name="aiqa", description="Manage the AI auto-answer system.")

    @aiqa.command(name="enable", description="Enable AI auto-answering in this server.")
    async def aiqa_enable(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, enabled=True)
        await interaction.response.send_message(
            embed=E.success("✅ AI Q&A enabled. I'll now answer Minecraft-related questions."),
            ephemeral=True
        )

    @aiqa.command(name="disable", description="Disable AI auto-answering in this server.")
    async def aiqa_disable(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, enabled=False)
        await interaction.response.send_message(
            embed=E.success("🚫 AI Q&A disabled."),
            ephemeral=True
        )

    @aiqa.command(name="setchannel", description="Restrict auto-answering to one channel (leave empty = all channels).")
    @app_commands.describe(channel="Channel to restrict to, or omit to allow all channels")
    async def aiqa_setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        await self._set_settings(interaction.guild_id, channel_id=channel.id if channel else None)
        msg = f"Channel restricted to {channel.mention}." if channel else "Channel restriction removed — works everywhere now."
        await interaction.response.send_message(embed=E.success(msg), ephemeral=True)

    @aiqa.command(name="status", description="Show current AI Q&A settings.")
    async def aiqa_status(self, interaction: discord.Interaction):
        if not await require_admin_or_owner(self.bot, interaction):
            return
        settings = await self._get_settings(interaction.guild_id)
        chan = f"<#{settings['channel_id']}>" if settings['channel_id'] else "All channels"
        key_status = "✅ Set" if ANTHROPIC_API_KEY else "❌ Missing (set ANTHROPIC_API_KEY!)"
        await interaction.response.send_message(
            embed=E.base(
                "🤖 AI Q&A Status",
                f"**Enabled:** {'✅ Yes' if settings['enabled'] else '❌ No'}\n"
                f"**Channel:** {chan}\n"
                f"**API Key:** {key_status}\n"
                f"**Cooldown:** {COOLDOWN_SECONDS}s per user"
            ),
            ephemeral=True
        )

    @aiqa.command(name="ask", description="Directly ask the AI a Minecraft question.")
    @app_commands.describe(question="Your question")
    async def aiqa_ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer(thinking=True)
        answer = await self._ask_claude(question, interaction.user.display_name)
        if not answer:
            await interaction.followup.send(
                embed=E.error("Couldn't reach the AI service right now. Try again in a bit.")
            )
            return
        chunks = list(self._chunks(answer, MAX_REPLY_CHARS))
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIQACog(bot))
