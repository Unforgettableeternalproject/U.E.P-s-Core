# U.E.P's Core - v0.1.0 Stable

### This project provides multilanguage README.md file
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## Project Overview

U.E.P (Unforgettable Eternal Project) is a desktop AI assistant project aimed at creating a personified AI companion with voice communication, environmental awareness, and task automation capabilities. The project uses a modular architecture that allows flexible enabling or disabling of different features.

## Core Features

✯ System Architecture:
- 🔹 Highly modular design with separation of core and functional modules
- 🔹 Flexible state management system supporting various workflows
- 🔹 Advanced logging system organized by type and month
- 🔹 Robust error handling and fallback mechanisms
- 🔹 Configuration-driven dynamic feature enabling

✯ Main Functions:
- 🔹 Multiple module collaboration (STT, NLP, MEM, LLM, TTS, SYS)
- 🔹 Complex workflow engine supporting multi-step operations
- 🔹 External API integration (such as Gemini model)
- 🔹 File processing, window control, and automation tasks
- 🔹 Flexible extension mechanism with dynamic module loading

## Project Structure

```
U.E.P-s-Core/
├── arts/               # Art resources and visual designs
├── configs/            # Global and module configurations
├── core/               # Core system components
│   ├── controller.py   # Main controller
│   ├── module_base.py  # Module base class
│   ├── registry.py     # Module registry
│   ├── router.py       # Message router
│   ├── session_manager.py # Session management
│   └── state_manager.py   # State management
├── devtools/           # Developer tools
├── docs/               # Documentation and specifications
├── logs/               # Log directory (organized by type and month)
├── memory/             # Memory storage
├── models/             # Model files
├── modules/            # Functional modules collection
│   ├── stt_module/     # Speech-to-Text module
│   ├── nlp_module/     # Natural Language Processing
│   ├── mem_module/     # Memory management module
│   ├── llm_module/     # Large Language Model
│   ├── tts_module/     # Text-to-Speech
│   └── sys_module/     # System functionality module
├── utils/              # Common utilities and helper functions
└── Entry.py            # Program entry point
```

## Installation and Configuration

1. Clone the repository
   ```
   git clone https://github.com/Unforgettableeternalproject/U.E.P-s-Core.git
   cd U.E.P-s-Core
   ```

2. Install dependencies
   ```
   pip install -r requirements.txt
   ```

3. Configure settings
   - Edit `configs/config.yaml` to enable required modules
   - Each module also has its own configuration file in the respective module directory

4. Run the program
   ```
   python Entry.py
   ```

## Current Development Status

⚑ Completed Features:
- ✅ Core architecture design and implementation
- ✅ Module dynamic loading and registration system
- ✅ Logging system (organized by type and month)
- ✅ Workflow engine framework
- ✅ State management system
- ✅ Basic file processing workflows

⚑ Features in Progress:
- ⏳ MEMModule integration and snapshot structure optimization
- ⏳ LLMModule output format standardization (including emotions and commands)
- ⏳ Event triggering system improvement
- ⏳ Frontend module (UI/MOV/ANI) integration

## Development Roadmap

Please refer to `docs/第二階段進度.md` for detailed development roadmap, including:
- Core six modules reconstruction focus
- Frontend three modules planning
- Frontend-backend integration and development collaboration emphasis

## Contributors

❦ Main contributors:
- ඩ elise-love
- ඩ yutao33003

## License

This project is under a private license. Unauthorized copying, modification, or distribution is prohibited.