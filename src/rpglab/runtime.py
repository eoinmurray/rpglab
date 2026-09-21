from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import traceback
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import (
    DiceCheck, Game, GameStore, RollReceipt, TurnRecord, encounter_section,
    markdown_section, markdown_title, public_context,
)
from .prompts import DIRECTOR_INSTRUCTIONS, NARRATOR_INSTRUCTIONS, REVIEWER_INSTRUCTIONS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectorOutput(StrictModel):
    outcome: Literal["resolved", "clarification", "rejected", "check"]
    event: str = Field(min_length=1, max_length=1800)
    state_after: str = Field(min_length=1, max_length=24000)
    character_after: str = Field(min_length=1, max_length=16000)
    check: Optional[DiceCheck] = None

    @model_validator(mode="after")
    def coherent(self) -> "DirectorOutput":
        if (self.outcome == "check") != (self.check is not None):
            raise ValueError("only check outcomes require check details")
        return self


class NarratorOutput(StrictModel):
    narration: str = Field(min_length=1, max_length=1200)


class ReviewerOutput(StrictModel):
    approved: bool
    target: Literal["director", "narrator", "none"]
    reason: str = Field(default="", max_length=600)

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
    roll: Optional[RollReceipt] = None
    encounter: Optional[str] = None

    def display(self) -> str:
        parts = [self.roll.badge()] if self.roll else []
        parts.append(self.narration)
        if self.encounter and self.encounter.casefold() != "none.":
            parts.append(f"### Active encounters\n\n{self.encounter}")
        return "\n\n".join(parts)


Agent = Callable[[dict], Awaitable[BaseModel]]
Roller = Callable[[int], int]


