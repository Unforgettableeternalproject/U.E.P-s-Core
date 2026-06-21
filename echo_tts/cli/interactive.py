from __future__ import annotations

import shlex
from typing import Callable, Protocol

# ── Emotion presets & helpers: loaded from echo_tts/config/emotion_presets.yaml ──
from echo_tts.config.emotion_presets import (
    EMOTION_LABELS,
    load_presets,
    get_preset_vector,
    get_preset_overrides,
)

EMOTION_PRESETS = load_presets()

# --- Language support ---
SUPPORTED_LANGUAGES = {"en": "English", "zh": "Chinese"}


def _detect_language(text: str) -> str:
    """Detect text language: 'zh', 'en', or 'other'.

    Heuristic rules:
    - CJK chars (Unified Ideographs) > 30% -> 'zh'
    - Japanese kana / Korean detected -> 'other'
    - Latin alphabet dominant -> 'en'
    - Non-Latin scripts (Cyrillic, Arabic, Thai, etc.) -> 'other'
    """
    if not text or not text.strip():
        return "en"
    total = max(len(text.strip()), 1)
    cjk = sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')
    if cjk / total > 0.3:
        return "zh"
    # Check for Japanese kana
    kana = sum(1 for c in text if '぀' <= c <= 'ヿ' or 'ㇰ' <= c <= 'ㇿ')
    if kana / total > 0.1:
        return "other"
    # Check for Korean
    hangul = sum(1 for c in text if '가' <= c <= '힯' or 'ᄀ' <= c <= 'ᇿ')
    if hangul / total > 0.1:
        return "other"
    # Check for non-Latin scripts (Cyrillic, Arabic, Thai, Devanagari, etc.)
    non_latin = sum(1 for c in text if ord(c) > 0x024f and not ('一' <= c <= '鿿'))
    if non_latin / total > 0.2:
        return "other"
    return "en"


def _translate_text(text: str, target_lang: str, source_lang: str = "auto") -> str | None:
    """Translate text using deep_translator (Google free). Returns None on failure.

    Args:
        text: Text to translate
        target_lang: Target language code ('en' or 'zh')
        source_lang: Source language code, or 'auto' for auto-detect
    """
    tgt_code = "en" if target_lang == "en" else "zh-CN"
    if source_lang == "auto":
        src_code = "auto"
    elif source_lang == "zh":
        src_code = "zh-CN"
    elif source_lang == "en":
        src_code = "en"
    else:
        src_code = "auto"  # unknown source -> let Google detect

    try:
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source=src_code, target=tgt_code).translate(text)
        return result
    except ImportError:
        try:
            from googletrans import Translator
            tgt = "en" if target_lang == "en" else "zh-cn"
            result = Translator().translate(text, dest=tgt).text
            return result
        except ImportError:
            return None
    except Exception:
        return None


