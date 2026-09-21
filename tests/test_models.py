import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rpglab.models import GameStore, TurnRecord


ROOT = Path(__file__).parents[1]
GAME_ID = "the-last-lantern"


def copy_game(root: Path) -> Path:
    destination = root / "games" / GAME_ID
    shutil.copytree(ROOT / "games" / GAME_ID, destination)
    GameStore(root).reset(GAME_ID)
    return destination


class ModelTest(TestCase):
    def test_loads_markdown_package_and_opening(self):
        store = GameStore(ROOT)
        game = store.load(GAME_ID)

        self.assertEqual(game.title, "The Last Lantern")
        self.assertEqual(game.screamsheet_id, "001-beacon-seven")
        self.assertEqual(game.default_character, "mara-venn")
        opening = store.opening(GAME_ID)
        self.assertIn("medicine ferry", opening)
        self.assertIn("maintenance platform", opening)

    def test_turn_updates_state_and_character_then_reset_restores_both(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            game_path = copy_game(root)
            store = GameStore(root)
            initial = store.load(GAME_ID)
            character = initial.characters["mara-venn"]
            changed_state = initial.state.replace("Forty-five minutes", "Thirty-five minutes")
            changed_character = character.replace("maintenance platform", "cable tram")

            store.append_turn(GAME_ID, TurnRecord(
                trace_id="test", number=1, actor="Mara Venn", input_kind="action",
                player_input="I depart", event="The tram leaves.",
                narration="The tram rolls into the rain.",
                state_after=changed_state, character_after=changed_character,
            ))

            self.assertEqual(store.load(GAME_ID).turn, 1)
            self.assertIn("Thirty-five minutes", store.load(GAME_ID).state)
            self.assertIn("cable tram", store.load(GAME_ID).characters["mara-venn"])
            self.assertEqual(store.state_at(GAME_ID, 1), changed_state)

            store.reset(GAME_ID)

            reset = store.load(GAME_ID)
            self.assertEqual(reset.turn, 0)
            self.assertIn("Forty-five minutes", reset.state)
            self.assertIn("maintenance platform", reset.characters["mara-venn"])
            self.assertEqual((game_path / "log.md").read_text(), "# Transcript\n")

    def test_discord_binding_is_owned_by_starting_user(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_game(root)
            store = GameStore(root)
            store.bind(GAME_ID, "discord", 123, "user-1")

            self.assertEqual(store.channel_context(123)["game_id"], GAME_ID)
            self.assertEqual(store.actor_for_user(GAME_ID, 123, "user-1"), "mara-venn")
            with self.assertRaisesRegex(ValueError, "Only the player"):
                store.actor_for_user(GAME_ID, 123, "user-2")
