# Plan: answering follow-ups without an @mention ("interjecting")

Status: **built** (after v0.4.0). The user-facing description is in the README under "Follow-ups
without @". Hard requirement: the decision to answer is made on Svalbard. A message the bot
wasn't addressed with never goes to OpenAI, xAI or Tavily unless the local check says yes.

## What was built, and how it differs from the plan below

The owner's decisions changed the rollout:

- **No shadow phase.** It went live with the heuristics and weights below, the window
  (120 s) and the chain limit (2).
- **On by default in every channel** (`INTERJECT_DEFAULT=on`).
  - `/grokinterject on | off | status` (Manage Channels for on/off) replaces the planned
    `off | shadow | live | status`.
  - The setting lives in `channel_settings`.
- **The local model ships now.** Ollama runs with `network_mode: "service:grokbot"`, so it
  is at `http://127.0.0.1:11434` (`OLLAMA_URL`; empty = no model stage).
  - At startup the bot pulls `INTERJECT_MODEL` (`qwen2.5:1.5b-instruct-q4_K_M`) in the
    background and retries with backoff while Ollama isn't up, then loads it with
    `keep_alive: -1`.
  - The check uses `/api/chat` with temperature 0 and a JSON-schema `format` forcing
    `{"addressed": bool}`.
- **Timeout and fallback.** The timeout is 5 s (`INTERJECT_TIMEOUT_S`), not 2 s, because the
  model may run on the CPU if driver 535 doesn't work with this Ollama. On a model error or
  timeout the bot **falls back to the strict heuristic** (score of 8 or more) instead of
  staying silent.
  - Model configured: answers when the score is 5 or more and the model says yes.
  - No model configured: answers when the score is 8 or more.
- **"Other people talking" in detail:**
  - One message from someone else gives -3, and -2 more if it was the last message before
    the follow-up.
  - Two such messages, or anyone @mentioning or replying to the asker, close the
    conversation.
- **Limits.** `INTERJECT_PER_CHANNEL_HOUR=6`. There is no separate interjection dollar
  budget: the normal caps apply. Over a cap, the bot stays silent instead of posting a
  refusal.
- **Shut-up hatch.**
  - Triggers: a 🤫 or 🔇 reaction on an unprompted answer, or "not you / shut up / stfu ...
    grok" within 10 min of one.
  - Effect: closes the conversation and mutes the channel for 30 min.
  - A "shut up" text only counts after a recent interjection. Otherwise "@grok shut up" is
    ordinary banter and gets answered.
  - Reactions use the default (non-privileged) reactions intent.
- **Decision log.**
  - `interject_log` gets one row per message from the current asker; other people's
    messages aren't logged.
  - One `log.info` line per candidate (score of 5 or more).
  - Labels `missed` and `false_positive` are filled in automatically.
  - Rows are deleted after 30 days.
- **Conversation state** (asker, answer, chain, other messages) is in memory only. A restart
  just closes open conversations.
- **Not built:** an embeddings stage, and answering a non-asker who says "grok, ..." without
  an @ (open question 4 below).

The rest of this file is the original plan, kept for reference.

## Server facts (checked 2026-11-09)

| | |
|---|---|
| CPU | i7-9700K, 8 cores, AVX2 |
| RAM | 31 GiB, ~19 GiB free, but swap full and an OOM kill on 2026-10-05: any model container needs `mem_limit` |
| GPU | RTX 2070 SUPER, 8 GB, nearly idle (Plex uses it for transcodes). Driver 535 (CUDA 12.2): check that a current Ollama actually uses the GPU before relying on it |
| Model servers | none running |

## Design: a staged local pipeline in `on_message`

Runs before `build_prompt` or the typing indicator, so nothing leaves the box until it says yes.

**Stage 1: free heuristics.** In memory, one "open conversation" per (channel, asker)
after each answer.

