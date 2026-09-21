import ast
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rpglab.cli import parser, validate_package_structure
from rpglab.discord_bot import Bot
from rpglab.runtime import Runtime


ROOT = Path(__file__).parents[1]
GAME_ID = "the-last-lantern"


class InterfaceTest(TestCase):
    def test_only_allowed_source_files_exist(self):
        source = ROOT / "src" / "rpglab"
        self.assertEqual(
            {path.name for path in source.iterdir() if path.is_file()},
            {"README.md", "models.py", "runtime.py", "prompts.py", "cli.py", "discord_bot.py"},
        )
        for name in ("models.py", "runtime.py", "cli.py", "discord_bot.py"):
            self.assertLessEqual(len((source / name).read_text().splitlines()), 1000, name)

    def test_all_model_prompts_live_in_prompts_module(self):
        runtime = ast.parse((ROOT / "src" / "rpglab" / "runtime.py").read_text())
        assigned_names = {
            target.id for node in ast.walk(runtime) if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        self.assertFalse({name for name in assigned_names if name.endswith("INSTRUCTIONS")})

    def test_validator_accepts_draft_and_initialized_game(self):
        self.assertEqual(
            validate_package_structure(ROOT / "drafts" / GAME_ID, False), [],
        )
        self.assertEqual(
            validate_package_structure(ROOT / "games" / GAME_ID, True), [],
        )

    def test_validator_rejects_runtime_state_in_draft(self):
        with TemporaryDirectory() as directory:
            package = Path(directory) / "drafts" / "small-game"
            shutil.copytree(ROOT / "drafts" / GAME_ID, package)
            (package / "state.md").write_text("# State\n")
            self.assertIn(
                "runtime path is not allowed in a draft: state.md",
                validate_package_structure(package, False),
            )

    def test_cli_accepts_play_and_reset(self):
        arguments = parser().parse_args(["play", GAME_ID, "--reset"])
        self.assertEqual(arguments.game, GAME_ID)
        self.assertTrue(arguments.reset)

    def test_discord_bot_constructs_with_runtime(self):
        bot = Bot(Runtime(ROOT))
        self.assertIsInstance(bot, Bot)