class Runtime:
    def __init__(
        self, root: Path, director: Optional[Agent] = None,
        narrator: Optional[Agent] = None, reviewer: Optional[Agent] = None,
        roller: Optional[Roller] = None,
    ):
        self.store = GameStore(root)
        self.director = director
        self.narrator = narrator
        self.reviewer = reviewer
        self.roller = roller or (lambda sides: secrets.randbelow(sides) + 1)
        self._turn_locks: dict[str, asyncio.Lock] = {}

    def games(self) -> list[str]:
        return self.store.list_games()

    def game(self, game_id: str) -> Game:
        return self.store.load(game_id)

    def character(self, game_id: str, value: Optional[str] = None) -> str:
        return self.store.character_id(game_id, value)

    def opening(self, game_id: str, character: Optional[str] = None) -> str:
        return self.store.opening(game_id, character)

    def scene(self, game_id: str, character: Optional[str] = None) -> str:
        game = self.game(game_id)
        character_id = self.store.character_id(game_id, character)
        return public_context(game.state, game.characters[character_id])

    def recap(self, game_id: str, limit: int = 6) -> str:
        turns = self.store.recent_turns(game_id, limit)
        return "\n\n".join(
            f"**{turn['actor']}:** {turn['player_input']}\n\n"
            f"{turn['roll'] + chr(10) + chr(10) if turn['roll'] else ''}"
            f"{turn['narration']}" for turn in turns
        ) or "Nothing has happened yet."

    def bind(
        self, game_id: str, interface: Literal["cli", "discord"],
        channel_id: Optional[int] = None, owner_user_id: Optional[str] = None,
        character: Optional[str] = None,
    ) -> str:
        return self.store.bind(game_id, interface, channel_id, owner_user_id, character)

    def channel_context(self, channel_id: int) -> Optional[dict[str, str]]:
        return self.store.channel_context(channel_id)

    def actor_for_user(self, game_id: str, channel_id: int, user_id: str) -> str:
        return self.store.actor_for_user(game_id, channel_id, user_id)

    def reset(self, game_id: str) -> None:
        with self.store.lock(game_id):
            self.store.reset(game_id)

    async def act(
        self, game_id: str, text: str, user_id: str = "local",
        actor: Optional[str] = None,
    ) -> RuntimeResult:
        async with self._turn_locks.setdefault(game_id, asyncio.Lock()):
            return await self._process(game_id, text, user_id, actor, "action")

    async def ask(
        self, game_id: str, text: str, user_id: str = "local",
        actor: Optional[str] = None,
    ) -> RuntimeResult:
        async with self._turn_locks.setdefault(game_id, asyncio.Lock()):
            return await self._process(game_id, text, user_id, actor, "question")

    async def _process(
        self, game_id: str, text: str, user_id: str,
        actor_value: Optional[str], input_kind: str,
    ) -> RuntimeResult:
        text = text.strip()
        if not text:
            return RuntimeResult(outcome="rejected", narration="That input is empty.")
        game = self.game(game_id)
        if game.finished:
            return RuntimeResult(
                outcome="rejected", narration="This adventure has concluded.", finished=True,
            )
        character_id = self.store.character_id(game_id, actor_value)
        character = game.characters[character_id]
        actor = markdown_title(character)
        trace_id, started, tokens = str(uuid.uuid4()), time.monotonic(), 0
        proposal = None
        locked_check = None
        roll = None
        director_feedback = None
        narrator_feedback = None
        director_repairs = narrator_repairs = 0
        try:
            for _attempt in range(3):
                if proposal is None:
                    proposal, used = await self._direct(
                        game, character, text, input_kind, user_id, trace_id,
                        director_feedback, locked_check, roll,
                    )
                    tokens += used
                    if proposal.outcome == "check":
                        locked_check = proposal.check
                        roll = self._roll(game_id, trace_id, locked_check)
                        proposal, used = await self._direct(
                            game, character, text, input_kind, user_id, trace_id,
                            None, locked_check, roll,
                        )
                        tokens += used
                        if proposal.outcome != "resolved":
                            raise RuntimeError("the Director did not resolve the locked check")
                    if proposal.outcome != "resolved":
                        proposal.state_after = game.state
                        proposal.character_after = character
                narration, used = await self._narrate(
                    game, character, proposal, text, input_kind, user_id,
                    trace_id, narrator_feedback,
                )
                tokens += used
                review, used = await self._review(
                    game, character, proposal, narration, text, input_kind,
                    user_id, trace_id, locked_check, roll,
                )
                tokens += used
                if review.approved:
                    if proposal.outcome != "resolved":
                        return RuntimeResult(
                            outcome=proposal.outcome, narration=narration,
                            duration_ms=round((time.monotonic() - started) * 1000),
                            tokens=tokens, roll=roll,
                        )
                    return self._commit(
                        game, proposal, narration, text, input_kind, actor,
                        trace_id, started, tokens, roll,
                    )
                self.store.append_diagnostic(
                    game_id, trace_id=trace_id, stage="reviewer", status="repair",
                    target=review.target, reason=review.reason,
                )
                if review.target == "narrator":
                    if narrator_repairs:
                        break
                    narrator_repairs += 1
                    narrator_feedback = review.reason
                else:
                    if director_repairs:
                        break
                    director_repairs += 1
                    director_feedback, narrator_feedback, proposal = review.reason, None, None
            raise RuntimeError("reviewer exhausted the repair budget")
        except Exception as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            self.store.append_diagnostic(
                game_id, trace_id=trace_id, stage="turn", status="error",
                duration_ms=duration_ms, tokens=tokens, input=text, actor=actor,
                exception_type=type(exc).__name__, exception=str(exc),
                traceback=traceback.format_exc(),
            )
            return RuntimeResult(
                outcome="processing_error",
                narration="That turn could not be completed. The game has not advanced.",
                duration_ms=duration_ms, tokens=tokens,
            )

    def _commit(
        self, game: Game, proposal: DirectorOutput, narration: str,
        text: str, input_kind: str, actor: str, trace_id: str,
        started: float, tokens: int, roll: Optional[RollReceipt],
    ) -> RuntimeResult:
        record = TurnRecord(
            trace_id=trace_id, number=game.turn + 1, actor=actor,
            input_kind=input_kind, player_input=text, event=proposal.event,
            narration=narration, character_after=proposal.character_after,
            state_after=proposal.state_after, roll=roll,
        )
        with self.store.lock(game.game_id):
            if self.game(game.game_id).turn != game.turn:
                raise RuntimeError("game changed while the turn was being processed")
            self.store.append_turn(game.game_id, record)
        duration_ms = round((time.monotonic() - started) * 1000)
        finished = markdown_section(proposal.state_after, "Status").casefold().startswith(
            "concluded"
        )
        self.store.append_diagnostic(
            game.game_id, trace_id=trace_id, stage="turn", status="completed",
            duration_ms=duration_ms, tokens=tokens, turn=record.number,
            outcome=proposal.outcome, actor=actor,
            roll=roll.model_dump(mode="json") if roll else None,
        )
        return RuntimeResult(
            outcome="resolved", narration=narration, finished=finished,
            duration_ms=duration_ms, tokens=tokens, roll=roll,
            encounter=encounter_section(proposal.state_after) or None,
        )

    async def _direct(
        self, game: Game, character: str, text: str, input_kind: str,
        user_id: str, trace_id: str, feedback: Optional[str],
        locked_check: Optional[DiceCheck], roll: Optional[RollReceipt],
    ) -> tuple[DirectorOutput, int]:
        context = {
            "PREMISE": game.premise, "SETTING": game.setting, "RULES": game.rules,
            "BOUNDARIES": game.boundaries, "SCREAMSHEET": game.screamsheet,
            "STATE": game.state, "CHARACTER": character,
            "RECENT_TURNS": self.store.recent_turns(game.game_id),
            "INPUT_KIND": input_kind, "PLAYER_INPUT": text,
        }
        if feedback:
            context["REVIEW_FEEDBACK"] = feedback
        if locked_check and roll:
            context["LOCKED_CHECK"] = locked_check.model_dump(mode="json")
            context["ROLL_RECEIPT"] = roll.model_dump(mode="json")
        if self.director:
            return DirectorOutput.model_validate(await self.director(context)), 0
        return await self._model_call(
            game.game_id, trace_id, "director", DIRECTOR_INSTRUCTIONS,
            context, DirectorOutput, 5000, user_id,
        )

    async def _narrate(
        self, game: Game, character: str, proposal: DirectorOutput,
        text: str, input_kind: str, user_id: str, trace_id: str,
        feedback: Optional[str],
    ) -> tuple[str, int]:
        context = {
            "PLAYER_INPUT": text, "INPUT_KIND": input_kind,
            "OUTCOME": proposal.outcome,
            "PUBLIC_CONTEXT": public_context(game.state, character),
            "PUBLIC_EVENT": proposal.event,
        }
        if feedback:
            context["REVIEW_FEEDBACK"] = feedback
        if self.narrator:
            output = NarratorOutput.model_validate(await self.narrator(context))
            return output.narration, 0
        output, tokens = await self._model_call(
            game.game_id, trace_id, "narrator", NARRATOR_INSTRUCTIONS,
            context, NarratorOutput, 800, user_id,
        )
        return output.narration, tokens

    async def _review(
        self, game: Game, character: str, proposal: DirectorOutput,
        narration: str, text: str, input_kind: str, user_id: str,
        trace_id: str, locked_check: Optional[DiceCheck],
        roll: Optional[RollReceipt],
    ) -> tuple[ReviewerOutput, int]:
        context = {
            "PREMISE": game.premise, "RULES": game.rules,
            "BOUNDARIES": game.boundaries, "SCREAMSHEET": game.screamsheet,
            "PLAYER_INPUT": text, "INPUT_KIND": input_kind,
            "PREVIOUS_STATE": game.state, "PROPOSED_STATE": proposal.state_after,
            "PREVIOUS_CHARACTER": character,
            "PROPOSED_CHARACTER": proposal.character_after,
            "DIRECTOR_EVENT": proposal.event, "NARRATION": narration,
        }
        if locked_check and roll:
            context["LOCKED_CHECK"] = locked_check.model_dump(mode="json")
            context["ROLL_RECEIPT"] = roll.model_dump(mode="json")
        if self.reviewer:
            return ReviewerOutput.model_validate(await self.reviewer(context)), 0
        return await self._model_call(
            game.game_id, trace_id, "reviewer", REVIEWER_INSTRUCTIONS,
            context, ReviewerOutput, 900, user_id,
        )

    def _roll(self, game_id: str, trace_id: str, check: DiceCheck) -> RollReceipt:
        rolled = self.roller(20)
        if not isinstance(rolled, int) or isinstance(rolled, bool) or not 1 <= rolled <= 20:
            raise ValueError("roller must return an integer from 1 to 20")
        total = rolled + check.modifier
        if rolled == 1:
            result = "critical_failure"
        elif rolled == 20:
            result = "critical_success"
        elif total < check.difficulty:
            result = "failure"
        elif total >= check.difficulty + 5:
            result = "strong_success"
        else:
            result = "success"
        receipt = RollReceipt(
            rolled=rolled, modifier=check.modifier, total=total,
            difficulty=check.difficulty, result=result,
        )
        self.store.append_diagnostic(
            game_id, trace_id=trace_id, stage="dice", status="rolled",
            check=check.model_dump(mode="json"), receipt=receipt.model_dump(mode="json"),
        )
        return receipt

    async def _model_call(
        self, game_id: str, trace_id: str, role: str, instructions: str,
        context: dict, output_type: type[BaseModel], max_tokens: int, user_id: str,
    ) -> tuple[BaseModel, int]:
        model = os.environ.get(
            f"OPENAI_{role.upper()}_MODEL", os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        )
        prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        self.store.append_diagnostic(
            game_id, trace_id=trace_id, stage=role, status="request",
            model=model, instructions=instructions, prompt=prompt,
        )
        effort = os.environ.get(f"OPENAI_{role.upper()}_REASONING_EFFORT", "low")
        async with AsyncOpenAI() as client:
            for attempt in range(2):
                try:
                    response = await client.responses.parse(
                        model=model, reasoning={"effort": effort}, instructions=instructions,
                        input=prompt, text_format=output_type, max_output_tokens=max_tokens,
                        safety_identifier=_safety_id(user_id),
                    )
                    break
                except ValidationError as exc:
                    if attempt or not _is_invalid_json(exc):
                        raise
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


def _is_invalid_json(exc: ValidationError) -> bool:
    return any(error.get("type") == "json_invalid" for error in exc.errors())
