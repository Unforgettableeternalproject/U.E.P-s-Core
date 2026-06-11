# Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python Entry.py smoke
python Entry.py benchmark --text "Hello from the TTS test project."
```

## Generate with copied IndexTTS2

The first-phase parity target is direct generation through the copied U.E.P IndexTTS2 assets:

```powershell
python Entry.py generate --text "Hello, this is U.E.P TTS running from the separate test project." --output outputs/tts/smoke.wav --device cuda
```

Large assets are expected at:

- `legacy_tts/modules/tts_module/checkpoints/`
- `legacy_tts/models/tts/uep.pt`

These files are intentionally ignored by Git.
## Persistent shell

Use the shell when iterating on multiple samples. The model is loaded only after `load`, `say`, or `save`, then reused until `unload` or process exit.

```powershell
python Entry.py shell
```

Example session:

```text
tts> load
tts> say "Hello from the persistent TTS shell."
tts> save --output saved.wav "Generate this without playback."
tts> status
tts> unload
tts> exit
```

Command semantics:

- `say <text>`: generate a temporary shell output, play it immediately, then delete the temporary file.
- `save --output <path> <text>`: generate a file without playback.
- `--no-playback`: shell fallback for devices/environments without audio output.
