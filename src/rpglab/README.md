# RPGLab code layout

This directory deliberately contains only five Python modules:

1. `models.py` — Markdown package models, persistence, snapshots, bindings, and dice records.
2. `runtime.py` — Director → Narrator → Reviewer orchestration and deterministic rolls.
3. `prompts.py` — every model instruction used by the runtime.
4. `cli.py` — package validation and terminal play.
5. `discord_bot.py` — Discord transport only.

Do not add another Python module. Put new behaviour in the module that owns its concern,
and refactor within that module when necessary. Each file may grow to roughly 1,000 lines each.

Game content and mutable state remain Markdown outside this code directory. Tests remain
under `tests/`. Generated `__pycache__` content is not source and must not be committed.
