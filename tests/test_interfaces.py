import ast
import io
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rpglab.cli import GameShell, main, parser
from rpglab.runtime import Runtime, RuntimeResult


ROOT = Path(__file__).parents[1]
GAME_ID = "the-clockwork-robin"


def copy_fresh_game(root):
    shutil.copytree(ROOT / "games" / GAME_ID, root / "games" / GAME_ID)
    game = root / "games" / GAME_ID
    for path in (game / "turns").glob("*.md"):
        path.unlink()
    (game / "state.md").write_text((game / "state-init.md").read_text(), encoding="utf-8")
    (game / "log.md").write_text("# Transcript\n", encoding="utf-8")
    (game / "diagnostics.jsonl").write_text("", encoding="utf-8")


class InterfaceTest(TestCase):
    def test_interfaces_depend_on_runtime_not_game_internals(self):
        for name in ("cli.py", "discord_bot.py"):
            tree = ast.parse((ROOT / "src/rpglab" / name).read_text(encoding="utf-8"))
            imports = {
                node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            self.assertIn("runtime", imports)
            self.assertNotIn("models", imports)

    def test_all_code_modules_are_under_700_lines(self):
        for path in (ROOT / "src/rpglab").glob("*.py"):
            self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 700, path.name)

    def test_cmd2_shell_constructs(self):
        shell = GameShell(Runtime(ROOT), GAME_ID)
        self.assertEqual(shell.game_id, GAME_ID)
        self.assertTrue(shell.onecmd("/quit"))

    def test_dev_shell_appends_turn_metrics(self):
        shell = GameShell(Runtime(ROOT), GAME_ID, dev=True)
        with patch.object(shell, "_show") as show:
            shell._show_result(RuntimeResult(
                outcome="resolved", narration="The door opens.",
                duration_ms=6840, tokens=2450,
            ))
        show.assert_called_once_with("The door opens.\n\n[6.8s, 2,450 tokens]")

    def test_play_parser_accepts_reset(self):
        arguments = parser().parse_args(["play", GAME_ID, "--reset"])
        self.assertTrue(arguments.reset)

    def test_untouched_game_prints_only_opening(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_fresh_game(root)
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"RPGLAB_ROOT": directory}),
                patch.object(sys, "argv", ["rpglab-cli", "play", GAME_ID]),
                patch("rpglab.cli.GameShell"), patch("sys.stdout", output),
            ):
                main()
            rendered = output.getvalue()
            self.assertIn("You wake in darkness to three hard knocks on your door.", rendered)
            self.assertNotIn("Rosehip", rendered)
            self.assertNotIn("Ivy Rook", rendered)
