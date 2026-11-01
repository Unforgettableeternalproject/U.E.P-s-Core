# U.E.P (Unified Experience Partner) - Project Overview

## Project Introduction

U.E.P is an **event-driven intelligent assistant system** designed to create an AI companion with long-term memory, dynamic emotions, and personalized interactions. The project adopts a modular architecture, integrating speech recognition, natural language processing, large language models, text-to-speech synthesis, and visual animation to provide a complete multimodal interaction experience.

### Core Philosophy
- **Long-term Memory Capability**: Solves the problem of traditional AI assistants having short-term memory and being unable to accumulate user interaction history
- **Flexible Workflow System**: Automatically recognizes user intent through LLM and executes corresponding complex task flows
- **Modularity & Extensibility**: Adopts event-driven architecture, supporting independent module development, testing, and replacement

---

## My Responsibilities

As the core developer of the project, I am responsible for:

### System Architecture Design
- Designed three-layer event-driven architecture (Input Layer → Processing Layer → Output Layer)
- Implemented central Event Bus coordinating 9 functional modules
- Established three-tier session management system (Global Session / Chat Session / Working Session)

### Core Feature Implementation
- **Memory System**: FAISS-based vector database supporting identity isolation and long-term memory retrieval
- **Workflow Engine**: Integrated MCP protocol, implementing 21 automated workflows (document generation, schedule management, knowledge retrieval, etc.)
- **Frontend Integration**: Developed Frontend Bridge and three-window toolset (Live2D animation, subtitles, dialogue bubbles)
- **Special State System**: Implemented dynamic emotion system (MISCHIEF mischief mode, SLEEP sleep state, etc.)

### Testing & Documentation
- Established comprehensive testing system: 476 test cases with 85% coverage
- Authored System Design Document (SDD), Project Execution Plan (PEP), and Test Reports (TR-00 ~ TR-07)

---

## Core Features

### 1. Event-Driven Modular Architecture
- **9 Functional Modules**: Speech Input (STT), Natural Language Processing (NLP), Memory Management (MEM), Language Model (LLM), System Control (SYS), Text-to-Speech (TTS), User Interface (UI), Animation Control (ANI), Motion Execution (MOV)
- **Event Bus Hub**: Coordinates inter-module communication through 20+ event types, achieving loosely coupled design

### 2. Identity-Isolated Long-Term Memory System
- Each user owns an independent FAISS vector index
- Supports semantic retrieval, time-range filtering, and memory management
- MCP tooling design allows LLM to actively query and create memories

### 3. LLM-Driven Workflow Automation
- **21 Workflows**: Schedule management, note-taking, document generation, knowledge retrieval, reminder settings, email drafts, etc.
- **NLP Intent Recognition**: Automatically analyzes user input and triggers corresponding workflows
- **MCP Protocol Integration**: Standardized LLM tool invocation interface

### 4. Three-Tier Session Management
- **Global Session (GS)**: System-level settings and global state
- **Chat Session (CS)**: Context and participant information for a single conversation
- **Working Session (WS)**: Temporary state during workflow execution

### 5. Dynamic Emotion & Personality System
- **Status Manager**: Tracks user states (IDLE, LISTENING, THINKING, SPEAKING, etc.)
- **MISCHIEF Mode**: Low-probability triggered playful interactions
- **SLEEP State**: Draggable wake-up animation interaction

### 6. Complete Frontend Integration
- **Frontend Bridge**: Unified management of frontend communication (Live2D animation, subtitle display, dialogue bubbles)
- **Three-Window Toolset**: Main window (Live2D), subtitle window, dialogue window
- **Animation Event System**: Frontend reports events after animation completion, ensuring process synchronization

---

## Technologies Used

### AI / ML Technologies
- **Whisper**: OpenAI speech recognition model
- **Google Gemini**: Large language model (supporting 2M token context caching)
- **FAISS**: Facebook vector similarity search engine
- **Edge-TTS**: Microsoft text-to-speech service

### System Architecture
- **Python 3.10**: Core development language
- **PyQt6**: System loop and event integration
- **Event Bus Pattern**: Central event coordination
- **MCP Protocol**: Model Context Protocol (LLM tool invocation standard)
- **YAML Configuration Management**: Modular configuration files

