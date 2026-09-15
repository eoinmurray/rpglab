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


def public_context(state: str) -> str:
    parts = []
    for heading in ("Current scene", "Established facts"):
        content = markdown_section(state, heading)
        if content:
            parts.append(f"## {heading}\n\n{content}")
    return "\n\n".join(parts)


class Game(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: str
    title: str
    opening: str
    initial_state: str
    state: str
    turn: int = Field(ge=0)

    @property
    def finished(self) -> bool:
        return markdown_section(self.state, "Status").casefold().startswith("concluded")


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    number: int = Field(ge=1)
    input_kind: Literal["action", "question"]
    player_input: str
    event: str
    narration: str
    state_after: str
    timestamp: str = Field(default_factory=now)

    def markdown(self) -> str:
        return (
            f"# Turn {self.number:03d}\n\n"
            f"Trace: `{self.trace_id}`\n\nTimestamp: `{self.timestamp}`\n\n"
            f"## Player input\n\n{self.player_input.strip()}\n\n"
            f"## Event\n\n{self.event.strip()}\n\n"
            f"## Narration\n\n{self.narration.strip()}\n\n"
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
        return sorted(
            path.name for path in self.games.iterdir()
            if path.is_dir() and GAME_ID.fullmatch(path.name)
            and (path / "state-init.md").is_file() and (path / "state.md").is_file()
        )

    def turn_paths(self, game_id: str) -> list[Path]:
        directory = self.path(game_id) / "turns"
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.iterdir() if TURN_FILE.fullmatch(path.name))

    def load_initial(self, game_id: str) -> Game:
        path = self.path(game_id) / "state-init.md"
        if not path.is_file():
            raise FileNotFoundError(game_id)
        state = path.read_text(encoding="utf-8").strip() + "\n"
        opening = markdown_section(state, "Opening")
        if not opening:
            raise ValueError("state-init.md requires an Opening section")
        return Game(
            game_id=game_id, title=markdown_title(state), opening=opening,
            initial_state=state, state=state, turn=0,
        )

    def load(self, game_id: str) -> Game:
        initial = self.load_initial(game_id)
        state_path = self.path(game_id) / "state.md"
        if not state_path.is_file():
            raise FileNotFoundError(state_path)
        state = state_path.read_text(encoding="utf-8").strip() + "\n"
        return initial.model_copy(update={"state": state, "turn": len(self.turn_paths(game_id))})

    def state_at(self, game_id: str, turn: int) -> str:
        if turn == 0:
            return self.load_initial(game_id).initial_state
        path = self.path(game_id) / "turns" / f"{turn:03d}.md"
        if not path.is_file():
            raise FileNotFoundError(path)
        document = path.read_text(encoding="utf-8")
        marker = "## State after\n"
        state = document.partition(marker)[2] if marker in document else ""
        if not state:
            raise ValueError(f"{path.name} has no State after snapshot")
        return state.strip() + "\n"

    def recent_turns(self, game_id: str, limit: int = 4) -> list[dict[str, str]]:
        values = []
        for path in self.turn_paths(game_id)[-limit:]:
            document = path.read_text(encoding="utf-8")
            values.append({
                "player_input": markdown_section(document, "Player input"),
                "event": markdown_section(document, "Event"),
                "narration": markdown_section(document, "Narration"),
            })
        return values

    def append_turn(self, game_id: str, record: TurnRecord) -> None:
        game = self.load(game_id)
        if record.number != game.turn + 1:
            raise ValueError("turn numbers must be consecutive")
        turn_path = self.path(game_id) / "turns" / f"{record.number:03d}.md"
        if turn_path.exists():
            raise ValueError("turn already exists")
        _atomic_write(turn_path, record.markdown())
        _atomic_write(self.path(game_id) / "state.md", record.state_after.strip() + "\n")
        _append_text(
            self.path(game_id) / "log.md",
            f"## Turn {record.number}\n\n**Anomancer:** {record.player_input.strip()}\n\n"
            f"{record.narration.strip()}\n\n",
        )

    def append_diagnostic(self, game_id: str, **value: Any) -> None:
        path = self.path(game_id) / "diagnostics.jsonl"
        _append_text(path, json.dumps({"timestamp": now(), **value}, ensure_ascii=False) + "\n")

    def reset(self, game_id: str) -> None:
        initial = self.load_initial(game_id)
        for path in self.turn_paths(game_id):
            path.unlink()
        _atomic_write(self.path(game_id) / "state.md", initial.initial_state)
        _atomic_write(self.path(game_id) / "log.md", "")
        _atomic_write(self.path(game_id) / "diagnostics.jsonl", "")

    def bind(self, game_id: str, interface: str, channel_id: Optional[int] = None) -> None:
        self.load(game_id)
        bindings = self._bindings()
        bindings[game_id] = {"interface": interface, "channel_id": channel_id}
        _atomic_write(self.bindings_file, json.dumps(bindings, indent=2) + "\n")

    def game_for_channel(self, channel_id: int) -> Optional[str]:
        for game_id, binding in self._bindings().items():
            if binding.get("interface") == "discord" and binding.get("channel_id") == channel_id:
                if game_id in self.list_games():
                    return game_id
        return None

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
        path = self.locks / f"{game_id}.lock"
        with path.open("a+", encoding="utf-8") as handle:
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
