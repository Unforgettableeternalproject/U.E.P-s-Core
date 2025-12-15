# U.E.P's Core - v0.9.4-stable (Phase 3 Stable Release)

### This project provides multilanguage README.md file
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## Project Overview

U.E.P (Unified Experience Partner) is a modular desktop AI assistant with **event-driven architecture**, featuring voice interaction, memory management, intelligent workflows, and desktop companion capabilities. The project has completed **Phase 2 reconstruction** and released **Phase 3 stable version** (core features 70-75% complete), with fully implemented frontend bridging, special states, intelligent VAD activation, three-panel user gadget, and more. The system is stable and production-ready for core functionalities.

## Core Features

✯ **System Architecture** (Phase 2 - Event-Driven):
- 🔹 **Event Bus** - 20+ system events for loosely-coupled module communication
- 🔹 **Three-Layer Processing Model** - Input → Processing → Output with flow-based deduplication
- 🔹 **Three-Tier Session Management** - General Session (GS) / Chatting Session (CS) / Workflow Session (WS)
- 🔹 **Working Context** - Collaboration channels (CHAT_MEM, WORK_SYS) for cross-module data exchange
- 🔹 **State-Session Integration** - Automatic session creation on state transitions
- 🔹 **Status Manager** - Dynamic mood/pride/helpfulness tracking

✯ **Six Core Modules** (95% Complete):
- 🔹 **STT** - Whisper-large-v3, VAD, speaker identification
- 🔹 **NLP** - BIOS intent segmentation, identity management, state decision authority
- 🔹 **MEM** - FAISS vector database, identity-isolated memory, snapshot system
- 🔹 **LLM** - Gemini API with context caching, MCP client, learning engine
- 🔹 **TTS** - IndexTTS Lite, emotion mapping, chunked streaming
- 🔹 **SYS** - Workflow engine with 9 categories, MCP server, background tasks

✯ **Frontend Modules** (Phase 3 In Progress - 81% Complete):
- 🔹 **UI** - Desktop overlay with PyQt5, three-panel user gadget, quick actions, theme system
- 🔹 **ANI** - Event-driven animation controller with emotion expression mapping
- 🔹 **MOV** - State-synchronized behavior engine with movement integration
- 🔹 **Frontend Bridge** - Unified frontend module management and event coordination

✯ **Phase 3 New Features**:
- 🌉 **Frontend-Backend Integration** - Frontend Bridge for complete coordination and event distribution
- 🎯 **Special State Mechanics** - MISCHIEF state with 5 behaviors and LLM-driven planning
- 😴 **Sleep-Wake System** - SLEEP state resource release and fast wake mechanism
- 🗣️ **Intelligent VAD Activation** - CALL intent-based voice triggering with reduced false positives
- 🎛️ **User Gadget** - Three-panel interface (status/settings/monitor) with auto-hide
- ⚡ **Quick Actions** - One-click show/hide, ON_CALL invocation, sleep toggle
- 🎵 **Complete Media Control** - Background playback monitoring with real-time control
- 📊 **System Monitoring** - Real-time module health, performance metrics, resource tracking

## Project Structure

```
U.E.P-s-Core/
├── arts/                    # Art resources and animation assets
├── configs/                 # Global and module configurations
├── core/                    # Core system components (Phase 2)
│   ├── controller.py        # Unified controller with exception management
│   ├── event_bus.py         # Event-driven architecture foundation
│   ├── framework.py         # Module coordinator and framework
│   ├── module_coordinator.py # Three-layer processing orchestrator
│   ├── registry.py          # Module registry with capabilities
│   ├── router.py            # Legacy router (Phase 3 cleanup)
│   ├── working_context.py   # Cross-module collaboration channels
│   ├── bases/               # Base classes for modules
│   ├── sessions/            # GS/CS/WS session managers
│   └── states/              # State management and queue
├── devtools/                # Developer tools and debug API
├── docs/                    # Documentation (SDD, Phase progress)
│   └── SDD/                 # System Design Documents
├── integration_tests/       # End-to-end integration tests
├── logs/                    # Log directory (debug/runtime/error)
├── memory/                  # Persistent memory and FAISS indices
├── models/                  # ML models (Whisper, TTS, NLP)
├── modules/                 # Functional modules collection
│   ├── stt_module/          # Speech-to-Text with VAD
│   ├── nlp_module/          # NLP with intent segmentation
│   ├── mem_module/          # Memory with identity isolation
│   ├── llm_module/          # LLM with context caching
│   ├── tts_module/          # TTS with emotion control
│   ├── sys_module/          # System workflows and MCP server
│   ├── ui_module/           # User interface (Phase 3)
│   ├── ani_module/          # Animation controller (Phase 3)
│   ├── mov_module/          # Movement behavior (Phase 3)
│   └── frontend_integration.py # Frontend coordinator
├── utils/                   # Common utilities and helpers
├── wheel/                   # Pre-compiled packages (not distributed)
└── Entry.py                 # Program entry point
```

## Installation and Configuration

### Prerequisites
- Python 3.10+
- CUDA 12.8+ (for GPU acceleration)
- Windows 10/11 (primary support)

