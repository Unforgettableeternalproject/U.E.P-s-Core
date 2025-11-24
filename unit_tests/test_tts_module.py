# -*- coding: utf-8 -*-
"""
TTS Module Complete Functionality Tests

Test core functionality of TTS module after refactoring:
1. Module initialization and component integration
2. IndexTTS Lite Engine loading
3. Character loading and switching
4. Emotion mapping from Status Manager
5. Single text synthesis
6. Long text chunking and streaming playback
7. Working Context integration
8. Error handling and edge cases

Reference validation standards in TTS 待辦.md
"""

import sys
import os
import pytest
from pathlib import Path
from typing import Dict, Any

# Add project root to system path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.tts_module.tts_module import TTSModule, PlaybackState
from modules.tts_module.schemas import TTSInput, TTSOutput
from modules.tts_module.emotion_mapper import EmotionMapper
from core.status_manager import status_manager
from core.working_context import working_context_manager
from utils.debug_helper import debug_log, info_log, error_log


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def tts():
    """Initialize TTS module"""
    # Clear sys.argv temporarily
    original_argv = sys.argv
    sys.argv = [original_argv[0]]  # Keep script name only
    
    try:
        module = TTSModule()
        # Initialize engine (if models are available)
        success = module.initialize()
        if not success:
            pytest.skip("TTS engine initialization failed - models may not be available")
        
        yield module
        
        # Cleanup
        try:
            if hasattr(module, 'shutdown'):
                module.shutdown()
        except:
            pass
    finally:
        # Restore sys.argv
        sys.argv = original_argv


@pytest.fixture(scope="function")
def reset_state(tts):
    """Reset playback state before each test"""
    tts._playback_state = PlaybackState.IDLE
    tts._current_playback_obj = None
    yield


# ============================================================================
# Test 1: Module Initialization
# ============================================================================

class TestModuleInitialization:
    """Test TTS module initialization"""
    
    def test_module_creation(self):
        """Test 1.1: Module can be created"""
        module = TTSModule()
        assert module is not None
        assert module.config is not None
        info_log("[Test] TTS module created successfully")
    
    def test_config_loading(self):
        """Test 1.2: Configuration loaded correctly"""
        module = TTSModule()
        assert module.model_dir is not None
        assert module.character_dir is not None
        assert module.default_character is not None
        assert isinstance(module.chunking_enabled, bool)
        assert isinstance(module.chunking_threshold, int)
        info_log("[Test] TTS configuration loaded correctly")
    
    def test_component_initialization(self):
        """Test 1.3: Core components initialized"""
        module = TTSModule()
        assert module.emotion_mapper is not None
        assert module.chunker is not None
        assert module.working_context_manager is not None
        assert module.status_manager is not None
        info_log("[Test] TTS components initialized")


# ============================================================================
# Test 2: Engine Initialization
# ============================================================================

class TestEngineInitialization:
    """Test IndexTTS Lite engine initialization"""
    
    def test_engine_initialization(self, tts):
        """Test 2.1: Engine initializes successfully"""
        assert tts.engine is not None
        info_log("[Test] IndexTTS Lite engine initialized")
    
    def test_engine_device(self, tts):
        """Test 2.2: Engine uses correct device"""
        assert tts.engine.device is not None
        assert str(tts.engine.device) in ['cuda', 'cpu', 'mps']
        info_log(f"[Test] Engine device: {tts.engine.device}")
    
    def test_character_loaded(self, tts):
        """Test 2.3: Default character loaded"""
        assert tts.engine.current_character is not None
        assert tts.engine.character_features is not None
        info_log(f"[Test] Character loaded: {tts.engine.current_character}")


# ============================================================================
# Test 3: Emotion Mapping
# ============================================================================

class TestEmotionMapping:
    """Test emotion mapping from Status Manager"""
    
    def test_emotion_mapper_exists(self, tts):
        """Test 3.1: Emotion mapper is available"""
        assert tts.emotion_mapper is not None
        info_log("[Test] Emotion mapper available")
    
    def test_map_from_status_manager(self, tts):
        """Test 3.2: Map emotions from Status Manager values"""
        # Get current status values
        status = status_manager.get_status()
        mood = status.get("mood", 0.0)
        pride = status.get("pride", 0.5)
        helpfulness = status.get("helpfulness", 0.5)
        boredom = status.get("boredom", 0.0)
        
        # Map to emotion vector
        emotion_vector = tts.emotion_mapper.map_from_status_manager(
            mood, pride, helpfulness, boredom
        )
        
        assert isinstance(emotion_vector, list)
        assert len(emotion_vector) == 8
        assert all(isinstance(v, float) for v in emotion_vector)
        info_log(f"[Test] Emotion vector mapped: {[f'{v:.3f}' for v in emotion_vector]}")


