from __future__ import annotations

import shlex
from typing import Callable, Protocol


class InteractiveSession(Protocol):
    def load(self) -> None:
        ...

    def say(self, text: str):
        ...

    def save(self, text: str, output_path: str):
        ...

    def unload(self) -> None:
        ...

    def status(self) -> dict[str, object]:
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
            self.write("Commands: load, say <text>, save --output <path> <text>, status, unload, exit")
            return True
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

        self.write(f"Unknown command: {verb}")
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
