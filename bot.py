"""Grok Discord bot with live search.

Talks to xAI's Responses API (POST /v1/responses).

Search modes:
  web   Grok calls our own `search_web` function, which the bot answers with
        Tavily (free tier, short snippets keep Grok's prompt small). Default.
  deep  xAI's server-side web_search + x_search (thorough, ~3-6x the cost).
  x     xAI's server-side x_search only (billed per post fetched).
  none  no search, cheapest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
XAI_API_KEY = os.environ["XAI_API_KEY"]
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
MODEL = os.getenv("GROK_MODEL", "grok-4.3")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are Grok, chatting in a Discord server. Be concise and use Discord markdown. "
    "When a question needs current or factual information you are unsure of, search first, "
    "and cite the sources you used.",
)
DEFAULT_SEARCH = os.getenv("DEFAULT_SEARCH", "web")
# Spending guards (USD). 0 disables a limit.
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "2.00"))
PER_USER_DAILY_USD = float(os.getenv("PER_USER_DAILY_USD", "0.50"))
USER_COOLDOWN_S = float(os.getenv("USER_COOLDOWN_S", "5"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1500"))
SEARCH_RESULTS = int(os.getenv("SEARCH_RESULTS", "5"))
MAX_TOOL_ROUNDS = 3
SHOW_COST = os.getenv("SHOW_COST", "true").lower() == "true"
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
HEARTBEAT_FILE = Path(os.getenv("HEARTBEAT_FILE", "/tmp/grokbot-heartbeat"))
# Optional: comma-separated guild IDs for instant slash-command sync while testing.
DEV_GUILD_IDS = [int(g) for g in os.getenv("DEV_GUILD_IDS", "").split(",") if g.strip()]

XAI_URL = "https://api.x.ai/v1/responses"
TAVILY_URL = "https://api.tavily.com/search"
DISCORD_LIMIT = 2000
THREAD_TTL_S = 30 * 86400  # xAI keeps stored responses for 30 days

log = logging.getLogger("grokbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SEARCH_FUNCTION = {
    "type": "function",
    "name": "search_web",
    "description": "Search the web for current information. Returns titles, URLs and short excerpts.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query"}},
        "required": ["query"],
    },
}


# --------------------------------------------------------------------------- state


class Store:
    """Spend per user per day, and which Discord message belongs to which Grok thread.

    SQLite in DATA_DIR so both survive restarts.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS spend (
                day TEXT NOT NULL, user_id INTEGER NOT NULL, cost REAL NOT NULL,
                PRIMARY KEY (day, user_id));
            CREATE TABLE IF NOT EXISTS threads (
                message_id INTEGER PRIMARY KEY, response_id TEXT NOT NULL, created REAL NOT NULL);
            """
        )
        self.db.execute("DELETE FROM threads WHERE created < ?", (time.time() - THREAD_TTL_S,))
        self.db.commit()
        self.last_call: dict[int, float] = {}

    def spent_today(self, user_id: int | None = None) -> float:
        day = date.today().isoformat()
        if user_id is None:
            row = self.db.execute("SELECT SUM(cost) FROM spend WHERE day = ?", (day,)).fetchone()
        else:
            row = self.db.execute(
                "SELECT cost FROM spend WHERE day = ? AND user_id = ?", (day, user_id)
            ).fetchone()
        return (row[0] or 0.0) if row else 0.0

    def check(self, user_id: int) -> str | None:
        if DAILY_BUDGET_USD and self.spent_today() >= DAILY_BUDGET_USD:
            return "The bot hit its daily spending cap. Try again tomorrow."
        if PER_USER_DAILY_USD and self.spent_today(user_id) >= PER_USER_DAILY_USD:
            return "You've hit your daily limit for this bot. Try again tomorrow."
        wait = USER_COOLDOWN_S - (time.monotonic() - self.last_call.get(user_id, -1e9))
        if wait > 0:
            return f"Slow down, try again in {wait:.0f}s."
        self.last_call[user_id] = time.monotonic()
        return None

    def record(self, user_id: int, cost: float) -> None:
        self.db.execute(
            "INSERT INTO spend (day, user_id, cost) VALUES (?, ?, ?) "
            "ON CONFLICT (day, user_id) DO UPDATE SET cost = cost + excluded.cost",
            (date.today().isoformat(), user_id, cost),
        )
        self.db.commit()

    def remember(self, message_ids: list[int], response_id: str | None) -> None:
        if not response_id:
            return
        now = time.time()
        self.db.executemany(
            "INSERT OR REPLACE INTO threads VALUES (?, ?, ?)",
            [(m, response_id, now) for m in message_ids],
        )
        self.db.commit()

    def thread_for(self, message_id: int) -> str | None:
        row = self.db.execute(
            "SELECT response_id FROM threads WHERE message_id = ? AND created >= ?",
            (message_id, time.time() - THREAD_TTL_S),
        ).fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------- grok


@dataclass
class GrokReply:
    text: str = ""
    response_id: str | None = None
    citations: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    searches: dict[str, int] = field(default_factory=dict)

    def count(self, kind: str, n: int = 1) -> None:
        if n:
            self.searches[kind] = self.searches.get(kind, 0) + n


def server_tools(mode: str) -> list[dict]:
    if mode == "deep":
        return [{"type": "web_search"}, {"type": "x_search"}]
    if mode == "x":
        return [{"type": "x_search"}]
    return []


def absorb(reply: GrokReply, data: dict) -> list[dict]:
    """Fold one /v1/responses result into reply; return any function calls to run."""
    calls, texts = [], []
    for item in data.get("output", []):
        kind = item.get("type")
        if kind == "function_call":
            calls.append(item)
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
                    for ann in part.get("annotations") or []:
                        if ann.get("url"):
                            reply.citations.append(ann["url"])
    for c in data.get("citations") or []:
        url = c if isinstance(c, str) else c.get("url")
        if url:
            reply.citations.append(url)
    if texts:
        reply.text = "".join(texts).strip()
    reply.response_id = data.get("id") or reply.response_id

    usage = data.get("usage") or {}
    if usage.get("cost_in_nano_usd") is not None:
        reply.cost_usd += usage["cost_in_nano_usd"] / 1e9
    tools = usage.get("server_side_tool_usage_details") or {}
    reply.count("xAI web", tools.get("web_search_calls") or 0)
    reply.count("X", tools.get("x_search_calls") or 0)
    return calls


class Grok:
    def __init__(self) -> None:
        self.xai = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            timeout=httpx.Timeout(180.0),  # server-side searches can take a while
        )
        self.tavily = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        await self.xai.aclose()
        await self.tavily.aclose()

    async def _post(self, body: dict) -> dict:
        r = await self.xai.post(XAI_URL, json=body)
        if r.status_code >= 400:
            log.error("xAI error %s: %s", r.status_code, r.text[:500])
            raise RuntimeError(f"xAI API error {r.status_code}")
        return r.json()

    async def search(self, query: str) -> tuple[str, list[str]]:
        r = await self.tavily.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "max_results": SEARCH_RESULTS, "search_depth": "basic"},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        trimmed = [
            {"title": x.get("title"), "url": x.get("url"), "excerpt": (x.get("content") or "")[:700]}
            for x in results
        ]
        return json.dumps(trimmed, ensure_ascii=False), [x["url"] for x in trimmed if x.get("url")]

    async def ask(self, prompt: str, mode: str, previous_id: str | None, user_id: int) -> GrokReply:
        if mode == "web" and not TAVILY_API_KEY:
            mode = "deep"  # no Tavily key: fall back to xAI's own search
        tools = [SEARCH_FUNCTION] if mode == "web" else server_tools(mode)

        body: dict = {
            "model": MODEL,
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": True,  # follow-up replies continue the thread via previous_response_id
            "user": str(user_id),
        }
        if previous_id:
            body["previous_response_id"] = previous_id  # carries the system prompt too
        else:
            body["instructions"] = SYSTEM_PROMPT
        if tools:
            body["tools"] = tools

        reply = GrokReply()
        tavily_urls: list[str] = []
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            calls = absorb(reply, await self._post(body))
            if not calls:
                break
            outputs = []
            for call in calls:
                try:
                    query = json.loads(call.get("arguments") or "{}").get("query", "")
                    text, urls = await self.search(query)
                    tavily_urls += urls
                    reply.count("web")
                except Exception as e:  # tell Grok, let it answer anyway
                    log.warning("search failed: %s", e)
                    text = json.dumps({"error": "search failed"})
                outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": text})
            body = {
                "model": MODEL,
                "previous_response_id": reply.response_id,
                "input": outputs,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "store": True,
                "user": str(user_id),
                "tools": tools,
            }
            if round_no == MAX_TOOL_ROUNDS - 1:
                body["tool_choice"] = "none"  # last round: answer with what you have

        # Prefer the URLs Grok cited; fall back to what our searches returned.
        reply.citations = list(dict.fromkeys(reply.citations or tavily_urls))
        reply.text = reply.text or "_(no answer)_"
        return reply


# --------------------------------------------------------------------------- discord


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def footer(reply: GrokReply) -> str:
    parts = []
    if reply.citations:
        parts.append("**Sources:** " + " ".join(f"<{u}>" for u in reply.citations[:5]))
    meta = []
    if reply.searches:
        meta.append("searched: " + ", ".join(f"{n} {k}" for k, n in reply.searches.items()))
    if SHOW_COST and reply.cost_usd:
        meta.append(f"${reply.cost_usd:.4f}")
    if meta:
        parts.append(f"-# {MODEL} · " + " · ".join(meta))
    return "\n".join(parts)


class GrokBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents.default(),  # no privileged intents needed
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)
        self.store = Store(DATA_DIR / "state" / "grokbot.db")
        self.grok: Grok | None = None

    async def setup_hook(self) -> None:
        self.grok = Grok()
        self.loop.create_task(self.heartbeat())
        for gid in DEV_GUILD_IDS:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()

    async def heartbeat(self) -> None:
        """Touch a file while connected; the Docker healthcheck reads its age."""
        while not self.is_closed():
            # latency is inf until the gateway has acked a heartbeat
            if self.is_ready() and math.isfinite(self.latency):
                HEARTBEAT_FILE.touch()
            await asyncio.sleep(30)

    async def close(self) -> None:
        if self.grok:
            await self.grok.aclose()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (model %s, default search %s)", self.user, MODEL, DEFAULT_SEARCH)

    async def answer(self, user_id: int, prompt: str, mode: str, previous_id: str | None) -> GrokReply:
        reply = await self.grok.ask(prompt, mode, previous_id, user_id)
        self.store.record(user_id, reply.cost_usd)
        return reply

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not self.user:
            return
        ref = message.reference
        previous_id = self.store.thread_for(ref.message_id) if ref and ref.message_id else None
        mentioned = self.user in message.mentions
        is_dm = isinstance(message.channel, discord.DMChannel)
        if not (mentioned or previous_id or is_dm):
            return

        prompt = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not prompt:
            return
        if err := self.store.check(message.author.id):
            await message.reply(err, mention_author=False)
            return

        async with message.channel.typing():
            try:
                reply = await self.answer(message.author.id, prompt, DEFAULT_SEARCH, previous_id)
            except Exception:
                log.exception("ask failed")
                await message.reply("Grok request failed, try again in a bit.", mention_author=False)
                return

        sent_ids, target = [], message
        for chunk in split_message(reply.text + "\n\n" + footer(reply)):
            target = await target.reply(chunk, mention_author=False, suppress_embeds=True)
            sent_ids.append(target.id)
        self.store.remember(sent_ids, reply.response_id)


bot = GrokBot()


@bot.tree.command(name="ask", description="Ask Grok (searches the web when useful)")
@app_commands.describe(question="What do you want to know?", search="How Grok may search")
@app_commands.choices(
    search=[
        app_commands.Choice(name="Web (default, cheap)", value="web"),
        app_commands.Choice(name="Deep: xAI web + X search (pricier)", value="deep"),
        app_commands.Choice(name="X / Twitter only (pricier)", value="x"),
        app_commands.Choice(name="No search (cheapest)", value="none"),
    ]
)
async def ask_cmd(
    interaction: discord.Interaction, question: str, search: app_commands.Choice[str] | None = None
) -> None:
    if err := bot.store.check(interaction.user.id):
        await interaction.response.send_message(err, ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        reply = await bot.answer(interaction.user.id, question, search.value if search else DEFAULT_SEARCH, None)
    except Exception:
        log.exception("ask failed")
        await interaction.followup.send("Grok request failed, try again in a bit.")
        return

    header = f"> **{interaction.user.display_name}:** {question[:300]}\n\n"
    sent_ids = []
    for chunk in split_message(header + reply.text + "\n\n" + footer(reply)):
        msg = await interaction.followup.send(chunk, suppress_embeds=True, wait=True)
        sent_ids.append(msg.id)
    bot.store.remember(sent_ids, reply.response_id)


@bot.tree.command(name="grokusage", description="Show today's Grok spend")
async def usage_cmd(interaction: discord.Interaction) -> None:
    cap = f" of ${DAILY_BUDGET_USD:.2f}" if DAILY_BUDGET_USD else ""
    await interaction.response.send_message(
        f"Today: ${bot.store.spent_today():.4f}{cap} total · you: ${bot.store.spent_today(interaction.user.id):.4f}",
        ephemeral=True,
    )


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
