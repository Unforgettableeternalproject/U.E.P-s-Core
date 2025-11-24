"""
Module Tests Package
===================

This package contains modularized test functions for different components of the U.E.P system.
Each module has its own test file with functions that take 'modules' as the first parameter.

Available test modules:
- stt_tests: Speech-to-Text testing functions
- nlp_tests: Natural Language Processing testing functions
- frontend_tests: Frontend/UI testing functions
- mem_tests: Memory system testing functions
- llm_tests: Large Language Model testing functions
- tts_tests: Text-to-Speech testing functions
- sys_tests: System-level testing functions

All test functions are designed to work with the debug_api.py wrapper system
that automatically passes the 'modules' parameter.
"""

# Import all test modules to make them available
from . import stt_tests
from . import nlp_tests
from . import frontend_tests
from . import mem_tests
from . import llm_tests
from . import tts_tests
from . import sys_tests

__all__ = [
    'stt_tests',
    'nlp_tests', 
    'frontend_tests',
    'mem_tests',
    'llm_tests',
    'tts_tests',
    'sys_tests'
]
