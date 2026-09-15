from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Game, GameStore, TurnRecord, markdown_section, public_context


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectorOutput(StrictModel):
    outcome: Literal["resolved", "clarification", "rejected"]
    event: str = Field(min_length=1, max_length=1600)
    state_after: str = Field(min_length=1, max_length=16000)


class NarratorOutput(StrictModel):
    narration: str = Field(min_length=1, max_length=700)


class ReviewerOutput(StrictModel):
    approved: bool
    target: Literal["director", "narrator", "none"]
    reason: str = Field(max_length=500)

    @model_validator(mode="after")
    def coherent(self) -> "ReviewerOutput":
        if self.approved and self.target != "none":
            raise ValueError("approved reviews must target none")
        if not self.approved and self.target == "none":
            raise ValueError("rejected reviews must identify a target")
        return self


class RuntimeResult(StrictModel):
    outcome: Literal["resolved", "clarification", "rejected", "processing_error"]
    narration: str
    finished: bool = False
    duration_ms: int = 0
    tokens: int = 0


Agent = Callable[[dict], Awaitable[BaseModel]]


@dataclass
class TurnMetrics:
    tokens: int = 0


DIRECTOR_INSTRUCTIONS = """
You direct a concise, player-led roleplaying game represented by Markdown prose.
Given the complete current state, recent turns, and player input, return one concrete
event and a complete replacement state document. The state may evolve freely.

Be demanding but fair. Test the player through consequences, trade-offs, and
characters who pursue their own interests. Reward intelligent or imaginative actions.
Never create difficulty by withholding earned information, inventing arbitrary
obstacles, moving the goalposts, or negating a genuine success. In a cosy game,
pressure should remain social, emotional, practical, or gently comic rather than
punitive or threatening.

Advance one foreground arc through what happens now. You may also move one background
character, tension, or arc when causally appropriate. Characters pursue their own
wants. Locations are prose, not a map. Open possibilities are undecided options, not
secret facts; commit to an answer only when play establishes it.

The Secrets section is private causal memory. Carry it forward in state_after. Never
expose a secret merely because you can see it. Preserve established past events; only
the current hidden situation and revelation status may evolve. When the player's
present action genuinely exposes part of a secret, make that discovery the turn's
public event, add it to Established facts, and mark its revelation status rather than
deleting the underlying history.

Do not write final narration. Do not summarize the setting, dump backstory, expose
private possibilities, repeat facts the player already knows, or answer more than the
player asked. Introduce at most one significant revelation per turn. Never choose the
player's beliefs, dialogue, destination, bargains, or commitments beyond their input.
Keep the event under 100 words and preserve the state's useful Markdown headings.
For clarification or rejection, leave state_after unchanged.

If REVIEW_FEEDBACK is supplied, replace the rejected proposal and address only that
specific problem.
""".strip()


NARRATOR_INSTRUCTIONS = """
Turn the approved public event into 15-55 words of natural, restrained game prose.
Respond directly to the player's input. Use only the supplied public context and
event. Do not add discoveries, explanations, people, motives, destinations, choices,
or setting facts. Do not recap. Prefer one concrete action or exchange over exposition.
For clarification, ask one short in-world question. Return clean English prose only.

Be on the player's side in presentation, not strategy. Make the player's action feel
concrete and consequential. Show clearly what they perceive, but never supply answers,
hints, decisions, or discoveries absent from the approved event.

Write lean, paratactic prose. Use short declarative sentences, usually with one action
or image in each. Prefer concrete nouns and active verbs. Use few adjectives and
adverbs. Carry emotion through action and dialogue rather than explaining motives.
Place dialogue among physical actions. Avoid ornate metaphors, lore summaries,
atmospheric padding, and long or heavily subordinate sentences.

Style reference: Mira came in from the rain. Water ran from her coat onto the floor.
She shut the door. "The gun is gone," she said.

If REVIEW_FEEDBACK is supplied, rewrite the narration to fix that exact problem.
""".strip()


REVIEWER_INSTRUCTIONS = """
You are the final quality gate for a concise roleplaying turn. Review the previous
complete state, proposed complete state, Director event, and player-facing narration.
Do not rewrite either output. Approve only when the turn follows from the player's
input, preserves continuity and player agency, exposes no private or unresolved
possibility as fact, introduces no unsupported knowledge, contains at most one major
revelation, avoids repetition and lore-dumping, and leaves an interesting situation.
The narration must contain nothing beyond the public event and previously established
public context, and must contain clean prose without stray symbols.

Be suspicious but surgical. Search aggressively for leaks and contradictions, but
reject only when you can identify the exact secret, unsupported statement, agency
violation, continuity error, or clear prose-rule failure. For Narrator style, reject
ornate, explanatory, repetitive, padded, or syntactically tangled prose. Name one
specific problem and request the smallest sufficient repair; do not demand a general
rewrite when a local correction will work.

Treat Secrets as private canonical memory. Reject if the Director drops or contradicts
established secret history. Reject any event or narration that leaks, confirms, or
strongly implies a secret not learned through the player's present action. When a
secret is legitimately discovered, require the proposed state to add the public
knowledge to Established facts and update its revelation status without erasing the
original secret history.

On rejection, target director for event/state problems or narrator for prose-only
problems, and give one precise repair instruction. Otherwise target none.
""".strip()