# ============================================================================
# Test 4: Single Text Synthesis
# ============================================================================

class TestSingleSynthesis:
    """Test single text synthesis"""
    
    def test_short_text_synthesis(self, tts, reset_state):
        """Test 4.1: Synthesize short text"""
        input_data = {
            "text": "Hello, this is a test for TTS module.",
            "save": False
        }
        
        result = tts.handle(input_data)
        print("🗣️ TTS response:", result)
        
        assert result["status"] == "success"
        assert result["is_chunked"] == False
        assert result["chunk_count"] == 1
        info_log("[Test] Short text synthesis successful")
    
    def test_synthesis_with_emotion(self, tts, reset_state):
        """Test 4.2: Synthesize with custom emotion vector"""
        emotion_vec = [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.2]
        
        input_data = {
            "text": "I am feeling happy today!",
            "save": False,
            "emotion_vector": emotion_vec
        }
        
        result = tts.handle(input_data)
        assert result["status"] == "success"
        info_log("[Test] Synthesis with custom emotion successful")
    
    def test_empty_text_handling(self, tts):
        """Test 4.3: Handle empty text input"""
        input_data = {
            "text": "",
            "save": False
        }
        
        result = tts.handle(input_data)
        print("❌ TTS error response:", result)
        
        assert result["status"] == "error"
        assert "required" in result["message"].lower()
        info_log("[Test] Empty text handled correctly")


# ============================================================================
# Test 5: Chunking and Streaming
# ============================================================================

class TestChunkingAndStreaming:
    """Test long text chunking and streaming playback"""
    
    def test_chunking_enabled(self, tts):
        """Test 5.1: Chunking is enabled"""
        assert tts.chunking_enabled == True
        assert tts.chunking_threshold > 0
        info_log(f"[Test] Chunking enabled, threshold: {tts.chunking_threshold}")
    
    def test_long_text_chunking(self, tts):
        """Test 5.2: Long text is chunked correctly"""
        long_text = "Hello! " * 50
        chunks = tts.chunker.split_text(long_text)
        
        assert len(chunks) > 1
        assert all(isinstance(chunk, str) for chunk in chunks)
        info_log(f"[Test] Text chunked into {len(chunks)} segments")
    
    def test_streaming_synthesis(self, tts, reset_state):
        """Test 5.3: Streaming synthesis for long text"""
        long_text = (
            "Area 3 is a totalitarian state called Crambell, divided into four quadrants "
            "named after the Four Horsemen of the Apocalypse. Each quadrant serves a specific "
            "purpose: Famein for the elderly, weak, women, and children; Pestilens as an arms "
            "control Zone and rest for the army; Wyar as the industrial and important district "
            "with connections to Area 4; and finally, Delth where most citizens live and work, "
            "having mining operations and dorms for miners."
        )
        
        input_data = {
            "text": long_text,
            "save": False,
            "force_chunking": True
        }
        
        result = tts.handle(input_data)
        print(f"🗣️ TTS streaming response: {result}")
        
        assert result["status"] == "success"
        assert result["is_chunked"] == True
        assert result["chunk_count"] > 1
        info_log(f"[Test] Streaming synthesis successful, {result['chunk_count']} chunks")


# ============================================================================
# Test 6: Error Handling
# ============================================================================

class TestErrorHandling:
    """Test error handling and edge cases"""
    
    def test_invalid_input_schema(self, tts):
        """Test 6.1: Handle invalid input schema"""
        result = tts.handle({"invalid_key": "value"})
        
        assert result["status"] == "error"
        assert "invalid" in result["message"].lower()
        info_log("[Test] Invalid input handled correctly")


# ============================================================================
# Main Test Runner
# ============================================================================

if __name__ == "__main__":
    """Run tests with pytest"""
    pytest.main([__file__, "-v", "-s"])
