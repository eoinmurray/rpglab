import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from rpglab.runtime import DirectorOutput, NarratorOutput, ReviewerOutput, Runtime


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


async def approve(_context):
    return ReviewerOutput(approved=True, target="none", reason="")


class RuntimeTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        copy_fresh_game(self.root)

    async def test_approved_turn_updates_state_snapshot_and_log(self):
        calls = []

        async def director(context):
            calls.append(context)
            state = context["STATE"].replace(
                "Someone is outside the door.", "Mrs Alder is inside the room.",
            )
            return DirectorOutput(
                outcome="resolved", event="Mrs Alder enters and asks for discreet help.",
                state_after=state,
            )

        async def narrator(context):
            return NarratorOutput(narration=context["PUBLIC_EVENT"])

        runtime = Runtime(self.root, director, narrator, approve)
        result = await runtime.act(GAME_ID, "I open the door")
        self.assertEqual(result.outcome, "resolved")
        self.assertEqual(runtime.game(GAME_ID).turn, 1)
        self.assertIn("Mrs Alder is inside", runtime.game(GAME_ID).state)
        self.assertTrue((runtime.store.path(GAME_ID) / "turns/001.md").is_file())
        self.assertIn("I open the door", (runtime.store.path(GAME_ID) / "log.md").read_text())
        self.assertEqual(calls[0]["RECENT_TURNS"], [])

    async def test_narrator_receives_only_public_context(self):
        seen = {}

        async def director(context):
            return DirectorOutput(
                outcome="resolved", event="The latch lifts.", state_after=context["STATE"],
            )

        async def narrator(context):
            seen.update(context)
            return NarratorOutput(narration="The latch lifts under your hand.")

        runtime = Runtime(self.root, director, narrator, approve)
        await runtime.act(GAME_ID, "I open the door")
        self.assertNotIn("Bea Lark", seen["PUBLIC_CONTEXT"])
        self.assertNotIn("Open possibilities", seen["PUBLIC_CONTEXT"])
        self.assertNotIn("First Primroses", seen["PUBLIC_CONTEXT"])
        self.assertNotIn("Secrets", seen["PUBLIC_CONTEXT"])
        self.assertNotIn("PROPOSED_STATE", seen)

    async def test_reviewer_receives_secrets_from_both_states(self):
        seen = {}

        async def director(context):
            return DirectorOutput(
                outcome="resolved", event="The latch lifts.", state_after=context["STATE"],
            )

        async def narrator(_context):
            return NarratorOutput(narration="The latch lifts under your hand.")

        async def reviewer(context):
            seen.update(context)
            return ReviewerOutput(approved=True, target="none", reason="")

        await Runtime(self.root, director, narrator, reviewer).act(GAME_ID, "I open the door")
        self.assertIn("Bea intends to return", seen["PREVIOUS_SECRETS"])
        self.assertEqual(seen["PREVIOUS_SECRETS"], seen["PROPOSED_SECRETS"])

    async def test_director_repair_restarts_narration(self):
        director_calls = 0
        narrator_calls = 0
        reviewer_calls = 0

        async def director(context):
            nonlocal director_calls
            director_calls += 1
            event = "The culprit confesses." if director_calls == 1 else "The latch lifts."
            if director_calls == 2:
                self.assertIn("Do not reveal", context["REVIEW_FEEDBACK"])
            return DirectorOutput(outcome="resolved", event=event, state_after=context["STATE"])

        async def narrator(context):
            nonlocal narrator_calls
            narrator_calls += 1
            return NarratorOutput(narration=context["PUBLIC_EVENT"])

        async def reviewer(_context):
            nonlocal reviewer_calls
            reviewer_calls += 1
            if reviewer_calls == 1:
                return ReviewerOutput(
                    approved=False, target="director", reason="Do not reveal the culprit yet.",
                )
            return ReviewerOutput(approved=True, target="none", reason="")

        result = await Runtime(self.root, director, narrator, reviewer).act(GAME_ID, "I open it")
        self.assertEqual(result.outcome, "resolved")
        self.assertEqual((director_calls, narrator_calls, reviewer_calls), (2, 2, 2))

    async def test_narrator_repair_reuses_director_event(self):
        director_calls = 0
        narrator_calls = 0
        reviewer_calls = 0

        async def director(context):
            nonlocal director_calls
            director_calls += 1
            return DirectorOutput(
                outcome="resolved", event="The latch lifts.", state_after=context["STATE"],
            )

        async def narrator(context):
            nonlocal narrator_calls
            narrator_calls += 1
            if narrator_calls == 2:
                self.assertIn("Remove", context["REVIEW_FEEDBACK"])
            text = "The latch lifts. 知" if narrator_calls == 1 else "The latch lifts."
            return NarratorOutput(narration=text)

        async def reviewer(_context):
            nonlocal reviewer_calls
            reviewer_calls += 1
            if reviewer_calls == 1:
                return ReviewerOutput(
                    approved=False, target="narrator", reason="Remove the stray symbol.",
                )
            return ReviewerOutput(approved=True, target="none", reason="")

        result = await Runtime(self.root, director, narrator, reviewer).act(GAME_ID, "I listen")
        self.assertEqual(result.narration, "The latch lifts.")
        self.assertEqual((director_calls, narrator_calls), (1, 2))

    async def test_double_rejection_commits_nothing(self):
        initial = Runtime(self.root).game(GAME_ID).state

        async def director(context):
            return DirectorOutput(
                outcome="resolved", event="Everything is explained.", state_after=context["STATE"],
            )

        async def narrator(_context):
            return NarratorOutput(narration="Everything is explained.")

        async def reject(_context):
            return ReviewerOutput(approved=False, target="director", reason="Too much exposition.")

        runtime = Runtime(self.root, director, narrator, reject)
        result = await runtime.act(GAME_ID, "I open the door")
        self.assertEqual(result.outcome, "processing_error")
        self.assertEqual(runtime.game(GAME_ID).turn, 0)
        self.assertEqual(runtime.game(GAME_ID).state, initial)

    async def test_question_is_an_in_world_turn(self):
        async def director(context):
            state = context["STATE"].replace(
                "Nothing beyond the current scene has been established for the player.",
                "- The visitor needs quiet help.",
            )
            return DirectorOutput(
                outcome="resolved", event="The visitor says she needs quiet help.",
                state_after=state,
            )

        async def narrator(context):
            return NarratorOutput(narration=context["PUBLIC_EVENT"])

        runtime = Runtime(self.root, director, narrator, approve)
        result = await runtime.ask(GAME_ID, "What do you need?")
        self.assertEqual(result.outcome, "resolved")
        self.assertEqual(runtime.game(GAME_ID).turn, 1)
