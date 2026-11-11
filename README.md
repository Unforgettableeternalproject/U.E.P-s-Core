# U.E.P's Core

### This project provides multilanguage README.md file
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## Project Overview

U.E.P (Unified Experience Partner) is a long-lived personal AI assistant core. The repository now hosts the **new Core** (`uep/`) plus the subsystems migrated into it (`subsystems/`).

> **Legacy removed**: the old nine-module event-driven system (`core/`, `modules/`, `utils/`, `devtools/`, `Entry.py`) has been deleted. Its last state is preserved at git tag `legacy-final-v0.9.4`; old documents are no longer shipped in the repository (see that tag if needed).

## Project Structure

```
U.E.P-s-Core/
├── uep/                     # New Core (Presence / Bus / Orchestrator / Endpoint ...)
├── subsystems/              # Migrated subsystems (echo_memory, echo_stt, echo_tts, echo_thought_core)
├── tests/uep/               # Core tests
├── scripts/                 # Gate script, env setup, index checker
├── docs/
│   ├── 架構概觀.md          # Architecture overview (public highlights)
│   └── index/               # Index / Tracking tables
├── train/nlp/               # NLP training data (seed for the Laya dataset)
├── arts/                    # Concept art
├── alembic.ini              # PostgreSQL migrations
└── pyproject.toml / uv.lock # Environment (Python 3.12)
```

## Installation and Configuration

- Python 3.12, managed by `uv` (`pyproject.toml` + `uv.lock`); Windows 10/11 is the primary platform; PostgreSQL is required for most Core tests.
- Build or update the shared inference environment: `scripts/setup_infer_env.ps1` (default `~/.uep/venv-inference-312-t211`).
- Run the whole test gate: `scripts/run_all_tests.ps1`; Core tests only: `python -m pytest tests/uep -q`.
- Check the index tables: `python scripts/check_index.py`.
- Acceptance console: `python -m uep.console` (internal guide, not published).

## Documentation

- **Architecture overview**: `docs/架構概觀.md` (detailed designs and contracts are not published)
- **Where is X / how far is Y**: `docs/index/` (`INDEX.md`, `TRACKING.md`)
- **Legacy (deprecated)**: git tag `legacy-final-v0.9.4`

## Contributors

❦ Main contributors:
- ඩ unforgettableeternalproject (Bernie)
- ඩ elise-love
- ඩ yutao33003

## License

This project is under a private license. Unauthorized copying, modification, or distribution is prohibited.