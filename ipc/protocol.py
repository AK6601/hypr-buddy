"""
IPC Protocol Definitions for Hypr Buddy
==========================================

Defines all message types exchanged between the three components:
  - Daemon  -> Brain  : DesktopEvent messages (via $XDG_RUNTIME_DIR/hypr-buddy/brain.sock)
  - Brain   -> Overlay : OverlayCommand messages (via $XDG_RUNTIME_DIR/hypr-buddy/overlay.sock)

All messages are serialized as single-line JSON terminated by a newline character.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Daemon -> Brain: Desktop Events
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    WINDOW_FOCUS = "window_focus"
    WINDOW_OPEN = "window_open"
    WINDOW_CLOSE = "window_close"
    WINDOW_MOVE = "window_move"
    WINDOW_RESIZE = "window_resize"
    NOTIFICATION = "notification"
    WORKSPACE = "workspace"
    FULLSCREEN = "fullscreen"
    BATTERY = "battery"
    TIME = "time"
    MONITOR = "monitor"
    CURSOR_MOVE = "cursor_move"
    SYSTEM_STATS = "system_stats"


@dataclass
class DesktopEvent:
    """A normalized desktop event sent from the daemon to the brain."""

    type: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "DesktopEvent":
        d = json.loads(raw)
        event_type = d.get("type")
        if not isinstance(event_type, str):
            raise ValueError(f"Expected string 'type', got {type(event_type).__name__}")
        return cls(
            type=event_type,
            timestamp=float(d.get("timestamp", time.time())),
            data=d.get("data") if isinstance(d.get("data"), dict) else {},
        )

    def validate(self) -> bool:
        """Basic validation: type must be a known EventType value."""
        try:
            EventType(self.type)
            return True
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# Brain -> Overlay: Commands
# ---------------------------------------------------------------------------

class CommandType(str, Enum):
    SET_STATE = "set_state"
    SAY = "say"
    MOVE = "move"
    VISIBILITY = "visibility"
    QUIT = "quit"


# Valid animation states the overlay understands.
VALID_STATES = frozenset({
    "idle", "talking", "happy", "sad", "surprised",
    "thinking", "sleeping", "waving", "angry",
    "laughing", "blushing",
})


@dataclass
class OverlayCommand:
    """A command sent from the brain to the overlay renderer."""

    cmd: str
    # Optional fields depending on command type
    state: str | None = None
    duration: float | None = None
    text: str | None = None
    x: int | None = None
    y: int | None = None
    visible: bool | None = None

    def to_json(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> "OverlayCommand":
        d = json.loads(raw)
        cmd = d.get("cmd")
        if not isinstance(cmd, str):
            raise ValueError(f"Expected string 'cmd', got {type(cmd).__name__}")
        return cls(
            cmd=cmd,
            state=d.get("state"),
            duration=d.get("duration"),
            text=d.get("text"),
            x=d.get("x"),
            y=d.get("y"),
            visible=d.get("visible"),
        )

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid)."""
        errors: list[str] = []
        try:
            CommandType(self.cmd)
        except ValueError:
            errors.append(f"Unknown command: {self.cmd}")

        if self.state is not None and self.state not in VALID_STATES:
            errors.append(f"Unknown state: {self.state}")

        if self.cmd == "set_state" and self.state is None:
            errors.append("set_state requires a 'state' field")

        if self.cmd == "say" and self.text is None:
            errors.append("say requires a 'text' field")

        if self.cmd == "move" and (self.x is None or self.y is None):
            errors.append("move requires 'x' and 'y' fields")

        if self.cmd == "visibility" and self.visible is None:
            errors.append("visibility requires a 'visible' field")

        return errors


# ---------------------------------------------------------------------------
# Helper: convenience constructors
# ---------------------------------------------------------------------------

def set_state(state: str, duration: float = 0.0) -> OverlayCommand:
    return OverlayCommand(cmd="set_state", state=state, duration=duration)

def say(text: str, state: str = "talking") -> OverlayCommand:
    return OverlayCommand(cmd="say", text=text, state=state)

def move(x: int, y: int) -> OverlayCommand:
    return OverlayCommand(cmd="move", x=x, y=y)

def hide() -> OverlayCommand:
    return OverlayCommand(cmd="visibility", visible=False)

def show() -> OverlayCommand:
    return OverlayCommand(cmd="visibility", visible=True)

def quit_overlay() -> OverlayCommand:
    return OverlayCommand(cmd="quit")


# ---------------------------------------------------------------------------
# Socket paths — use XDG_RUNTIME_DIR for proper per-user isolation
# (mode 0700, not world-readable like /tmp)
# ---------------------------------------------------------------------------

import os as _os

def _runtime_dir() -> str:
    """Return a secure, per-user runtime directory.

    Prefers XDG_RUNTIME_DIR (mode 0700 on Linux). Falls back to a
    user-owned temp directory if XDG_RUNTIME_DIR is not available.
    """
    candidates = [
        _os.environ.get("XDG_RUNTIME_DIR"),
        f"/run/user/{_os.getuid()}",
    ]
    for base in candidates:
        if base and _os.path.isdir(base):
            buddy_dir = _os.path.join(base, "hypr-buddy")
            _os.makedirs(buddy_dir, mode=0o700, exist_ok=True)
            return buddy_dir
    # Final fallback: temp dir with restricted permissions
    import tempfile
    buddy_dir = _os.path.join(tempfile.gettempdir(), f"hypr-buddy-{_os.getuid()}")
    _os.makedirs(buddy_dir, mode=0o700, exist_ok=True)
    return buddy_dir

RUNTIME_DIR = _runtime_dir()
OVERLAY_SOCKET = _os.path.join(RUNTIME_DIR, "overlay.sock")
BRAIN_SOCKET = _os.path.join(RUNTIME_DIR, "brain.sock")
PID_FILE = _os.path.join(RUNTIME_DIR, "pids")