class Runtime:
    def __init__(
        self, root: Path, director: Optional[Agent] = None,
        narrator: Optional[Agent] = None, reviewer: Optional[Agent] = None,
    ):
        self.store = GameStore(root)
        self.director = director
        self.narrator = narrator
        self.reviewer = reviewer

    def games(self) -> list[str]:
        return self.store.list_games()

    def game(self, game_id: str) -> Game:
        return self.store.load(game_id)

    def opening(self, game_id: str) -> str:
        return self.game(game_id).opening

    def scene(self, game_id: str) -> str:
        return markdown_section(self.game(game_id).state, "Current scene") or "The scene is unclear."

    def recap(self, game_id: str, limit: int = 6) -> str:
        turns = self.store.recent_turns(game_id, limit)
        return "\n\n".join(
            f"**Anomancer:** {turn['player_input']}\n\n{turn['narration']}" for turn in turns
        ) or "Nothing has happened yet."

    def bind(self, game_id: str, interface: Literal["cli", "discord"], channel_id=None) -> None:
        self.store.bind(game_id, interface, channel_id)

    def reset(self, game_id: str) -> None:
        with self.store.lock(game_id):
            self.store.reset(game_id)

    def game_for_channel(self, channel_id: int) -> Optional[str]:
        return self.store.game_for_channel(channel_id)

    async def act(self, game_id: str, text: str, user_id: str = "local") -> RuntimeResult:
        return await self._process(game_id, text, user_id, "action")

    async def ask(self, game_id: str, text: str, user_id: str = "local") -> RuntimeResult:
        return await self._process(game_id, text, user_id, "question")

    async def _process(
        self, game_id: str, text: str, user_id: str, input_kind: str,
    ) -> RuntimeResult:
        text = text.strip()
        if not text:
            return RuntimeResult(outcome="rejected", narration="That input is empty.")
        game = self.game(game_id)
        if game.finished:
            return RuntimeResult(outcome="rejected", narration="This story has ended.", finished=True)
        trace_id, started, metrics = str(uuid.uuid4()), time.monotonic(), TurnMetrics()
        proposal = None
        director_feedback = None
        narrator_feedback = None
        try:
            for _attempt in range(2):
                if proposal is None:
                    proposal, used = await self._direct(
                        game, text, input_kind, user_id, trace_id, director_feedback,
                    )
                    metrics.tokens += used
                    if proposal.outcome != "resolved":
                        proposal.state_after = game.state
                narration, used = await self._narrate(
                    game, proposal, text, input_kind, user_id, trace_id, narrator_feedback,
                )
                metrics.tokens += used
                review, used = await self._review(
                    game, proposal, narration, text, input_kind, user_id, trace_id,
                )
                metrics.tokens += used
                if review.approved:
                    return self._commit(
                        game, proposal, narration, text, input_kind,
                        trace_id, started, metrics,
                    )
                self.store.append_diagnostic(
                    game_id, trace_id=trace_id, stage="reviewer", status="repair",
                    target=review.target, reason=review.reason,
                )
                if review.target == "narrator":
                    narrator_feedback = review.reason
                else:
                    director_feedback, narrator_feedback, proposal = review.reason, None, None
            raise RuntimeError("reviewer rejected both attempts")
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.store.append_diagnostic(
                game_id, trace_id=trace_id, stage="turn", status="error",
                duration_ms=duration_ms, tokens=metrics.tokens, input=text,
                exception_type=type(exc).__name__, exception=str(exc),
                traceback=traceback.format_exc(),
            )
            return RuntimeResult(
                outcome="processing_error",
                narration="That turn could not be completed. The story has not advanced.",
                duration_ms=duration_ms, tokens=metrics.tokens,
            )

    def _commit(
        self, game: Game, proposal: DirectorOutput, narration: str, text: str,
        input_kind: str, trace_id: str, started: float, metrics: TurnMetrics,
    ) -> RuntimeResult:
        record = TurnRecord(
            trace_id=trace_id, number=game.turn + 1, input_kind=input_kind,
            player_input=text, event=proposal.event, narration=narration,
            state_after=proposal.state_after,
        )
        with self.store.lock(game.game_id):
            if self.game(game.game_id).turn != game.turn:
                raise RuntimeError("game changed while the turn was being processed")
            self.store.append_turn(game.game_id, record)
        duration_ms = round((time.monotonic() - started) * 1000)
        finished = markdown_section(proposal.state_after, "Status").casefold().startswith("concluded")
        self.store.append_diagnostic(
            game.game_id, trace_id=trace_id, stage="turn", status="completed",
            duration_ms=duration_ms, tokens=metrics.tokens, turn=record.number,
            outcome=proposal.outcome,
        )
        return RuntimeResult(
            outcome=proposal.outcome, narration=narration, finished=finished,
            duration_ms=duration_ms, tokens=metrics.tokens,
        )

    async def _direct(
        self, game: Game, text: str, input_kind: str, user_id: str,
        trace_id: str, feedback: Optional[str],
    ) -> tuple[DirectorOutput, int]:
        context = {
            "STATE": game.state,
            "RECENT_TURNS": self.store.recent_turns(game.game_id),
            "INPUT_KIND": input_kind,
            "PLAYER_INPUT": text,
        }
        if feedback:
            context["REVIEW_FEEDBACK"] = feedback
        if self.director is not None:
            return DirectorOutput.model_validate(await self.director(context)), 0
        return await self._model_call(
            game.game_id, trace_id, "director", DIRECTOR_INSTRUCTIONS,
            context, DirectorOutput, 3000, user_id,
        )

    async def _narrate(
        self, game: Game, proposal: DirectorOutput, text: str, input_kind: str,
        user_id: str, trace_id: str, feedback: Optional[str],
    ) -> tuple[str, int]:
        context = {
            "PLAYER_INPUT": text, "INPUT_KIND": input_kind,
            "OUTCOME": proposal.outcome, "PUBLIC_CONTEXT": public_context(game.state),
            "PUBLIC_EVENT": proposal.event,
        }
        if feedback:
            context["REVIEW_FEEDBACK"] = feedback
        if self.narrator is not None:
            output = NarratorOutput.model_validate(await self.narrator(context))
            return output.narration, 0
        output, tokens = await self._model_call(
            game.game_id, trace_id, "narrator", NARRATOR_INSTRUCTIONS,
            context, NarratorOutput, 500, user_id,
        )
        return output.narration, tokens

    async def _review(
        self, game: Game, proposal: DirectorOutput, narration: str, text: str,
        input_kind: str, user_id: str, trace_id: str,
    ) -> tuple[ReviewerOutput, int]:
        context = {
            "PLAYER_INPUT": text, "INPUT_KIND": input_kind,
            "PREVIOUS_STATE": game.state, "PROPOSED_STATE": proposal.state_after,
            "PREVIOUS_SECRETS": markdown_section(game.state, "Secrets"),
            "PROPOSED_SECRETS": markdown_section(proposal.state_after, "Secrets"),
            "DIRECTOR_EVENT": proposal.event, "NARRATION": narration,
        }
        if self.reviewer is not None:
            return ReviewerOutput.model_validate(await self.reviewer(context)), 0
        return await self._model_call(
            game.game_id, trace_id, "reviewer", REVIEWER_INSTRUCTIONS,
            context, ReviewerOutput, 600, user_id,
        )

    async def _model_call(
        self, game_id: str, trace_id: str, role: str, instructions: str,
        context: dict, output_type: type[BaseModel], max_tokens: int, user_id: str,
    ) -> tuple[BaseModel, int]:
        model = os.environ.get(
            f"OPENAI_{role.upper()}_MODEL",
            os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        )
        prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        self.store.append_diagnostic(
            game_id, trace_id=trace_id, stage=role, status="request",
            model=model, instructions=instructions, prompt=prompt,
        )
        effort = os.environ.get(f"OPENAI_{role.upper()}_REASONING_EFFORT", "low")
        async with AsyncOpenAI() as client:
            response = await client.responses.parse(
                model=model, reasoning={"effort": effort}, instructions=instructions,
                input=prompt, text_format=output_type, max_output_tokens=max_tokens,
                safety_identifier=_safety_id(user_id),
            )
        if response.output_parsed is None:
            raise RuntimeError(f"the {role} returned no structured result")
        usage = response.usage.model_dump(mode="json") if response.usage else None
        self.store.append_diagnostic(
            game_id, trace_id=trace_id, stage=role, status="response",
            response_id=response.id, usage=usage,
            parsed_output=response.output_parsed.model_dump(mode="json"),
        )
        return response.output_parsed, response.usage.total_tokens if response.usage else 0


def _safety_id(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:64]
