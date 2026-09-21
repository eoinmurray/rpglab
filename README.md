# RPGLab

RPGLab is a Markdown-first tabletop RPG runtime. Clone it, add an OpenAI API key, and
start the included single-player game from the terminal.

## Getting started

You need Git, [uv](https://docs.astral.sh/uv/), Python 3.9 or newer, and an OpenAI API
key.

```sh
git clone https://github.com/eoinmurray/rpglab.git
cd rpglab
uv sync
cp .env.example .env
```

Open `.env` and set:

```dotenv
OPENAI_API_KEY=your-api-key
```

Validate the packages, then start the included test game from its initial state:

```sh
uv run rpglab-cli check
uv run rpglab-cli play the-last-lantern --reset
```

Type what your character attempts and press Enter. The game responds with the result
and waits for the next action. Inside play, `ask`, `scene`, `recap`, and `quit` are also
available, with optional leading slashes.

The `rpglab-cli` command is the installed entry point for `src/rpglab/cli.py`. Running
the file directly is not supported because it is part of the `rpglab` package.

## Create a game

Use an AI coding agent with this repository open. The agent should interview you, create
the Markdown package, initialize its runtime state, and run the validator. You do not
need to create the files by hand.

Start the conversation with something like:

```text
Create a new single-player RPGLab game with me. Begin by asking me, one question at a
time, about the kind of character I want to play, the setting, tone, themes, boundaries,
and the sort of first adventure I want. Help me make choices when I am unsure.

Once you understand the game, create it as a draft using the repository's current
package format and the-last-lantern as a structural example. Make the first Screamsheet
a bounded, immediately playable situation rather than a plotted story: establish who I
am, what I can do, why I must act now, the stakes, several viable approaches, useful
people and places, hidden truths, pressure triggers, and durable completion conditions.

Show me a concise summary of the draft. After I approve it, initialize it under games/
with its opening character state, shared state, and turns/000.md reset snapshot. Run
`uv run rpglab-cli check`, fix any errors, and tell me the exact command to start playing.
Do not change the runtime code unless I explicitly ask you to.
```

You can give the AI as much or as little of the initial idea as you have. For example:

```text
I want to play an exhausted diplomatic courier carrying something dangerous across a
flooded city. Keep it tense but humane, with no horror and no predetermined solution.
```

The AI should stop for your approval after drafting the premise, setting, rules,
boundaries, character, and first Screamsheet. Once it has initialized and validated the
approved game, start it with the command it provides, normally:

```sh
uv run rpglab-cli play <game-id> --reset
```

## How it works

A game package supplies a premise, setting, rules, boundaries, character sheets, and
authored Screamsheets. The runtime directs player actions through those prepared
situations without prescribing scene order or outcomes.

## Package layout

```text
games/<game-id>/
  premise.md
  setting.md
  rules.md
  boundaries.md
  characters/
    <character-id>.md
  screamsheets/
    001-first-job.md
  state.md
  turns/
    000.md
    001.md
  log.md
  diagnostics.jsonl
```

The four root Markdown documents and `screamsheets/` are stable authored material.
Character files contain individual sheets, current scenes, conditions, equipment, and
private knowledge. `state.md` contains shared mutable state. Every accepted turn stores
the complete resulting shared state and acting character sheet under `turns/`.

Drafts use the same authored structure but omit `state.md`, `turns/`, `log.md`, and
`diagnostics.jsonl`.

## Runtime

The Director receives the complete package, current shared state, acting character,
recent turns, and player input. It proposes one event plus replacement shared and
character state. Python performs any requested d20 roll. The Narrator renders the
approved event from the player's viewpoint. The Reviewer checks agency, continuity,
Screamsheet fidelity, pressure, secrets, rules, and prose before the turn is saved.

Difficulty is 10, 14, or 18. Modifiers are -2, 0, or +2 when the character sheet or
preparation clearly warrants them. Natural 1 and 20 are critical. Other results fail
below the difficulty, succeed at it, and strongly succeed by five or more.

## Terminal

```sh
uv run rpglab-cli check
uv run rpglab-cli games
uv run rpglab-cli play the-last-lantern
uv run rpglab-cli play the-last-lantern --reset
```

Use `--reset` to restore the game to `turns/000.md` before playing. Without it, play
continues from the saved state.

## Discord

Set `DISCORD_BOT_API_KEY` and optionally `DISCORD_GUILD_ID`, then run:

```sh
uv run rpglab-discord
```

Use `/games`, then `/start`. Ordinary messages in the bound channel are actions;
`/act`, `/ask`, `/scene`, and `/recap` are explicit alternatives. The player who starts
the game owns that channel binding.

## Code layout

The source contract is documented in `src/rpglab/README.md`. Only `models.py`,
`runtime.py`, `prompts.py`, `cli.py`, and `discord_bot.py` are permitted Python modules.
All model instructions live in `prompts.py`.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```