class InteractiveSession(Protocol):
    def load(self) -> None:
        ...

    def say(self, text: str):
        ...

    def save(self, text: str, output_path: str):
        ...

    def save_auto(self, text: str):
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

    def set_emotion_overrides(self, overrides: dict | None) -> None:
        ...

    def get_emotion_overrides(self) -> dict | None:
        ...

    def set_language(self, language: str) -> None:
        ...

    def get_language(self) -> str:
        ...

    def load_lora(self, checkpoint_path: str, scale: float = 1.0) -> None:
        ...

    def set_lora_scale(self, scale: float) -> None:
        ...

    def unload_lora(self) -> None:
        ...

    def get_lora_status(self) -> dict:
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
                "Commands:\n"
                "  say <text>                   Generate and play\n"
                "  save <text>                  Save with auto timestamp filename\n"
                "  save --output <path> <text>  Save to specific path (no overwrite)\n"
                "  profile <text>               Generate with stage profiling\n"
                "  emotion [preset|values]      Show/set emotion\n"
                "  language [en|zh]             Show/set language\n"
                "  lora [on|off|<scale>]        LoRA control\n"
                "  load / unload / status / exit\n"
                "Flags:\n"
                "  --batch  (say/save) generate all emotion presets"
            )
            return True
        if verb == "emotion":
            return self._handle_emotion(args)
        if verb in {"language", "lang"}:
            return self._handle_language(args)
        if verb == "lora":
            return self._handle_lora(args)
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
            batch, remaining = self._extract_batch_flag(args)
            text = self._parse_text_args(remaining, command_name="say")
            text = self._auto_translate(text)
            if batch:
                self._run_emotion_batch(text, mode="say")
            else:
                result = self.session.say(text)
                self.write(f"played: {result.output_path}")
            return True
        if verb == "save":
            batch, remaining = self._extract_batch_flag(args)
            text, output = self._parse_save_args(remaining)
            text = self._auto_translate(text)
            if batch:
                self._run_emotion_batch(text, mode="save", output_stem=output)
            else:
                if output is not None:
                    result = self.session.save(text, output_path=output)
                else:
                    result = self.session.save_auto(text)
                self.write(f"saved: {result.output_path}")
            return True
        if verb == "profile":
            text = self._parse_text_args(args, command_name="profile")
            text = self._auto_translate(text)
            result = self.session.profile(text)
            self.write(result.report.format_report())
            return True

        self.write(f"Unknown command: {verb}")
        return True

    def _handle_language(self, args: list[str]) -> bool:
        """Handle the language command: show or set language."""
        if not args:
            current = self.session.get_language()
            name = SUPPORTED_LANGUAGES.get(current, current)
            self.write(f"Current language: {current} ({name})")
            self.write(f"Available: {', '.join(f'{k} ({v})' for k, v in SUPPORTED_LANGUAGES.items())}")
            self.write("Auto-translate: text in wrong language will be translated before synthesis")
            return True

        lang = args[0].lower()
        if lang not in SUPPORTED_LANGUAGES:
            self.write(f"Unsupported language '{lang}'. Available: {', '.join(SUPPORTED_LANGUAGES.keys())}")
            return True

        self.session.set_language(lang)
        name = SUPPORTED_LANGUAGES[lang]
        self.write(f"Language set to: {lang} ({name})")
        return True

    def _handle_lora(self, args: list[str]) -> bool:
        """Handle the lora command: toggle on/off, or adjust scale.

        Usage:
            lora            — toggle on/off
            lora <0.0-1.0>  — turn on (or adjust) with specific scale
            lora on         — turn on at default scale
            lora off        — turn off
        """
        status = self.session.get_lora_status()

        if not args:
            # Toggle
            if status["active"]:
                try:
                    self.session.unload_lora()
                    self.write("🧬 LoRA OFF — 原始權重已還原")
                except Exception as e:
                    self.write(f"Error: {e}")
            else:
                try:
                    self.session.load_lora()
                    new = self.session.get_lora_status()
                    self.write(f"🧬 LoRA ON  scale={new['scale']:.0%}")
                except Exception as e:
                    self.write(f"LoRA load failed: {e}")
            return True

        subcmd = args[0].lower()

        if subcmd == "on":
            scale = float(args[1]) if len(args) > 1 else None
            try:
                if scale is not None:
                    self.session.load_lora(scale=scale)
                else:
                    self.session.load_lora()
                new = self.session.get_lora_status()
                self.write(f"🧬 LoRA ON  scale={new['scale']:.0%}")
            except Exception as e:
                self.write(f"LoRA load failed: {e}")
            return True

        if subcmd == "off":
            try:
                self.session.unload_lora()
                self.write("🧬 LoRA OFF — 原始權重已還原")
            except Exception as e:
                self.write(f"Error: {e}")
            return True

        # "lora scale 0.85" alias
        if subcmd == "scale" and len(args) > 1:
            try:
                scale = float(args[1])
                if not status["active"]:
                    self.session.load_lora(scale=scale)
                else:
                    self.session.set_lora_scale(scale)
                self.write(f"🧬 LoRA ON  scale={scale:.0%}")
            except ValueError:
                self.write(f"Invalid scale: {args[1]}")
            except RuntimeError as e:
                self.write(f"Error: {e}")
            return True

        # Direct scale value: "lora 0.85"
        try:
            scale = float(subcmd)
            if not status["active"]:
                self.session.load_lora(scale=scale)
            else:
                self.session.set_lora_scale(scale)
            self.write(f"🧬 LoRA ON  scale={scale:.0%}")
            return True
        except ValueError:
            pass

        self.write("Usage: lora [on|off|scale <0-1>|<0-1>]")
        return True

    def _auto_translate(self, text: str) -> str:
        """Auto-detect language and route accordingly.

        Language routing rules:
        - Supported language (zh/en) -> auto-set session language, pass through
          (normalizer handles mixed zh+en and trad->simp internally)
        - Unsupported language ('other') -> translate to session language
        """
        detected = _detect_language(text)

        # Supported language: auto-follow detection, no translation needed
        if detected in ("zh", "en"):
            current = self.session.get_language()
            if detected != current:
                self.session.set_language(detected)
                det_name = SUPPORTED_LANGUAGES.get(detected, detected)
                self.write(f"[Auto-language: {det_name}]")
            return text  # normalizer handles trad->simp and mixed language

        # Unsupported language: translate to session language
        source_hint = detected if detected in ("zh", "en") else "auto"
        translated = _translate_text(text, target_lang, source_lang=source_hint)
        if translated:
            src_name = SUPPORTED_LANGUAGES.get(detected, f"other ({detected})")
            tgt_name = SUPPORTED_LANGUAGES.get(target_lang, target_lang)
            self.write(f"[Auto-translate {src_name} -> {tgt_name}]: {translated}")
            return translated

        self.write(f"[Warning] Translation failed, using original text")
        return text

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
            entry = EMOTION_PRESETS[name]
            vec = get_preset_vector(entry)
            overrides = get_preset_overrides(entry)
            self.session.set_emotion(list(vec))
            self.session.set_emotion_overrides(overrides)
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
            self.session.set_emotion_overrides(None)
            pairs = [f"{l}={v:.2f}" for l, v in zip(EMOTION_LABELS, values)]
            self.write(f"Emotion set: [{', '.join(pairs)}]")
        except ValueError:
            self.write(f"Unknown preset '{name}'. Available: {', '.join(EMOTION_PRESETS.keys())}")

        return True

    @staticmethod
    def _extract_batch_flag(args: list[str]) -> tuple[bool, list[str]]:
        """Extract ``--batch`` flag from args, return (is_batch, remaining_args)."""
        if "--batch" in args:
            remaining = [a for a in args if a != "--batch"]
            return True, remaining
        return False, args

    def _run_emotion_batch(
        self,
        text: str,
        mode: str = "save",
        output_stem: str | None = None,
    ) -> None:
        """Generate the same text with every emotion preset.

        Args:
            text: the text to synthesize.
            mode: ``"save"`` to save files, ``"say"`` to save + play each.
            output_stem: base filename for save (e.g. ``"test"`` → ``test_happy.wav``).
                If None, auto-generates based on timestamp.
        """
        from pathlib import Path

        original_emotion = self.session.get_emotion()
        presets = list(EMOTION_PRESETS.items())
        total = len(presets)

        self.write(f"[batch] Generating {total} emotion variants...")

        # Determine output directory and stem
        if output_stem is not None:
            p = Path(output_stem)
            stem = p.stem
            suffix = p.suffix if p.suffix.lower() in ('.wav', '.mp3', '.flac', '.ogg') else '.wav'
            out_dir = self.session._resolve_output_path(p).parent
        else:
            from datetime import datetime
            stem = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            suffix = ".wav"
            out_dir = self.session.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        original_overrides = self.session.get_emotion_overrides()

        results = []
        for i, (preset_name, entry) in enumerate(presets):
            vec = get_preset_vector(entry)
            overrides = get_preset_overrides(entry)
            self.session.set_emotion(vec)
            self.session.set_emotion_overrides(overrides)
            out_path = out_dir / f"{stem}_{preset_name}{suffix}"
            self.write(f"  [{i + 1}/{total}] {preset_name}...")

            try:
                if mode == "say":
                    # Save then play
                    result = self.session.save(text, output_path=out_path)
                    self.session.player.play(result.output_path)
                else:
                    result = self.session.save(text, output_path=out_path)
                results.append((preset_name, result.output_path))
            except Exception as exc:
                self.write(f"    ⚠ {preset_name} failed: {exc}")

        # Restore original emotion + overrides
        self.session.set_emotion(original_emotion)
        self.session.set_emotion_overrides(original_overrides)

        self.write(f"[batch] Done — {len(results)}/{total} files:")
        for name, path in results:
            self.write(f"  {name}: {path}")

    @staticmethod
    def _parse_text_args(args: list[str], command_name: str) -> str:
        text = " ".join(args).strip()
        if not text:
            raise ValueError(f"{command_name} requires text")
        return text

    @staticmethod
    def _parse_save_args(args: list[str]) -> tuple[str, str | None]:
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

        text = " ".join(text_parts).strip()
        if not text:
            raise ValueError("save requires text")

        # Auto-append .wav if output path has no audio extension
        if output is not None:
            from pathlib import Path
            p = Path(output)
            if p.suffix.lower() not in ('.wav', '.mp3', '.flac', '.ogg'):
                output = str(p.with_suffix('.wav'))

        return text, output
