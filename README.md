# grokbot

A Discord bot that answers with xAI's Grok and searches the web when it needs current information. It runs as a container on the homelab box (Svalbard). See [ethbra/homelab](https://github.com/ethbra/homelab), `stacks/grokbot/`.

## Using it

- `/ask question:<text> search:<Web | Deep | X | None>`: ask Grok a question.
- `@grokbot <question>`, or a DM: same thing, using the default search mode.
- Reply to any bot answer to continue that conversation. Threads last 30 days, which is how long xAI keeps them.
- `/grokusage`: shows today's spend. Only you can see the reply.

Each answer ends with up to 5 source links, which searches ran, and what the answer cost.

| Search mode | How it works | Typical cost per answer |
|---|---|---|
| **Web** (default) | Grok calls the bot's `search_web` function. The bot searches with Tavily and returns 5 short excerpts. | ~$0.005–0.01 |
| **Deep** | xAI's own web and X search. More thorough: it runs follow-up searches and reads whole pages. | ~$0.02–0.05 |
| **X** | xAI's X search, billed per post it fetches | ~$0.05–0.15 |
| **None** | No search | ~$0.001–0.005 |

At about 10 questions a day, that comes to roughly **$1.50–3 a month** for xAI. Tavily's free tier covers 1,000 searches a month. Spending caps (defaults: $2/day for the whole bot, $0.50/day per user, 5-second cooldown) are stored in SQLite, so they survive restarts.

## How it's deployed

```
this repo: push tag v1.2.3 --> Actions builds ghcr.io/ethbra/grokbot:1.2.3 (public image)
homelab:   signed commit bumping the tag in stacks/grokbot/compose.yaml
Svalbard:  homelab-pull applies it; the container connects out to Discord, xAI and Tavily
```

The bot only makes outbound connections, so it needs no DNS, tunnel, nginx route or open port. Secrets live in the homelab repo's SOPS file (`grokbot_discord_token`, `grokbot_xai_api_key`, `grokbot_tavily_api_key`). The stacks role writes them to a root-only `.env` on the box. State (`grokbot.db`) is in `/srv/appdata/grokbot/state/`.

### Releasing

```bash
git tag v0.2.0 && git push origin v0.2.0
```

Wait for the `ci` workflow to go green, then bump `image: ghcr.io/ethbra/grokbot:0.2.0` in the homelab repo with a signed commit to `main`.

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then fill in the keys
python bot.py
```

Use a separate test bot application for local runs, so your tests don't compete with the deployed bot. Put your server ID in `DEV_GUILD_IDS` so slash commands show up instantly.

## Configuration

These are environment variables, all optional except the first two.

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN`, `XAI_API_KEY` | (required) | |
| `TAVILY_API_KEY` | (empty) | Without it, Web mode falls back to Deep. |
| `GROK_MODEL` | `grok-4.3` | `grok-4.7` costs about 2.5x more per token. |
| `DEFAULT_SEARCH` | `web` | Used for mentions, DMs and `/ask` when no mode is picked. |
| `DAILY_BUDGET_USD` / `PER_USER_DAILY_USD` | `2.00` / `0.50` | `0` turns a limit off. |
| `USER_COOLDOWN_S` | `5` | |
| `MAX_OUTPUT_TOKENS` | `1500` | |
| `SEARCH_RESULTS` | `5` | Excerpts per search. More results means more tokens. |
| `SHOW_COST` | `true` | |
| `DEV_GUILD_IDS` | | |

## Discord application setup

1. At https://discord.com/developers/applications, click **New Application**, open **Bot**, then **Reset Token**. The token goes into SOPS as `grokbot_discord_token`.
2. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes. For permissions, tick **View Channels**, **Send Messages**, **Send Messages in Threads** and **Read Message History**. Open the generated URL to invite the bot.
3. No privileged intents are needed.

Everything people send the bot goes to xAI, and search queries go to Tavily. Let your server members know.
