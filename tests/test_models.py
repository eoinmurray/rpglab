import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rpglab.models import GameStore, TurnRecord, public_context


ROOT = Path(__file__).parents[1]
GAME_ID = "the-clockwork-robin"


def copy_fresh_game(root):
    source = ROOT / "games" / GAME_ID
    shutil.copytree(source, root / "games" / GAME_ID)
    game = root / "games" / GAME_ID
    for path in (game / "turns").glob("*.md"):
        path.unlink()
    (game / "state.md").write_text((game / "state-init.md").read_text(), encoding="utf-8")
    (game / "log.md").write_text("# Transcript\n", encoding="utf-8")
    (game / "diagnostics.jsonl").write_text("", encoding="utf-8")


class ModelTest(TestCase):
    def test_repository_game_uses_markdown_state(self):
        path = ROOT / "games" / GAME_ID
        self.assertTrue((path / "state-init.md").is_file())
        self.assertTrue((path / "state.md").is_file())
        self.assertTrue((path / "turns").is_dir())
        game = GameStore(ROOT).load(GAME_ID)
        self.assertEqual(game.title, "The Clockwork Robin")
        self.assertEqual(game.opening, "You wake in darkness to three hard knocks on your door.")
        self.assertEqual(GameStore(ROOT).load_initial(GAME_ID).turn, 0)
        self.assertGreaterEqual(game.turn, 0)

    def test_public_context_excludes_private_prose_sections(self):
        game = GameStore(ROOT).load_initial(GAME_ID)
        rendered = public_context(game.state)
        self.assertIn("Current scene", rendered)
        self.assertIn("Established facts", rendered)
        self.assertNotIn("Bea Lark", rendered)
        self.assertNotIn("Open possibilities", rendered)
        self.assertNotIn("First Primroses", rendered)
        self.assertNotIn("Secrets", rendered)

    def test_turn_snapshot_reconstructs_historical_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_fresh_game(root)
            store = GameStore(root)
            initial = store.load(GAME_ID).state
            changed = initial.replace("Someone is outside the door.", "Mrs Alder is inside the room.")
            store.append_turn(GAME_ID, TurnRecord(
                trace_id="test", number=1, input_kind="action",
                player_input="I open the door", event="Mrs Alder enters.",
                narration="Mrs Alder steps inside.", state_after=changed,
            ))
            self.assertEqual(store.state_at(GAME_ID, 0), initial)
            self.assertEqual(store.state_at(GAME_ID, 1), changed)
            self.assertEqual(store.load(GAME_ID).state, changed)

    def test_state_can_be_replaced_as_free_markdown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_fresh_game(root)
            store = GameStore(root)
            free_state = "# A changed story\n\n## Status\n\nOngoing.\n"
            store.append_turn(GAME_ID, TurnRecord(
                trace_id="test", number=1, input_kind="action", player_input="I wait",
                event="Time passes.", narration="The rain eases.", state_after=free_state,
            ))
            self.assertEqual(store.load(GAME_ID).state, free_state)

    def test_reset_removes_turns_and_restores_initial_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_fresh_game(root)
            store = GameStore(root)
            store.append_turn(GAME_ID, TurnRecord(
                trace_id="test", number=1, input_kind="action", player_input="I wait",
                event="Time passes.", narration="The rain eases.", state_after="# Changed\n",
            ))
            store.append_diagnostic(GAME_ID, stage="test")
            store.reset(GAME_ID)
            self.assertEqual(store.load(GAME_ID).state, store.load_initial(GAME_ID).state)
            self.assertEqual(store.turn_paths(GAME_ID), [])
            self.assertEqual((store.path(GAME_ID) / "log.md").read_text(), "")
            self.assertEqual((store.path(GAME_ID) / "diagnostics.jsonl").read_text(), "")
