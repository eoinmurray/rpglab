import asyncio
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rpglab.models import DiceCheck, GameStore
from rpglab.runtime import DirectorOutput, NarratorOutput, ReviewerOutput, Runtime


ROOT = Path(__file__).parents[1]
GAME_ID = "the-last-lantern"


def copy_game(root: Path) -> None:
    destination = root / "games" / GAME_ID
    shutil.copytree(ROOT / "games" / GAME_ID, destination)
    GameStore(root).reset(GAME_ID)


async def approve(_context):
    return ReviewerOutput(approved=True, target="none")


class RuntimeTest(TestCase):
    def test_resolved_turn_persists_shared_and_character_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_game(root)

            async def director(context):
                return DirectorOutput(
                    outcome="resolved", event="Mara launches the tram into the rain.",
                    state_after=context["STATE"].replace(
                        "Forty-five minutes remain", "Thirty-five minutes remain",
                    ),
                    character_after=context["CHARACTER"].replace(
                        "maintenance platform", "moving cable tram",
                    ),
                )

            async def narrator(_context):
                return NarratorOutput(narration="The tram rolls out above the black water.")

            runtime = Runtime(root, director, narrator, approve)
            result = asyncio.run(runtime.act(GAME_ID, "I depart in the tram"))

            self.assertEqual(result.outcome, "resolved")
            self.assertEqual(runtime.game(GAME_ID).turn, 1)
            self.assertIn("Thirty-five minutes", runtime.game(GAME_ID).state)
            self.assertIn("moving cable tram", runtime.game(GAME_ID).characters["mara-venn"])

    def test_check_is_rolled_in_code_and_resolved_on_second_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_game(root)
            calls = []

            async def director(context):
                calls.append(context)
                if "ROLL_RECEIPT" not in context:
                    return DirectorOutput(
                        outcome="check", event="The crossing is dangerous.",
                        state_after=context["STATE"], character_after=context["CHARACTER"],
                        check=DiceCheck(
                            reason="Cross the damaged rail", difficulty=14, modifier=2,
                            success_stakes="Reach the beacon", failure_stakes="Lose time",
                        ),
                    )
                return DirectorOutput(
                    outcome="resolved", event="Mara reaches the beacon.",
                    state_after=context["STATE"], character_after=context["CHARACTER"],
                )

            async def narrator(context):
                return NarratorOutput(narration=context["PUBLIC_EVENT"])

            runtime = Runtime(root, director, narrator, approve, roller=lambda _sides: 12)
            result = asyncio.run(runtime.act(GAME_ID, "I cross the damaged rail"))

            self.assertEqual(result.roll.total, 14)
            self.assertEqual(result.roll.result, "success")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["ROLL_RECEIPT"]["rolled"], 12)

    def test_runtime_supplies_complete_game_package(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy_game(root)
            seen = {}

            async def director(context):
                seen.update(context)
                return DirectorOutput(
                    outcome="clarification", event="Which equipment do you take?",
                    state_after=context["STATE"], character_after=context["CHARACTER"],
                )

            async def narrator(context):
                return NarratorOutput(narration=context["PUBLIC_EVENT"])

            runtime = Runtime(root, director, narrator, approve)
            result = asyncio.run(runtime.act(GAME_ID, "I prepare"))

            self.assertEqual(result.outcome, "clarification")
            for key in (
                "PREMISE", "SETTING", "RULES", "BOUNDARIES", "SCREAMSHEET",
                "STATE", "CHARACTER",
            ):
                self.assertTrue(seen[key])
            self.assertEqual(runtime.game(GAME_ID).turn, 0)