Hard skips:
- the message is not from the last asker
- the window has expired (default 120 s)
- the message replies to or mentions another person, a role or @everyone
- more than one other human has spoken since the answer
- link, emoji or attachment only
- more than 400 characters
- a plain "thanks/ok/lol"
- 2 unprompted answers already in this chain

Score:

| Signal | Points |
|---|---|
| Continuation opener (but / and / so / wait / what about / what if / why) | +3 |
| Contains `?` | +2 |
| "you / u / ur / your" | +2 |
| Bot's name used to address it ("grok, ...") | +3 |
| Bot's name in the third person ("grok is ...") | -3 |
| Bot answered under 45 s ago | +2 |
| 15 words or fewer | +1 |
| Another human spoke in between | -3 |

A score of 5 or more makes the message a candidate.

**Stage 2: small local model, candidates only.** Qwen2.5-1.5B-Instruct (Q4) on Ollama, on
the GPU with a CPU fallback.
- Input: the last ~6 cached messages, with speakers relabelled "Grok", "User A", "User B".
- Question: "Is the LAST message addressed to the assistant and expecting it to respond?"
- Settings: forced yes/no output, temperature 0, 2 s timeout.
- If the model is down or times out, stay silent.

Embedding models and zero-shot classifiers were considered and rejected as a stage. They
measure topic similarity, not who is being addressed: "and 1944?" scores low, and two
friends discussing the same topic scores high.

**Stage 3: the normal answer path.**
- Spend caps apply, plus interjection-only limits: per-channel hourly cap, and a daily
  sub-budget.
- Silent when a limit is hit.
- Continues the stored thread of the bot's last answer.

## Controls and safety

- `/grokinterject off | shadow | live | status` per channel, Manage Channels permission,
  stored in a `channel_settings` table, **default off**. DMs are unaffected. CI's
  command-set check needs updating.
- Shut-up hatch: a chosen reaction on an interjection, or "not you, grok", closes the
  conversation and mutes interjections in that channel for 30 min.
- Decision log table `interject_log`, which stores no message text:
  - ids, mode, features, score, stage reached, model verdict and latency, decision
  - an auto-filled label: an @mention within 90 s after a "no" is a missed one; a
    shut-up reaction is a false positive
  - purged after 30 days

## Homelab fit

- Ollama as a pinned image (e.g. `ollama/ollama:0.40.x`):
  - `read_only`, user 1000, model volume, `mem_limit: 3g`
  - `NVIDIA_VISIBLE_DEVICES=all`
  - no published ports
- Networking: grokbot is on the default `bridge`, which has no container DNS. Run the
  model server with `network_mode: "service:grokbot"` and call `http://127.0.0.1:11434`.
  - The alternative is an `internal: true` network with no internet access. It isolates
    better, but breaks the `network_mode: bridge` convention.
- No secrets, so no SOPS changes.

## Rollout

| Phase | What | Effort |
|---|---|---|
| 1 | Heuristics + `/grokinterject` + log, **shadow** in 1-2 channels for 1-2 weeks; target precision of 95% or more | ~1-1.5 days |
| 2 | Heuristics live, strict (score of 8 or more, same asker, 120 s, chain of 2 or fewer) | ~0.5 day |
| 3 | Ollama container, model stage in shadow next to live heuristics; confirm GPU use | ~1.5-2 days |
| 4 | Require the model's yes (candidate score of 5 or more); review precision monthly | small |

## Open questions for the owner

1. Is a separate model container OK? It is a multi-GB image and needs ~2 GB of VRAM next
   to Plex. The alternative is heuristics only.
2. What window and chain limit (120 s / 2)?
3. Who can toggle a channel: Manage Channels, or the owner only?
4. Should a non-asker saying "grok, ..." with no @ ever trigger an answer? Suggested: not
   in v1.
5. What interjection budget and hourly cap?
6. Which shut-up reaction, and how long should the mute last?
7. Shared network namespace, or an internal network?
