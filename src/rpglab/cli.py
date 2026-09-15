from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from cmd2 import Cmd, Statement
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.theme import Theme

from .runtime import Runtime, RuntimeResult


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
        self.output = console()
        self.hidden_commands.extend([
            "alias", "edit", "history", "macro", "run_pyscript", "run_script",
            "set", "shell", "shortcuts",
        ])

    def _show(self, text: str) -> None:
        self.output.print(Markdown(text))

    def _show_result(self, result: RuntimeResult) -> None:
        text = result.narration
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
        for game_id in runtime.games():
            runtime.game(game_id)
            console().print(f"{game_id}: valid")
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
    runtime.bind(arguments.game, "cli")
    if game.turn == 0:
        console().print(Markdown(game.opening))
    else:
        console().print(Markdown(runtime.scene(arguments.game)))
    GameShell(runtime, arguments.game, dev=arguments.dev).cmdloop()


if __name__ == "__main__":
    main()
