from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GAME_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
TURN_FILE = re.compile(r"^(\d{3,})\.md$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def markdown_section(document: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(document)
    return match.group(1).strip() if match else ""


def markdown_title(document: str) -> str:
    match = re.search(r"^# (.+?)\s*$", document, re.MULTILINE)
    return match.group(1).strip() if match else "Untitled game"


def encounter_section(state: str) -> str:
    return markdown_section(state, "Active encounters")


def active_screamsheet_id(state: str) -> str:
    value = markdown_section(state, "Active Screamsheet")
    match = re.search(r"`([^`]+)`", value)
    return match.group(1) if match else value.partition(" ")[0].strip()


def public_context(state: str, character: str) -> str:
    parts = []
    scene = markdown_section(character, "Current scene")
    knowledge = markdown_section(character, "Private knowledge")
    if scene:
        parts.append(f"## Current scene\n\n{scene}")
    for heading in ("World pressure", "Established facts", "Active encounters"):
        value = markdown_section(state, heading)
        if value:
            parts.append(f"## {heading}\n\n{value}")
    if knowledge:
        parts.append(f"## Private knowledge\n\n{knowledge}")
    return "\n\n".join(parts)


class Game(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_id: str
    title: str
    premise: str
    setting: str
    rules: str
    boundaries: str
    screamsheet_id: str
    screamsheet: str
    state: str
    characters: dict[str, str]
    turn: int = Field(ge=0)

    @property
    def finished(self) -> bool:
        return markdown_section(self.state, "Status").casefold().startswith("concluded")

    @property
    def default_character(self) -> str:
        if not self.characters:
            raise ValueError("game has no characters")
        return next(iter(self.characters))


class DiceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=240)
    difficulty: Literal[10, 14, 18]
    modifier: int = Field(ge=-2, le=2)
    success_stakes: str = Field(min_length=1, max_length=300)
    failure_stakes: str = Field(min_length=1, max_length=300)


class RollReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    die: Literal["d20"] = "d20"
    rolled: int = Field(ge=1, le=20)
    modifier: int = Field(ge=-2, le=2)
    total: int = Field(ge=-1, le=22)
    difficulty: Literal[10, 14, 18]
    result: Literal[
        "critical_failure", "failure", "success", "strong_success", "critical_success",
    ]

    def badge(self) -> str:
        sign = f" + {self.modifier}" if self.modifier > 0 else ""
        if self.modifier < 0:
            sign = f" − {abs(self.modifier)}"
        label = self.result.replace("_", " ").upper()
        return f"🎲 **d20: {self.rolled}{sign} = {self.total} vs {self.difficulty} — {label}**"


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str
    number: int = Field(ge=1)
    actor: str
    input_kind: Literal["action", "question"]
    player_input: str
    event: str
    narration: str
    character_after: str
    state_after: str
    roll: Optional[RollReceipt] = None
    timestamp: str = Field(default_factory=now)

    def markdown(self) -> str:
        roll = f"## Roll\n\n{self.roll.badge()}\n\n" if self.roll else ""
        return (
            f"# Turn {self.number:03d}\n\nTrace: `{self.trace_id}`\n\n"
            f"Timestamp: `{self.timestamp}`\n\n## Actor\n\n{self.actor}\n\n"
            f"## Player input\n\n{self.player_input}\n\n## Event\n\n{self.event}\n\n"
            f"{roll}## Narration\n\n{self.narration}\n\n"
            f"## Character after\n\n{self.character_after.strip()}\n\n"
            f"## State after\n\n{self.state_after.strip()}\n"
        )


class GameStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.games = self.root / "games"
        self.locks = self.root / ".rpglab-locks"
        self.bindings_file = self.root / ".rpglab-bindings.json"

    def path(self, game_id: str) -> Path:
        if not GAME_ID.fullmatch(game_id):
            raise ValueError("invalid game ID")
        return self.games / game_id

    def list_games(self) -> list[str]:
        if not self.games.is_dir():
            return []
        required = ("premise.md", "setting.md", "rules.md", "boundaries.md", "state.md")
        return sorted(
            path.name for path in self.games.iterdir()
            if path.is_dir() and GAME_ID.fullmatch(path.name)
            and all((path / name).is_file() for name in required)
            and (path / "characters").is_dir() and (path / "screamsheets").is_dir()
        )

    def turn_paths(self, game_id: str) -> list[Path]:
        directory = self.path(game_id) / "turns"
        if not directory.is_dir():
            return []
        return sorted(
            path for path in directory.iterdir()
            if TURN_FILE.fullmatch(path.name) and path.name != "000.md"
        )

    def load(self, game_id: str) -> Game:
        path = self.path(game_id)
        if game_id not in self.list_games():
            raise FileNotFoundError(game_id)

        def read(name: str) -> str:
            return (path / name).read_text(encoding="utf-8").strip() + "\n"

        state = read("state.md")
        characters = {
            item.stem: item.read_text(encoding="utf-8").strip() + "\n"
            for item in sorted((path / "characters").glob("*.md"))
        }
        if not characters:
            raise ValueError("game requires at least one character")
        screamsheet_id = active_screamsheet_id(state)
        screamsheet_path = path / "screamsheets" / f"{screamsheet_id}.md"
        if not screamsheet_path.is_file():
            raise ValueError(f"active Screamsheet not found: {screamsheet_id}")
        premise = read("premise.md")
        return Game(
            game_id=game_id, title=markdown_title(premise), premise=premise,
            setting=read("setting.md"), rules=read("rules.md"),
            boundaries=read("boundaries.md"), screamsheet_id=screamsheet_id,
            screamsheet=screamsheet_path.read_text(encoding="utf-8").strip() + "\n",
            state=state, characters=characters, turn=len(self.turn_paths(game_id)),
        )

    def character_id(self, game_id: str, value: Optional[str] = None) -> str:
        game = self.load(game_id)
        if value:
            folded = value.casefold()
            for character_id, document in game.characters.items():
                if folded in {character_id.casefold(), markdown_title(document).casefold()}:
                    return character_id
            raise ValueError(f"unknown character: {value}")
        if len(game.characters) != 1:
            raise ValueError("choose a character")
        return game.default_character

    def opening(self, game_id: str, character_id: Optional[str] = None) -> str:
        game = self.load(game_id)
        character = game.characters[self.character_id(game_id, character_id)]
        values = (
            markdown_section(game.screamsheet, "Player pitch"),
            markdown_section(game.screamsheet, "Briefing"),
            markdown_section(character, "Current scene"),
        )
        return "\n\n".join(value for value in values if value)

    def recent_turns(self, game_id: str, limit: int = 6) -> list[dict[str, str]]:
        values = []
        for path in self.turn_paths(game_id)[-limit:]:
            document = path.read_text(encoding="utf-8")
            values.append({
                "actor": markdown_section(document, "Actor"),
                "player_input": markdown_section(document, "Player input"),
                "roll": markdown_section(document, "Roll"),
                "narration": markdown_section(document, "Narration"),
            })
        return values

    def append_turn(self, game_id: str, record: TurnRecord) -> None:
        game = self.load(game_id)
        if record.number != game.turn + 1:
            raise ValueError("turn numbers must be consecutive")
        character_id = self.character_id(game_id, record.actor)
        turn_path = self.path(game_id) / "turns" / f"{record.number:03d}.md"
        if turn_path.exists():
            raise ValueError("turn already exists")
        _atomic_write(turn_path, record.markdown())
        _atomic_write(self.path(game_id) / "state.md", record.state_after.strip() + "\n")
        _atomic_write(
            self.path(game_id) / "characters" / f"{character_id}.md",
            record.character_after.strip() + "\n",
        )
        roll = f"{record.roll.badge()}\n\n" if record.roll else ""
        _append_text(
            self.path(game_id) / "log.md",
            f"\n## Turn {record.number}\n\n**{record.actor}:** {record.player_input}\n\n"
            f"{roll}{record.narration}\n",
        )

    def state_at(self, game_id: str, turn: int) -> str:
        path = self.path(game_id) / "turns" / f"{turn:03d}.md"
        if not path.is_file():
            raise FileNotFoundError(path)
        document = path.read_text(encoding="utf-8")
        state = document.partition("## State after\n")[2]
        if not state:
            raise ValueError(f"{path.name} has no State after snapshot")
        return state.strip() + "\n"

    def reset(self, game_id: str) -> None:
        initial = (self.path(game_id) / "turns" / "000.md").read_text(encoding="utf-8")
        character_part, marker, state = initial.partition("## State after\n")
        character = character_part.partition("## Character after\n")[2]
        if not marker or not character.strip() or not state.strip():
            raise ValueError("turns/000.md requires Character after and State after")
        character_id = self.character_id(game_id)
        for path in self.turn_paths(game_id):
            path.unlink()
        _atomic_write(self.path(game_id) / "state.md", state.strip() + "\n")
        _atomic_write(
            self.path(game_id) / "characters" / f"{character_id}.md",
            character.strip() + "\n",
        )
        _atomic_write(self.path(game_id) / "log.md", "# Transcript\n")
        _atomic_write(self.path(game_id) / "diagnostics.jsonl", "")

    def append_diagnostic(self, game_id: str, **value: Any) -> None:
        _append_text(
            self.path(game_id) / "diagnostics.jsonl",
            json.dumps({"timestamp": now(), **value}, ensure_ascii=False) + "\n",
        )

    def bind(
        self, game_id: str, interface: str, channel_id: Optional[int] = None,
        owner_user_id: Optional[str] = None, character: Optional[str] = None,
    ) -> str:
        character_id = self.character_id(game_id, character)
        bindings = self._bindings()
        bindings[game_id] = {
            "interface": interface, "channel_id": channel_id,
            "owner_user_id": owner_user_id, "character": character_id,
        }
        _atomic_write(self.bindings_file, json.dumps(bindings, indent=2) + "\n")
        return character_id

    def channel_context(self, channel_id: int) -> Optional[dict[str, str]]:
        for game_id, binding in self._bindings().items():
            if binding.get("channel_id") == channel_id and game_id in self.list_games():
                return {"game_id": game_id, "character": binding.get("character", "")}
        return None

    def actor_for_user(self, game_id: str, channel_id: int, user_id: str) -> str:
        binding = self._bindings().get(game_id, {})
        if binding.get("channel_id") != channel_id:
            raise ValueError("This channel is not bound to that game.")
        if binding.get("owner_user_id") not in {None, str(user_id)}:
            raise ValueError("Only the player who started this game may act here.")
        return self.character_id(game_id, binding.get("character"))

    def _bindings(self) -> dict[str, dict[str, Any]]:
        if not self.bindings_file.is_file():
            return {}
        try:
            value = json.loads(self.bindings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    @contextmanager
    def lock(self, game_id: str) -> Iterator[None]:
        self.locks.mkdir(parents=True, exist_ok=True)
        with (self.locks / f"{game_id}.lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
