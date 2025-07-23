# U.E.P's Core Version History

This document provides a summary of major changes in U.E.P's Core releases.

## [0.1.0_stable] - 2025-07-23
### Release
- First stable release with complete core architecture and basic function modules

### Added
- Core module loading and registration system
- State management and workflow engine
- Logging system with type and month categorization
- File processing workflows
- External API integration (Gemini)
- Basic module structure: STT, NLP, MEM, LLM, TTS, SYS

### Fixed
- Path handling related errors
- Improved error handling for log files
- Resolved dependency issues when loading multiple modules
- Fixed empty log files and directory cleanup logic

### Improved
- Logs organized by month
- Disabled automatic log cleanup, preserving complete history
- Enhanced error handling and fallback mechanisms
- Improved configuration loading process

## [0.0.4] - 2025-06-15
### Added
- Enhanced workflow engine
- Added file processing features
- Basic TTS module functionality

### Fixed
- Concurrency issues in state management
- Index errors in memory module

## [0.0.3] - 2025-05-20
### Added
- Initial memory module implementation
- LLM integration functionality
- Basic system function module

### Improved
- Optimized NLP module performance
- Improved STT accuracy

## [0.0.2] - 2025-05-01
### Added
- NLP module integration
- Test framework setup
- Model training files

### Fixed
- Fixed STT module unit tests

## [0.0.1] - 2025-04-13
### Added
- Initial project structure
- Basic STT module functionality
- Foundational configuration system