### Installation Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/Unforgettableeternalproject/U.E.P-s-Core.git
   cd U.E.P-s-Core
   ```

2. **Create virtual environment**
   ```bash
   python -m venv env
   # Windows
   .\env\Scripts\activate
   # Linux/Mac
   source env/bin/activate
   ```

3. **Install PyTorch with CUDA** (Manual Step)
   ```bash
   # For RTX 40xx/50xx series with CUDA 12.8
   pip install torch==2.7.0+cu128 torchvision==0.22.0+cu128 torchaudio==2.7.0+cu128 \
     --index-url https://download.pytorch.org/whl/cu128
   ```
   > **Note**: PyTorch+CUDA must be installed separately due to specific GPU requirements

4. **Install other dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Install pre-compiled packages** (from `wheel/` directory)
   ```bash
   # Some packages require manual installation from wheel/
   # These are not publicly distributed due to custom builds
   pip install wheel/pyannote.audio-*.whl
   pip install wheel/fairseq-*.whl
   # ... (see wheel/ directory for available packages)
   ```

6. **Configure settings**
   - Copy `configs/config.yaml.example` to `configs/config.yaml` (if exists)
   - Edit `configs/config.yaml` to:
     - Set your Gemini API key
     - Enable/disable modules
     - Adjust debug levels
   - Each module has its own `config.yaml` in `modules/xxx_module/`

7. **Run the program**
   ```bash
   # Production mode
   python Entry.py
   
   # Debug mode (interactive CLI)
   python Entry.py --debug
   
   # Debug GUI mode
   python Entry.py --debug-gui
   ```

### Troubleshooting
- **CUDA not found**: Ensure NVIDIA drivers are up to date
- **PyAudio issues**: May require portaudio library on Linux
- **Missing wheel files**: Contact maintainers for access to pre-compiled packages

## Development Status

### ✅ Phase 1 - Core Module Foundation (Completed)
- Core six modules (STT, NLP, MEM, LLM, TTS, SYS) basic implementation
- Module registration and dynamic loading
- Basic workflow engine
- Configuration system

### ✅ Phase 2 - Event-Driven Architecture (Completed - v0.7.4)
**Architecture Transformation** (96% completion):
- ✅ Event Bus with 20+ system events
- ✅ Three-layer processing model (Input/Processing/Output)
- ✅ Three-tier session management (GS/CS/WS)
- ✅ Working Context with collaboration channels
- ✅ State-Session integration
- ✅ Flow-based deduplication mechanism

**Module Refactoring** (95% avg completion):
- ✅ **STT**: VAD, Whisper-large-v3, speaker identification
- ✅ **NLP**: BIOS segmentation, identity management, state authority
- ✅ **MEM**: FAISS vector DB, identity isolation, snapshot system (100%)
- ✅ **LLM**: Context caching, MCP client, learning engine
- ✅ **TTS**: IndexTTS Lite, emotion mapping, chunked streaming
- ✅ **SYS**: Workflow engine, MCP server, 9 workflow categories (100%)

**Key Achievements**:
- ✅ Session-state unified lifecycle
- ✅ MCP protocol integration for LLM tool-calling
- ✅ Identity-isolated memory with per-user FAISS indices
- ✅ Status Manager with mood/pride/helpfulness tracking
- ✅ Integration tests for critical paths

### ✅ Phase 3 - Frontend Integration (Stable Release - v0.9.4-stable)
**Overall Completion**: 70-75% (Core Features Stable)

**Completed Objectives**:
- ✅ **Objective 1 - Frontend Integration** (90%)
  - Frontend Bridge system for complete coordination
  - Three-panel user gadget: Status/Settings/Monitor (95%)
  - Quick action buttons: Show/Call/Sleep (90%)
  - Auto-hide widget implementation
  - Configuration hot-reload mechanism
  
- ✅ **Objective 2 - Special States** (92%)
  - MISCHIEF state 100% complete: 5 behaviors, LLM planning engine, execution stats
  - SLEEP state 85% complete: Resource release, wake mechanism, animation integration
  
- ✅ **Objective 3 - Advanced VAD** (90%)
  - CALL intent-based intelligent activation
  - VAD/text input dual-mode support
  - State-aware activation control
  
- ⚠️ **Objective 4 - Workflow Enhancement** (45%)
  - ✅ Play Media workflow fully implemented: Background monitoring, real-time control (90%)
  - ✅ Memory system toolification: Tool-based retrieval (80%)
  - ❌ Sub-workflow system not yet implemented
  - ❌ Natural language path resolution pending
  
- ✅ **Objective 5 - System Monitoring** (72%)
  - Module performance metrics auto-collection (85%)
  - User Gadget & Debug GUI real-time display (90%)
  - Basic exception logging mechanism (40%)
  
- ⚠️ **Objective 6 - Module Structure Unification** (30%)
  - Partial modules have sub-structures (MEM, SYS, UI, MOV)
  - Complete unification refactoring in progress

**Key Achievements**:
- ✅ Frontend Bridge for event-driven frontend-backend coordination
- ✅ Dual event systems (System Event Bus + Frontend Event Bus)
- ✅ StatusManager fully integrated with frontend modules
- ✅ MISCHIEF behavior system with LLM-driven planning
- ✅ Configuration hot-reload with identity management integration
- ✅ Performance metrics history tracking (last 10 snapshots)

**Future Plans**: v0.10.0 (Phase 3 Complete) expected in 2026 Q1

### 📅 Phase 4 - Platform Adaptation (Future)
- Multi-platform support (Windows/Linux/macOS)
- Performance optimization
- Public beta testing
- Production deployment

## Documentation

- **System Design**: `docs/SDD.md` - Complete system architecture documentation
- **Phase 2 Progress**: `docs/第二階段進度.md` - Phase 2 planning and goals
- **Phase 3 Progress**: `docs/第三階段進度.md` - Phase 3 detailed objectives and roadmap
- **Project Progress**: `docs/本學期的專案進度.md` - Overall project status
- **API Reference**: `docs/SDD/` - Module-specific design documents

## Contributors

❦ Main contributors:
- ඩ unforgettableeternalproject (Bernie)
- ඩ elise-love
- ඩ yutao33003

## License

This project is under a private license. Unauthorized copying, modification, or distribution is prohibited.