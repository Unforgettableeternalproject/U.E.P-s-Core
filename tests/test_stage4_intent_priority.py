# tests/test_stage4_intent_priority.py
"""
Stage 4 Intent Priority & Session Restriction Test Suite

Tests the complete session state restriction logic per NLP狀態處理.md:
- No session: DW/BW as COMMAND, CALL ignore, CHAT add state, UNKNOWN ignore
- Active CS: DW interrupt, BW queue, CHAT continue
- Active WS: All inputs as Response with metadata
- COMPOUND: Complex filtering rules
- Priority ordering: DW(100) > CALL(70) > CHAT(50) > BW(30) > UNKNOWN(10)
"""

import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch, MagicMock
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from modules.nlp_module.intent_types import IntentType, IntentSegment
from modules.nlp_module.nlp_module import NLPModule
from core.states.state_manager import UEPState as SystemState


def create_test_segment(text: str, intent_type: IntentType, confidence: float = 0.9, 
                        start_pos: int = 0, end_pos: Optional[int] = None) -> IntentSegment:
    """Helper function to create test IntentSegment with proper structure"""
    if end_pos is None:
        end_pos = len(text)
    
    return IntentSegment(
        segment_text=text,
        intent_type=intent_type,
        confidence=confidence,
        metadata={
            'start_pos': start_pos,
            'end_pos': end_pos
        }
    )


