from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path

from cmd2 import Cmd, Statement
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.theme import Theme

from .runtime import Runtime, RuntimeResult


AUTHORED_FILES = ("premise.md", "setting.md", "rules.md", "boundaries.md")
AUTHORED_DIRECTORIES = ("characters", "screamsheets")
RUNTIME_FILES = ("state.md", "log.md", "diagnostics.jsonl")
RUNTIME_DIRECTORIES = ("turns",)
PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
CHARACTER_FILE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")
SCREAMSHEET_FILE = re.compile(r"^\d{3}-[a-z0-9][a-z0-9-]*\.md$")
TURN_FILE = re.compile(r"^\d{3,}\.md$")


def validate_package_structure(path: Path, published: bool) -> list[str]:
    """Return structural errors for one draft or published game package."""
    path = Path(path)
    kind = "game" if published else "draft"
    if not path.is_dir():
        return [f"{kind} package is not a directory: {path}"]

    errors = []
    if not PACKAGE_ID.fullmatch(path.name):
        errors.append(f"invalid {kind} ID: {path.name}")
    for name in AUTHORED_FILES:
        if not (path / name).is_file():
            errors.append(f"missing file: {name}")
    for name in AUTHORED_DIRECTORIES:
        if not (path / name).is_dir():
            errors.append(f"missing directory: {name}/")

    characters = path / "characters"
    if characters.is_dir():
        for item in sorted(characters.iterdir()):
            if not item.is_file() or not CHARACTER_FILE.fullmatch(item.name):
                errors.append(f"invalid character entry: characters/{item.name}")

    screamsheets = path / "screamsheets"
    if screamsheets.is_dir():
        entries = sorted(screamsheets.iterdir())
        valid = [
            item for item in entries
            if item.is_file() and SCREAMSHEET_FILE.fullmatch(item.name)
        ]
        if not valid:
            errors.append("screamsheets/ requires at least one NNN-name.md file")
        for item in entries:
            if item not in valid:
                errors.append(f"invalid Screamsheet entry: screamsheets/{item.name}")

    if not published:
        for name in (*RUNTIME_FILES, *RUNTIME_DIRECTORIES):
            if (path / name).exists():
                errors.append(f"runtime path is not allowed in a draft: {name}")
        return errors

    for name in RUNTIME_FILES:
        if not (path / name).is_file():
            errors.append(f"missing runtime file: {name}")
    for name in RUNTIME_DIRECTORIES:
        if not (path / name).is_dir():
            errors.append(f"missing runtime directory: {name}/")

    turns = path / "turns"
    if turns.is_dir():
        entries = sorted(turns.iterdir())
        if not (turns / "000.md").is_file():
            errors.append("turns/ requires the initial snapshot 000.md")
        for item in entries:
            if not item.is_file() or not TURN_FILE.fullmatch(item.name):
                errors.append(f"invalid turn entry: turns/{item.name}")
    return errors


def console() -> Console:
    return Console(theme=Theme({"markdown.h1": "bold cyan", "markdown.h2": "bold cyan"}))


def root() -> Path:
    return Path(os.environ.get("RPGLAB_ROOT", Path.cwd())).resolve()


def load_env(path: Path) -> None:
    file = path / ".env"
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class GameShell(Cmd):
    prompt = "\n> "
    intro = ""

    def __init__(self, runtime: Runtime, game_id: str, dev: bool = False):
        super().__init__(allow_cli_args=False, auto_load_commands=False)
        self.runtime = runtime
        self.game_id = game_id
        self.dev = dev
        self._exit_requested = False
        self.output = console()
        self.hidden_commands.extend([
            "alias", "edit", "history", "macro", "run_pyscript", "run_script",
            "set", "shell", "shortcuts",
        ])

    def sigint_handler(self, signum, frame) -> None:
        self._exit_requested = True
        super().sigint_handler(signum, frame)

    def onecmd_plus_hooks(self, line: str, **kwargs) -> bool:
        stop = super().onecmd_plus_hooks(line, **kwargs)
        return stop or self._exit_requested

    def _show(self, text: str) -> None:
        self.output.print(Markdown(text))

    def _show_result(self, result: RuntimeResult) -> None:
        text = result.display()
        if self.dev:
            text += f"\n\n[{result.duration_ms / 1000:.1f}s, {result.tokens:,} tokens]"
        self._show(text)

    def default(self, statement: Statement):
        raw = statement.raw.strip()
        command, _, argument = raw.partition(" ")
        if command in {"/quit", "/exit"}:
            return True
        if command == "/scene":
            return self.do_scene(statement)
        if command == "/recap":
            return self.do_recap(statement)
        if command == "/ask":
            return self._ask(argument)
        if command == "/act":
            return self._act(argument)
        return self._act(raw)

    def do_act(self, statement: Statement) -> None:
        """act ACTION: perform an action."""
        self._act(statement.args)

    def _act(self, text: str) -> None:
        with self.output.status("Director is thinking…", spinner="line"):
            result = asyncio.run(self.runtime.act(self.game_id, text))
        self._show_result(result)

    def do_ask(self, statement: Statement) -> None:
        """ask QUESTION: ask an in-world question."""
        self._ask(statement.args)

    def _ask(self, text: str) -> None:
        with self.output.status("Director is thinking…", spinner="line"):
            result = asyncio.run(self.runtime.ask(self.game_id, text))
        self._show_result(result)

    def do_scene(self, _statement: Statement) -> None:
        """Show only currently known state."""
        self._show(self.runtime.scene(self.game_id))

    def do_recap(self, _statement: Statement) -> None:
        """Show recent transcript entries."""
        self._show(self.runtime.recap(self.game_id))

    def do_quit(self, _statement: Statement) -> bool:
        """Save and leave the game."""
        return True

    do_exit = do_quit


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="rpglab-cli")
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("games")
    for name in ("play", "scene", "recap", "check"):
        command = commands.add_parser(name)
        if name != "check":
            command.add_argument("game")
        if name == "play":
            command.add_argument("--dev", action="store_true")
            command.add_argument("--reset", action="store_true")
    return value


def main() -> None:
    workspace = root()
    load_env(workspace)
    runtime = Runtime(workspace)
    arguments = parser().parse_args()
    if arguments.command == "games":
        table = Table("Game", "Title")
        for game_id in runtime.games():
            table.add_row(game_id, runtime.game(game_id).title)
        console().print(table)
        return
    if arguments.command == "check":
        failed = False
        for directory_name, published in (("drafts", False), ("games", True)):
            directory = workspace / directory_name
            if not directory.is_dir():
                continue
            for path in sorted(item for item in directory.iterdir() if item.is_dir()):
                errors = validate_package_structure(path, published)
                label = "game" if published else "draft"
                if errors:
                    failed = True
                    console().print(f"{label} {path.name}: invalid")
                    for error in errors:
                        console().print(f"  {error}")
                else:
                    console().print(f"{label} {path.name}: valid")
        if failed:
            raise SystemExit(1)
        return
    if arguments.command == "scene":
        console().print(Markdown(runtime.scene(arguments.game)))
        return
    if arguments.command == "recap":
        console().print(Markdown(runtime.recap(arguments.game)))
        return
    if arguments.reset:
        runtime.reset(arguments.game)
    game = runtime.game(arguments.game)
    character = runtime.bind(arguments.game, "cli")
    if game.turn == 0:
        console().print(Markdown(runtime.opening(arguments.game, character)))
    else:
        console().print(Markdown(runtime.scene(arguments.game, character)))
    GameShell(runtime, arguments.game, dev=arguments.dev).cmdloop()


if __name__ == "__main__":
    main()
