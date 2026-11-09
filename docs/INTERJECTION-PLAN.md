# Plan: answering follow-ups without an @mention ("interjecting")

Status: **plan only, not built.** Hard requirement: the decision to answer is made on
Svalbard. A message the bot wasn't addressed with never goes to OpenAI, xAI or Tavily
unless the local check says yes.

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