class TestStage4NoSession(unittest.TestCase):
    """Test intent processing when no CS or WS is active"""
    
    def setUp(self):
        """Set up test environment"""
        self.nlp_module = NLPModule()
    
    def test_single_call_no_session(self):
        """CALL: Ignore input, interrupt loop"""
        segments = [
            create_test_segment("Hello UEP", IntentType.CALL, confidence=0.95, start_pos=0, end_pos=9)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        self.assertTrue(result["skip_input_layer"], "CALL should interrupt loop")
        self.assertEqual(len(result["states_to_add"]), 0, "CALL should not add states")
        self.assertIn("interrupting", result["processing_notes"][0].lower())
    
    def test_single_chat_no_session(self):
        """CHAT: Add CHAT state to queue"""
        segments = [
            create_test_segment("How are you", IntentType.CHAT, confidence=0.92, start_pos=0, end_pos=11)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        self.assertFalse(result["skip_input_layer"])
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "CHAT")
        self.assertEqual(result["states_to_add"][0]["priority"], 50)
    
    def test_single_direct_work_no_session(self):
        """DW: Add WORK state with direct mode"""
        segments = [
            create_test_segment("Open file", IntentType.DIRECT_WORK, confidence=0.94, start_pos=0, end_pos=9)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        self.assertFalse(result["skip_input_layer"])
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")
        self.assertEqual(result["states_to_add"][0]["work_mode"], "direct")
        self.assertEqual(result["states_to_add"][0]["priority"], 100)
    
    def test_single_background_work_no_session(self):
        """BW: Add WORK state with background mode"""
        segments = [
            create_test_segment("Play music", IntentType.BACKGROUND_WORK, confidence=0.91, start_pos=0, end_pos=10)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        self.assertFalse(result["skip_input_layer"])
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")
        self.assertEqual(result["states_to_add"][0]["work_mode"], "background")
        self.assertEqual(result["states_to_add"][0]["priority"], 30)
    
    def test_single_unknown_no_session(self):
        """UNKNOWN: Ignore input, interrupt loop"""
        segments = [
            create_test_segment("asdf jkl qwerty", IntentType.UNKNOWN, confidence=0.15, start_pos=0, end_pos=15)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        self.assertTrue(result["skip_input_layer"], "UNKNOWN should interrupt loop")
        self.assertEqual(len(result["states_to_add"]), 0, "UNKNOWN should not add states")
        self.assertIn("unknown", result["processing_notes"][0].lower())
    
    def test_compound_call_chat_no_session(self):
        """COMPOUND: CALL + CHAT -> Ignore CALL, process CHAT"""
        segments = [
            create_test_segment("Hello UEP", IntentType.CALL, confidence=0.95, start_pos=0, end_pos=9),
            create_test_segment("How are you", IntentType.CHAT, confidence=0.92, start_pos=11, end_pos=22)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        # Should filter out CALL
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "CHAT")
    
    def test_compound_chat_command_no_session(self):
        """COMPOUND: CHAT + COMMAND -> Prioritize COMMAND"""
        segments = [
            create_test_segment("I want to", IntentType.CHAT, confidence=0.88, start_pos=0, end_pos=9),
            create_test_segment("open the file", IntentType.DIRECT_WORK, confidence=0.93, start_pos=10, end_pos=23)
        ]
        
        result = self.nlp_module._process_no_session_state(segments)
        
        # COMMAND should be added first (higher priority)
        self.assertEqual(len(result["states_to_add"]), 2)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")  # COMMAND first
        self.assertEqual(result["states_to_add"][1]["state_type"], "CHAT")


class TestStage4ActiveCS(unittest.TestCase):
    """Test intent processing when CS is active"""
    
    def setUp(self):
        """Set up test environment with mock CS"""
        self.nlp_module = NLPModule()
        self.mock_cs = Mock()
        self.mock_cs.session_id = "test_cs_123"
        self.mock_cs.pause = Mock()
    
    def test_chat_in_active_cs(self):
        """CHAT: Continue CS, don't queue"""
        segments = [
            create_test_segment("Tell me more", IntentType.CHAT, confidence=0.93, start_pos=0, end_pos=12)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertFalse(result["skip_input_layer"])
        self.assertFalse(result["interrupt_cs"])
        self.assertEqual(len(result["states_to_add"]), 0, "CHAT should not queue in CS")
        self.assertIn("continuing", result["processing_notes"][0].lower())
    
    def test_call_in_active_cs(self):
        """CALL: Treat as CHAT in CS"""
        segments = [
            create_test_segment("Hey", IntentType.CALL, confidence=0.91, start_pos=0, end_pos=3)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "CHAT")
    
    def test_direct_work_interrupt_cs(self):
        """DW: Interrupt CS, add WORK, end loop"""
        segments = [
            create_test_segment("Save file", IntentType.DIRECT_WORK, confidence=0.95, start_pos=0, end_pos=9)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertTrue(result["interrupt_cs"], "DW should interrupt CS")
        self.assertTrue(result["skip_input_layer"], "DW should end loop")
        self.assertEqual(len(result["pause_cs_sessions"]), 1)
        self.assertEqual(result["pause_cs_sessions"][0], "test_cs_123")
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")
        self.assertEqual(result["states_to_add"][0]["work_mode"], "direct")
    
    def test_background_work_queue_cs(self):
        """BW: Add WORK, don't interrupt CS"""
        segments = [
            create_test_segment("Play song", IntentType.BACKGROUND_WORK, confidence=0.92, start_pos=0, end_pos=9)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertFalse(result["interrupt_cs"], "BW should not interrupt CS")
        self.assertFalse(result["skip_input_layer"])
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")
        self.assertEqual(result["states_to_add"][0]["work_mode"], "background")
    
    def test_unknown_in_active_cs(self):
        """UNKNOWN: Continue CS, ignore input"""
        segments = [
            create_test_segment("fdsafsa rewq", IntentType.UNKNOWN, confidence=0.20, start_pos=0, end_pos=12)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertFalse(result["interrupt_cs"])
        self.assertEqual(len(result["states_to_add"]), 0)
        self.assertIn("unknown", result["processing_notes"][0].lower())
    
    def test_compound_dw_chat_interrupt_cs(self):
        """COMPOUND: DW + CHAT -> Interrupt CS, add DW then CHAT"""
        segments = [
            create_test_segment("Open file", IntentType.DIRECT_WORK, confidence=0.94, start_pos=0, end_pos=9),
            create_test_segment("and show me", IntentType.CHAT, confidence=0.89, start_pos=10, end_pos=21)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertTrue(result["interrupt_cs"], "DW+CHAT should interrupt")
        self.assertEqual(len(result["states_to_add"]), 2)
        # DW should be first (higher priority)
        self.assertEqual(result["states_to_add"][0]["state_type"], "WORK")
        self.assertEqual(result["states_to_add"][1]["state_type"], "CHAT")
    
    def test_compound_bw_chat_no_interrupt_cs(self):
        """COMPOUND: BW + CHAT -> Queue BW, continue CS"""
        segments = [
            create_test_segment("Play music", IntentType.BACKGROUND_WORK, confidence=0.91, start_pos=0, end_pos=10),
            create_test_segment("while we talk", IntentType.CHAT, confidence=0.88, start_pos=11, end_pos=24)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertFalse(result["interrupt_cs"], "BW+CHAT should not interrupt")
        # Should add BW WORK state, CHAT continues CS
        self.assertEqual(len(result["states_to_add"]), 1)
        self.assertEqual(result["states_to_add"][0]["work_mode"], "background")
    
    def test_compound_dw_bw_interrupt_cs(self):
        """COMPOUND: DW + BW -> Interrupt CS, add both"""
        segments = [
            create_test_segment("Save file", IntentType.DIRECT_WORK, confidence=0.95, start_pos=0, end_pos=9),
            create_test_segment("and sync data", IntentType.BACKGROUND_WORK, confidence=0.90, start_pos=10, end_pos=23)
        ]
        
        result = self.nlp_module._process_active_cs_state(segments, [self.mock_cs])
        
        self.assertTrue(result["interrupt_cs"], "DW+BW should interrupt")
        self.assertEqual(len(result["states_to_add"]), 2)
        # Should be sorted by priority: DW(100) > BW(30)
        self.assertEqual(result["states_to_add"][0]["priority"], 100)
        self.assertEqual(result["states_to_add"][1]["priority"], 30)


class TestStage4ActiveWS(unittest.TestCase):
    """Test intent processing when WS is active"""
    
    def setUp(self):
        """Set up test environment with mock WS"""
        self.nlp_module = NLPModule()
        self.mock_ws = Mock()
        self.mock_ws.session_id = "test_ws_456"
    
    def test_chat_in_active_ws(self):
        """CHAT: Mark for LLM to ask if work should end"""
        segments = [
            create_test_segment("Let's chat", IntentType.CHAT, confidence=0.90, start_pos=0, end_pos=10)
        ]
        
        result = self.nlp_module._process_active_ws_state(segments, [self.mock_ws])
        
        self.assertTrue(result["response_metadata"]["chat_detected"])
        self.assertTrue(result["response_metadata"]["suggest_end_work"])
        self.assertIn("end-work", result["processing_notes"][0].lower())
    
    def test_direct_work_in_active_ws(self):
        """DW: Treat as Response with work content"""
        segments = [
            create_test_segment("Create folder", IntentType.DIRECT_WORK, confidence=0.93, start_pos=0, end_pos=13)
        ]
        
        result = self.nlp_module._process_active_ws_state(segments, [self.mock_ws])
        
        self.assertTrue(result["response_metadata"]["work_content"])
        self.assertIn("direct_work", result["response_metadata"]["work_types"])
    
    def test_background_work_in_active_ws(self):
        """BW: Treat as Response with work content"""
        segments = [
            create_test_segment("Download updates", IntentType.BACKGROUND_WORK, confidence=0.91, start_pos=0, end_pos=16)
        ]
        
        result = self.nlp_module._process_active_ws_state(segments, [self.mock_ws])
        
        self.assertTrue(result["response_metadata"]["work_content"])
        self.assertIn("background_work", result["response_metadata"]["work_types"])
    
    def test_unknown_in_active_ws(self):
        """UNKNOWN: Treat as Response, let LLM handle"""
        segments = [
            create_test_segment("jfkdsal rewqrewq", IntentType.UNKNOWN, confidence=0.18, start_pos=0, end_pos=16)
        ]
        
        result = self.nlp_module._process_active_ws_state(segments, [self.mock_ws])
        
        self.assertTrue(result["response_metadata"]["uncertain_input"])
        self.assertIn("unknown", result["processing_notes"][0].lower())
    
    def test_mixed_intents_in_active_ws(self):
        """Multiple intent types: Combine metadata"""
        segments = [
            create_test_segment("Save", IntentType.DIRECT_WORK, confidence=0.93, start_pos=0, end_pos=4),
            create_test_segment("and backup", IntentType.BACKGROUND_WORK, confidence=0.90, start_pos=5, end_pos=15)
        ]
        
        result = self.nlp_module._process_active_ws_state(segments, [self.mock_ws])
        
        self.assertTrue(result["response_metadata"]["work_content"])
        self.assertIn("direct_work", result["response_metadata"]["work_types"])
        self.assertIn("background_work", result["response_metadata"]["work_types"])


class TestStage4PriorityOrdering(unittest.TestCase):
    """Test priority ordering: DW(100) > CALL(70) > CHAT(50) > BW(30) > UNKNOWN(10)"""
    
    def test_intent_priorities(self):
        """Verify correct priority assignments"""
        from modules.nlp_module.intent_types import INTENT_PRIORITY_MAP
        
        self.assertEqual(INTENT_PRIORITY_MAP[IntentType.DIRECT_WORK], 100)
        self.assertEqual(INTENT_PRIORITY_MAP[IntentType.CALL], 70)
        self.assertEqual(INTENT_PRIORITY_MAP[IntentType.CHAT], 50)
        self.assertEqual(INTENT_PRIORITY_MAP[IntentType.BACKGROUND_WORK], 30)
        self.assertEqual(INTENT_PRIORITY_MAP[IntentType.UNKNOWN], 10)
    
    def test_segment_priority_ordering(self):
        """Verify segments are created with correct priorities"""
        segments = [
            create_test_segment("chat", IntentType.CHAT, confidence=0.9, start_pos=0, end_pos=4),
            create_test_segment("call", IntentType.CALL, confidence=0.9, start_pos=5, end_pos=9),
            create_test_segment("work", IntentType.DIRECT_WORK, confidence=0.9, start_pos=10, end_pos=14),
        ]
        
        # Sort by priority descending
        sorted_segments = sorted(segments, key=lambda s: s.priority, reverse=True)
        
        self.assertEqual(sorted_segments[0].intent_type, IntentType.DIRECT_WORK)
        self.assertEqual(sorted_segments[1].intent_type, IntentType.CALL)
        self.assertEqual(sorted_segments[2].intent_type, IntentType.CHAT)


class TestStage4IntentSegmenterIntegration(unittest.TestCase):
    """Test IntentSegmenter integration with NLP module"""
    
    def test_intent_segmenter_loading(self):
        """Verify IntentSegmenter loads successfully"""
        from modules.nlp_module.intent_segmenter import get_intent_segmenter
        
        segmenter = get_intent_segmenter()
        self.assertIsNotNone(segmenter)
        self.assertTrue(hasattr(segmenter, 'segment_intents'))
    
    def test_single_intent_segmentation(self):
        """Test single intent segmentation"""
        from modules.nlp_module.intent_segmenter import get_intent_segmenter
        
        segmenter = get_intent_segmenter()
        
        test_cases = [
            ("Hello UEP", IntentType.CALL),
            ("How are you today", IntentType.CHAT),
            ("Open the file", IntentType.DIRECT_WORK),
            ("Play some music", IntentType.BACKGROUND_WORK),
        ]
        
        for text, expected_type in test_cases:
            segments = segmenter.segment_intents(text)
            self.assertGreater(len(segments), 0, f"No segments for '{text}'")
            # Check if expected type is present
            intent_types = [s.intent_type for s in segments]
            self.assertIn(expected_type, intent_types, 
                         f"Expected {expected_type.name} in {[t.name for t in intent_types]}")
    
    def test_compound_intent_detection(self):
        """Test compound intent detection"""
        from modules.nlp_module.intent_segmenter import get_intent_segmenter
        from modules.nlp_module.intent_types import IntentSegment as NewIntentSegment
        
        segmenter = get_intent_segmenter()
        
        # Test with punctuation (should work well per training)
        segments = segmenter.segment_intents("Hello UEP. How are you today?")
        
        self.assertGreater(len(segments), 0)
        is_compound = NewIntentSegment.is_compound_input(segments)
        
        # May or may not be compound depending on model, just verify no crash
        self.assertIsInstance(is_compound, bool)
        
        if is_compound:
            highest = NewIntentSegment.get_highest_priority_segment(segments)
            self.assertIsNotNone(highest)
            self.assertIsInstance(highest.priority, int)


def run_stage4_tests():
    """Run all Stage 4 tests and report results"""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestStage4NoSession))
    suite.addTests(loader.loadTestsFromTestCase(TestStage4ActiveCS))
    suite.addTests(loader.loadTestsFromTestCase(TestStage4ActiveWS))
    suite.addTests(loader.loadTestsFromTestCase(TestStage4PriorityOrdering))
    suite.addTests(loader.loadTestsFromTestCase(TestStage4IntentSegmenterIntegration))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "="*70)
    print("Stage 4 Test Summary")
    print("="*70)
    print(f"Tests run: {result.testsRun}")
    print(f"Successes: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print(f"Success rate: {((result.testsRun - len(result.failures) - len(result.errors)) / result.testsRun * 100):.1f}%")
    print("="*70)
    
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_stage4_tests()
    sys.exit(0 if success else 1)
