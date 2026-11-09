# grokbot

A Discord bot that answers with a cheap, strong main model (OpenAI `gpt-6-luna` by default) and searches the web when it needs current information. It calls Grok when a question is about X/Twitter. It runs as a container on the homelab box (Svalbard). See [ethbra/homelab](https://github.com/ethbra/homelab), `stacks/grokbot/`.

## Using it

- `/ask question:<text> search:<Auto | Web | X | Deep | None>`: ask a question.
- `@grokbot <question>`, or a DM: same thing, in Auto mode.
- Reply to any bot answer to continue that conversation. A reply continues on the same model that started the thread. Threads last 30 days.
- Reply to **anyone's** message with `@grok is this true?` and the bot sees that message (and up to `REPLY_DEPTH` levels of the reply chain above it). A bare `@grok` reply works too.

### What the bot sees

With a mention, reply or DM, the bot also gets:
- the last `CONTEXT_MESSAGES` (default 12) messages in the channel or thread, with display names and times, so "@grok is that right?" works without replying;
- the message being replied to, and its reply chain;
- the starter message of a thread or forum post;
- images attached to the triggering message or the replied-to message (up to `MAX_IMAGES`, default 4), as vision input. Other files are listed by name only.

When you reply to one of the bot's own answers, the stored conversation already has the earlier context, so the bot only adds the messages posted since that answer.

All of this is passed as quoted data, separate from the instructions, and the model is told not to follow instructions inside it. It costs about 1–2k extra input tokens per answer, which is a fraction of a cent on Luna.
- `/grokusage`: shows today's spend. Only you can see the reply.

Each answer ends with up to 5 source links, the models used, which searches ran (with how many X posts were fetched), and what the answer cost.

The default system prompt has the bot answer in character as Grok, the AI of X, whichever model is answering. If someone sincerely asks which model it runs on, it says the bot uses several models and points to the footer. Set `SYSTEM_PROMPT` to replace the prompt. The current date and time (the container's `TZ`) are added to every request, so "latest" and "today" mean something.

### How it decides when to use Grok

There is no separate routing model. The main model gets two functions and decides when to call them, based on their descriptions and the system prompt:

- `search_web`: the bot runs a Tavily search and returns 5 short excerpts.
- `search_x`: the bot hands the query to Grok with xAI's `x_search`. The model can pass `handles` (only these accounts; returns their newest posts) and `days` (only recent posts). Grok returns a summary with post links, and the main model writes the answer. This is offered only when `XAI_API_KEY` is set, and it is capped at `X_SEARCHES_PER_ANSWER` (default 1) per answer, because xAI bills X search per post fetched.

So "is it true JFK died in 1962?" costs one cheap web search. "What are people on X saying about the launch?" also brings in Grok.

| Mode | What happens | Typical cost per answer (estimates) |
|---|---|---|
| **Auto** (default) | Luna, with `search_web` and `search_x` available | ~$0.001 (web only), ~$0.05–0.10 when it uses X |
| **Web** | Luna, `search_web` only | ~$0.001 |
| **X** | Luna is made to call `search_x` first | ~$0.05–0.10 |
| **Deep** | The whole question goes to Grok with xAI's web and X search | ~$0.02–0.10 |
| **None** | Luna, no tools | ~$0.0003 |

At about 10 questions a day, mostly web, that comes to roughly **$0.30–0.50 a month** for OpenAI. Add about $0.05–0.10 for each answer that uses X. X search is where most of the cost goes. Tavily's free tier covers 1,000 searches a month. Spending caps (defaults: $2/day for the whole bot, $0.50/day per user, 5-second cooldown) are stored in SQLite, so they survive restarts. The bot computes OpenAI costs from token counts and the price table in `bot.py` (`MODEL_PRICE` overrides it). xAI reports its own exact cost.

## How it's deployed

```
this repo: push tag v1.2.3 --> Actions builds ghcr.io/ethbra/grokbot:1.2.3 (public image)
homelab:   signed commit bumping the tag in stacks/grokbot/compose.yaml
Svalbard:  homelab-pull applies it; the container connects out to Discord, OpenAI, xAI and Tavily
```

The bot only makes outbound connections (to Discord, OpenAI, xAI and Tavily), so it needs no DNS, tunnel, nginx route or open port. Secrets live in the homelab repo's SOPS file (`grokbot_discord_token`, `grokbot_openai_api_key`, `grokbot_xai_api_key`, `grokbot_tavily_api_key`). The stacks role writes them to a root-only `.env` on the box. State (`grokbot.db`) is in `/srv/appdata/grokbot/state/`.

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

These are environment variables. See `.env.example`.

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | (required) | |
| `OPENAI_API_KEY` | | Required for the default `gpt-6-luna`. Not needed if `MODEL` is a `grok-*` model. |
| `XAI_API_KEY` | | Optional. Enables `search_x` and Deep mode. |
| `TAVILY_API_KEY` | | Without it there is no web search outside Deep mode. |
| `MODEL` | `gpt-6-luna` | Any OpenAI or `grok-*` model. Set `MODEL_PRICE` if it isn't in the built-in price table. |
| `GROK_MODEL` | `grok-4.3` | Used for `search_x` and Deep mode. |
| `REASONING_EFFORT` | (model default) | OpenAI reasoning models only: `low`, `medium` or `high`. |
| `DEFAULT_SEARCH` | `auto` | Used for mentions, DMs and `/ask` when no mode is picked. |
| `X_SEARCHES_PER_ANSWER` | `1` | |
| `DAILY_BUDGET_USD` / `PER_USER_DAILY_USD` | `2.00` / `0.50` | `0` turns a limit off. |
| `USER_COOLDOWN_S` | `5` | |
| `MAX_OUTPUT_TOKENS` | `4000` | Reasoning tokens count toward this. |
| `SEARCH_RESULTS` | `5` | Excerpts per web search. |
| `CONTEXT_MESSAGES` | `12` | Recent channel messages sent with a mention. `0` turns it off. |
| `REPLY_DEPTH` | `5` | How far up a reply chain to read. |
| `MAX_IMAGES` | `4` | Images passed as vision input. `0` turns it off. |
| `SHOW_COST` | `true` | |
| `DEV_GUILD_IDS` | | |

## Discord application setup

1. At https://discord.com/developers/applications, click **New Application**, open **Bot**, then **Reset Token**. The token goes into SOPS as `grokbot_discord_token`.
2. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes. For permissions, tick **View Channels**, **Send Messages**, **Send Messages in Threads** and **Read Message History**. Open the generated URL to invite the bot.
3. Under **Bot → Privileged Gateway Intents**, turn on **Message Content Intent**. The bot requests it, and Discord refuses the connection without it. Verification only applies once the bot is in 100+ servers.

Everything people send the bot goes to OpenAI (and to xAI when Grok is used), and search queries go to Tavily. When someone mentions the bot, the recent messages in that channel go along too, including messages from people who never talked to the bot. Let your server members know.
