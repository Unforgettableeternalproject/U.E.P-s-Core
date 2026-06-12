from __future__ import annotations

import shlex
from typing import Callable, Protocol


# --- Emotion presets (name → 8D vector) ---
# Order: happy, angry, sad, afraid, disgusted, melancholic, surprised, calm
EMOTION_PRESETS: dict[str, list[float]] = {
    "neutral":     [0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0],
    "calm":        [0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.3],
    "happy":       [0.3,  0.0,  0.0,  0.0,  0.0,  0.0,  0.05, 0.0],
    "angry":       [0.0,  0.3,  0.0,  0.0,  0.05, 0.0,  0.0,  0.0],
    "sad":         [0.0,  0.0,  0.3,  0.0,  0.0,  0.1,  0.0,  0.0],
    "afraid":      [0.0,  0.0,  0.05, 0.3,  0.0,  0.0,  0.05, 0.0],
    "disgusted":   [0.0,  0.05, 0.0,  0.0,  0.3,  0.0,  0.0,  0.0],
    "melancholic": [0.0,  0.0,  0.1,  0.0,  0.0,  0.3,  0.0,  0.05],
    "surprised":   [0.05, 0.0,  0.0,  0.05, 0.0,  0.0,  0.3,  0.0],
    "gentle":      [0.1,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.2],
    "stern":       [0.0,  0.15, 0.0,  0.0,  0.05, 0.0,  0.0,  0.1],
    "wistful":     [0.05, 0.0,  0.1,  0.0,  0.0,  0.15, 0.0,  0.1],
}

EMOTION_LABELS = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


class InteractiveSession(Protocol):
    def load(self) -> None:
        ...

    def say(self, text: str):
        ...

    def save(self, text: str, output_path: str):
        ...

    def profile(self, text: str):
        ...

    def unload(self) -> None:
        ...

    def status(self) -> dict[str, object]:
        ...

    def set_emotion(self, emotion_vector: list[float]) -> None:
        ...

    def get_emotion(self) -> list[float]:
        ...


class InteractiveShell:
    def __init__(self, session: InteractiveSession, write: Callable[[str], None] = print):
        self.session = session
        self.write = write

    def execute(self, raw_command: str) -> bool:
        command = raw_command.strip()
        if not command:
            return True

        try:
            parts = shlex.split(command)
        except ValueError as exc:
            self.write(f"Parse error: {exc}")
            return True

        if not parts:
            return True

        verb = parts[0].lower()
        args = parts[1:]

        if verb in {"exit", "quit"}:
            return False
        if verb == "help":
            self.write(
                "Commands: load, say <text>, save --output <path> <text>, "
                "profile <text>, emotion [preset|values], status, unload, exit"
            )
            return True
        if verb == "emotion":
            return self._handle_emotion(args)
        if verb == "load":
            self.session.load()
            self.write("loaded")
            return True
        if verb == "unload":
            self.session.unload()
            self.write("unloaded")
            return True
        if verb == "status":
            self.write(str(self.session.status()))
            return True
        if verb == "say":
            text = self._parse_text_args(args, command_name="say")
            result = self.session.say(text)
            self.write(f"played: {result.output_path}")
            return True
        if verb == "save":
            text, output = self._parse_save_args(args)
            result = self.session.save(text, output_path=output)
            self.write(f"saved: {result.output_path}")
            return True
        if verb == "profile":
            text = self._parse_text_args(args, command_name="profile")
            result = self.session.profile(text)
            self.write(result.report.format_report())
            return True

        self.write(f"Unknown command: {verb}")
        return True

    def _handle_emotion(self, args: list[str]) -> bool:
        """Handle the emotion command: show, set preset, or set raw 8D vector."""
        if not args:
            # Show current emotion + available presets
            current = self.session.get_emotion()
            # Format with labels
            pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, current)]
            self.write(f"Current: [{', '.join(pairs)}]")
            self.write(f"Presets: {', '.join(EMOTION_PRESETS.keys())}")
            return True

        # Try as preset name
        name = args[0].lower()
        if name in EMOTION_PRESETS:
            vec = EMOTION_PRESETS[name]
            self.session.set_emotion(list(vec))
            pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, vec)]
            self.write(f"Emotion set to '{name}': [{', '.join(pairs)}]")
            return True

        # Try as 8 floats
        try:
            values = [float(a) for a in args]
            if len(values) != 8:
                self.write(f"Expected 8 values, got {len(values)}. Use: emotion <preset> or emotion H A S F D M U C")
                return True
            self.session.set_emotion(values)
            pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, values)]
            self.write(f"Emotion set: [{', '.join(pairs)}]")
        except ValueError:
            self.write(f"Unknown preset '{name}'. Available: {', '.join(EMOTION_PRESETS.keys())}")

        return True

    @staticmethod
    def _parse_text_args(args: list[str], command_name: str) -> str:
        text = " ".join(args).strip()
        if not text:
            raise ValueError(f"{command_name} requires text")
        return text

    @staticmethod
    def _parse_save_args(args: list[str]) -> tuple[str, str]:
        output: str | None = None
        text_parts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--output":
                if index + 1 >= len(args):
                    raise ValueError("--output requires a path")
                output = args[index + 1]
                index += 2
                continue
            text_parts.append(arg)
            index += 1

        if not output:
            raise ValueError("save requires --output <path>")
        text = " ".join(text_parts).strip()
        if not text:
            raise ValueError("save requires text")
        return text, output