### Development Tools
- **pytest**: Unit testing and integration testing
- **GitHub Actions**: CI/CD automation
- **logging**: Hierarchical logging system (debug/runtime/error)

---

## Project Status

### Current Version: v0.9.4-stable
- **Core Module Stability**: Event Bus, Sessions, Controller, Frontend Bridge are all stable

### Testing Status
- **Total Test Cases**: 476
- **Pass Rate**: 81.9% (390 passed / 476 total)
- **Test Coverage**: 85% line coverage, 80% branch coverage
- **Known Issues**: 28 (P0=1, P1=6, P2=18, P3=3)

---

## Development Challenges & Achievements

### 1. Architecture Refactoring: From Direct Calls to Event-Driven
**Challenge**: v1.0 used direct function calls, resulting in tightly coupled modules that were difficult to maintain and extend.

**Solution**: Complete refactoring to event-driven architecture, with all modules communicating through Event Bus, achieving loosely coupled design.

**Achievements**:
- Modules can be developed and tested independently
- Adding new features requires no modification to existing code
- System stability significantly improved

### 2. MCP Protocol Integration
**Challenge**: How to enable LLM to actively invoke system functions (memory queries, workflow execution, etc.)?

**Solution**: Integrated Model Context Protocol, encapsulating system functions as standardized tools, allowing LLM to execute operations through structured calls.

**Achievements**:
- Enabled LLM to actively query memories
- Supported complex workflow automation (e.g., generating email drafts with attachments)
- Established an extensible tool ecosystem

### 3. Identity-Isolated Memory System
**Challenge**: How to ensure memory isolation and security in a multi-user environment?

**Solution**: Created independent FAISS indexes for each user, tracking memory flow through Memory Token mechanism.

**Achievements**:
- Completely isolated user memories
- Efficient semantic retrieval (FAISS vector search)
- Traceable memory sources (avoiding hallucination issues)

### 4. Performance Optimization
**Challenge**: System cold start time was excessively long (initially 120s), affecting user experience.

**Solution**:
- Lazy loading of non-essential modules
- Gemini context caching (reducing token processing time)
- Removed redundant initialization processes

**Results**: Cold start time reduced to **47.6s** (60% reduction)

### 5. Technical Debt Management
**Challenge**: ConditionalStep workflow steps execute sequentially without waiting mechanisms, leading to race conditions.

**Current Status**: Documented in technical debt list, planned for refactoring in v0.10.0.

---

## Future Roadmap

### Short-term Goals
- Fix TTS module loading time issue (currently 20~24s)
- Increase unit test pass rate to 90%
- Complete error handling mechanisms for workflow engine

### Mid-term Goals
- Refactor workflow engine (resolve ConditionalStep architecture debt)
- Implement more workflow types (30+ types)
- Increase test coverage to 90%

### Long-term Goals
- Support multi-user concurrent sessions
- Cloud memory synchronization functionality
- Complete plugin ecosystem

---

## Project Highlights

### Technical Innovation
- ✅ Event-driven architecture achieving complete module decoupling
- ✅ MCP protocol integration enabling LLM to actively invoke system functions
- ✅ Identity-isolated vector memory system

### Development Achievements
- ✅ 476 test cases with 85% coverage
- ✅ 21 automated workflows
- ✅ Complete frontend integration (Live2D animation, subtitles, dialogue bubbles)

### Project Management
- ✅ Complete documentation system (SDD, PEP, TR test reports)
- ✅ Iterative development (Phase 1 → Phase 2 → Phase 3)
- ✅ CI/CD automated testing

### Long-term Value
- ✅ Extensible modular design, easy to add new features
- ✅ Standardized interfaces (Event Bus, MCP), supporting third-party integration
- ✅ Comprehensive testing and monitoring system ensuring system stability

---

**Programming Language**: Python 3.10  
**Codebase Size**: Approximately 15,000+ lines (excluding tests)  
**Test Coverage**: 85% line coverage, 80% branch coverage  
**Test Pass Rate**: 81.9% (390/476 cases)
