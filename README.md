# grokbot

A Discord bot that answers with a cheap, strong main model (OpenAI `gpt-6-luna` by default) and searches the web when it needs current information. It calls Grok when a question is about X/Twitter. It runs as a container on the homelab box (Svalbard). See [ethbra/homelab](https://github.com/ethbra/homelab), `stacks/grokbot/`.

## Using it

- `/ask question:<text> search:<Auto | Web | X | Deep | None>`: ask a question.
- `@grokbot <question>`, or a DM: same thing, in Auto mode.
- Reply to any bot answer to continue that conversation. A reply continues on the same model that started the thread. Threads last 30 days.
- Reply to **anyone's** message with `@grok is this true?` and the bot sees that message, verbatim, and the reply chain above it (up to `REPLY_DEPTH`, default 25, messages). A bare `@grok` reply works too.
- `/grokusage`: shows today's spend. Only you can see the reply.

### What the bot sees

With a mention, reply, DM or `/ask`, each request contains:
- a short note on where the bot is (a Discord group chat among friends, where crude banter is normal);
- the bot's recent history in that channel or thread: its own earlier answers, verbatim, and a one-line anonymized summary of each question that triggered them, oldest first, with times (at most `HISTORY_ITEMS`, default 20, and `HISTORY_CHARS` characters). So "what did you say earlier?" and "@grok is that right?" work without replying;
- the starter message of a thread or forum post;
- the message being replied to, verbatim, and its parent, and so on up the reply chain (up to `REPLY_DEPTH`, default 25). The walk stops at a deleted message or a reference to another channel;
- images attached to the triggering message or the replied-to message (up to `MAX_IMAGES`, default 4), as vision input. Other files are listed by name only;
- the question, as "*display name* asks: ...".

Raw channel messages from people who never addressed the bot are not sent. `CONTEXT_MESSAGES` (default 0) can turn the old behavior back on: the last N channel messages, with display names.

When you reply to one of the bot's own answers, the stored model conversation (`previous_response_id`) already has that exchange, including the search results it was answered with, so the bot only adds history from after that answer.

All context is passed as quoted data, separate from the instructions, and the model is told not to follow instructions inside it. It costs a few thousand extra input tokens per answer at most, a fraction of a cent on Luna.

#### Stored history and retention

After an answer is sent, the bot stores, per channel, for **24 hours** (`HISTORY_HOURS`), and then deletes:
- its answer, verbatim (without the sources line), with known Discord names replaced by pseudonyms;
- a summary of the question (25 words or fewer), written by one small extra call to `MODEL` (about $0.00005, counted toward the asker's spend). The call happens after the reply is sent, so it never slows an answer. If it fails, a truncated copy of the question is stored instead.

Stored rows hold no Discord user ID, username or display name. The person who asked is shown as a pseudonym like `user-3fa2`: a short keyed hash of the user and channel. The key is random, lives only for the current 24-hour window and is then replaced, so a pseudonym means the same person within one channel and window, and can't be traced back once the key is gone. Names of people the bot saw in the request (the asker, mentions, reply-chain authors) are replaced with their pseudonyms in both the summary and the stored answer. A name typed as plain text that isn't one of those may survive in the stored answer. Bot answers use the same 24-hour retention as the summaries, so the two never drift apart. Expired rows are pruned on every write and at startup. `HISTORY_HOURS=0` turns storage off.

Each answer ends with a small line of up to 5 source links, shown as short masked links (`[site](<url>)`) so they don't fill the chat or unfurl previews. With `SHOW_DETAILS=true` it also lists the models used, which searches ran and what the answer cost; either way that line is logged for every answer.

The default system prompt has the bot answer in character as Grok, the AI of X, whichever model is answering. If someone sincerely asks which model it runs on, it says the bot uses several models, mainly OpenAI's GPT with Grok for X search. Set `SYSTEM_PROMPT` to replace the prompt. The current date and time (the container's `TZ`) are added to every request, so "latest" and "today" mean something.

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
| `HISTORY_HOURS` | `24` | How long the per-channel history (anonymized question summaries + the bot's answers) is kept. `0` stores nothing. |
| `HISTORY_ITEMS` | `20` | Most history entries sent with a request. |
| `HISTORY_CHARS` | `8000` | Character budget for those entries (each counts at most 1,500). |
| `CONTEXT_MESSAGES` | `0` | Raw recent channel messages (anyone's, with display names) sent with a mention. Off by default. |
| `REPLY_DEPTH` | `25` | How far up a reply chain to read. `0` turns it off. |
| `MAX_IMAGES` | `4` | Images passed as vision input. `0` turns it off. |
| `SHOW_DETAILS` | `false` | `true` adds a line under each answer with the models used, searches run and cost. Either way, each answer's details are logged (`docker logs grokbot`). |
| `DEV_GUILD_IDS` | | |

## Discord application setup

1. At https://discord.com/developers/applications, click **New Application**, open **Bot**, then **Reset Token**. The token goes into SOPS as `grokbot_discord_token`.
2. Under **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes. For permissions, tick **View Channels**, **Send Messages**, **Send Messages in Threads** and **Read Message History**. Open the generated URL to invite the bot.
3. Under **Bot → Privileged Gateway Intents**, turn on **Message Content Intent**. The bot requests it, and Discord refuses the connection without it. Verification only applies once the bot is in 100+ servers.

Everything people send the bot goes to OpenAI (and to xAI when Grok is used), and search queries go to Tavily. When someone mentions the bot, the messages in the reply chain they replied to go along too, with display names, even if those people never talked to the bot. Raw channel messages are no longer sent by default (`CONTEXT_MESSAGES=0`). The bot stores, for 24 hours in `/data/state/grokbot.db`, anonymized summaries of the questions it was asked and its own answers (see [Stored history and retention](#stored-history-and-retention)), plus per-user daily spend and the IDs of its own reply messages for conversation threading. Let your server members know.
