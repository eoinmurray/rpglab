# RPGLab

RPGLab is a small, fiction-first roleplaying runtime. The game state is ordinary
Markdown prose. A Director evolves that state, a Narrator renders one public event,
and a Reviewer gates the complete turn before anything is saved.

## Architecture

```text
models.py       Markdown state, immutable turn snapshots, storage, transcript
runtime.py      Director, Narrator, Reviewer, repair loop, and turn orchestration
discord_bot.py  Discord interface backed only by Runtime
cli.py          cmd2 and Rich terminal interface backed only by Runtime
```

## Game files

```text
games/<game-id>/
  state-init.md      immutable starting state
  state.md           freely evolving current state
  turns/001.md       immutable accepted turn and complete state-after snapshot
  log.md             player-facing transcript
  diagnostics.jsonl  prompts, model outputs, timing, tokens, reviews, and errors
```

`state.md` normally describes the current scene, world in motion, characters,
active narrative arcs, established facts, and unresolved possibilities. These are
prose conventions rather than a graph schema. Possibilities are not facts and may
change until play establishes an answer.

Authored mysteries also carry a private `Secrets` section inside `state.md`. It holds
the causal history, current hidden situation, and revelation status. The Director
carries it forward; the Narrator never receives it; the Reviewer rejects accidental
leaks or rewritten history. Earned discoveries are added to `Established facts` and
marked revealed without deleting the underlying secret.

Every accepted turn stores the complete resulting state. Turn zero is
`state-init.md`; turn N is the `State after` section of `turns/NNN.md`. `state.md`
is the convenient latest copy and can be restored from the newest snapshot.

## Runtime chain

The Director receives the complete state, recent turn summaries, and player input.
It returns one public event and a complete replacement state. The Narrator sees only
the previous public scene, established public facts, and that event. It writes the
short response shown to the player.

The Reviewer then sees the previous and proposed states, event, input, and narration.
It checks continuity, agency, leaks, spoilers, pacing, repetition, and whether the
turn is interesting. It never edits. A Director rejection restarts Director and
Narrator; a prose-only rejection reruns Narrator. After at most one repair, an
approved turn is committed atomically enough to recover from its turn snapshot.

There are deliberately no deterministic gameplay checks yet. Pydantic only parses
the small agent envelopes, and filesystem code protects turn ordering and writes.

## Terminal

```sh
uv sync
uv run rpglab-cli games
uv run rpglab-cli check
uv run rpglab-cli play the-clockwork-robin
uv run rpglab-cli play the-clockwork-robin --dev
uv run rpglab-cli play the-clockwork-robin --reset --dev
```

An untouched game prints only its opening sentence. Ordinary input and `act` are
treated as actions; `ask` supplies a question hint but is still an in-world turn.
`scene`, `recap`, `quit`, and their slash-prefixed forms remain available.

Developer mode appends aggregate Director, Narrator, and Reviewer latency and token
usage to each response.

## Models

The default for all roles is `gpt-5.6-luna` with low reasoning. Override roles with
`OPENAI_DIRECTOR_MODEL`, `OPENAI_NARRATOR_MODEL`, or `OPENAI_REVIEWER_MODEL`, and
their corresponding `*_REASONING_EFFORT` variables.

## Discord

Set `DISCORD_BOT_API_KEY` and optionally `DISCORD_GUILD_ID`, then run:

```sh
uv run rpglab-discord
```

## Tests

```sh
uv run python -m unittest discover -s tests -v
```
