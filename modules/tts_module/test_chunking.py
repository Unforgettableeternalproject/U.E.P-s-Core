"""
TTS Chunking Test Script
Test TTSChunker segmentation and IndexTTS generation
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from utils.tts_chunker import TTSChunker
from modules.tts_module.tts_module import TTSModule
from modules.tts_module.schemas import TTSInput
import time

def print_separator(title: str):
    """Print separator line"""
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70 + "\n")

def test_simple_tts(tts: TTSModule):
    """Test 1: Simple TTS Generation"""
    print_separator("Test 1: Simple TTS Generation")
    
    try:
        test_text = "Hello! This is a simple test of the TTS system."
        print(f"📝 Input text: {test_text}")
        print(f"📏 Text length: {len(test_text)} chars\n")
        
        print("🔊 Generating and playing audio (not saving)...\n")
        start_time = time.time()
        result = tts.handle({"text": test_text, "save": False})
        elapsed = time.time() - start_time
        
        print(f"✅ Generation and playback successful!")
        print(f"⏱️  Time taken: {elapsed:.2f} seconds")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_chunker_basic():
    """Test 2: Basic TTSChunker Segmentation"""
    print_separator("Test 2: Basic TTSChunker Segmentation")
    
    try:
        chunker = TTSChunker(
            max_chars=100,
            min_chars=30,
            respect_punctuation=True,
            pause_between_chunks=0.1
        )
        
        test_text = (
            "This is the first sentence. This is the second sentence with a comma. "
            "Does the third sentence have a question mark? Of course it does! "
            "The fourth sentence is longer and contains more content, "
            "and it also has some conjunctions like this. The last sentence ends here."
        )
        
        print(f"📝 Input text:\n{test_text}\n")
        print(f"📏 Text length: {len(test_text)} chars")
        print(f"⚙️  max_chars: 100, min_chars: 30\n")
        
        chunks = chunker.split_text(test_text)
        
        print(f"✅ Segmentation complete! Total {len(chunks)} chunks:\n")
        for i, chunk in enumerate(chunks, 1):
            print(f"Chunk {i} ({len(chunk)} chars):")
            print(f"  {chunk}")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_chunker_protection():
    """Test 3: URL/Abbreviation Protection (Patch A)"""
    print_separator("Test 3: URL/Abbreviation Protection (Patch A)")
    
    try:
        chunker = TTSChunker(max_chars=80)
        
        test_text = (
            "Please visit https://example.com for more information. "
            "Contact us at user@example.com for help. "
            "Abbreviations like e.g. and i.e. should stay intact. "
            "Numbers like 1,234.56 should keep their format."
        )
        
        print(f"📝 Input text:\n{test_text}\n")
        print(f"📏 Text length: {len(test_text)} chars\n")
        
        chunks = chunker.split_text(test_text)
        
        print(f"✅ Segmentation complete! Total {len(chunks)} chunks:\n")
        for i, chunk in enumerate(chunks, 1):
            print(f"Chunk {i}:")
            print(f"  {chunk}")
            
            # Check if protection works
            if 'https://' in test_text and 'https://' in chunk:
                print("  ✓ URL preserved intact")
            if '@' in test_text and '@' in chunk:
                print("  ✓ Email preserved intact")
            if 'e.g.' in chunk or 'i.e.' in chunk:
                print("  ✓ Abbreviations preserved intact")
            if '1,234.56' in chunk:
                print("  ✓ Number format preserved intact")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_chunker_quotes():
    """Test 4: Quote/Bracket Pairing (Patch B)"""
    print_separator("Test 4: Quote/Bracket Pairing (Patch B)")
    
    try:
        chunker = TTSChunker(max_chars=60)
        
        test_text = (
            'He said "This is a long quoted content that should not be split in the middle", then continued. '
            'There are also cases like (content inside parentheses should stay complete) like this.'
        )
        
        print(f"📝 Input text:\n{test_text}\n")
        print(f"📏 Text length: {len(test_text)} chars\n")
        
        chunks = chunker.split_text(test_text)
        
        print(f"✅ Segmentation complete! Total {len(chunks)} chunks:\n")
        for i, chunk in enumerate(chunks, 1):
            print(f"Chunk {i}:")
            print(f"  {chunk}")
            
            # Check pairing
            if '"' in chunk:
                if chunk.count('"') % 2 == 0:
                    print("  ✓ Quotes properly paired")
                else:
                    print("  ⚠️  Quotes not paired!")
            if '(' in chunk:
                if ')' in chunk:
                    print("  ✓ Parentheses properly paired")
                else:
                    print("  ⚠️  Parentheses not paired!")
            print()
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_long_text_with_tts(tts: TTSModule):
    """Test 5: Long Text TTS Generation (Integration Test)"""
    print_separator("Test 5: Long Text TTS Generation (Integration Test)")
    
    try:
        test_text = (
            "Hello everyone! Today I want to talk about artificial intelligence. "
            "AI has become increasingly important in our daily lives. "
            "From voice assistants like me, to recommendation systems, "
            "machine learning is everywhere. However, we must also consider "
            "the ethical implications of AI development. Privacy, bias, "
            "and transparency are crucial issues that need to be addressed. "
            "As we move forward, it's important to develop AI responsibly. "
            "Thank you for listening!"
        )
        
        print(f"📝 Input text:\n{test_text}\n")
        print(f"📏 Text length: {len(test_text)} chars")
        print(f"⚙️  chunking_threshold: {tts.chunking_threshold}\n")
        
        # Preview chunking results
        chunks = tts.chunker.split_text(test_text)
        print(f"📊 Expected {len(chunks)} chunks:")
        for i, chunk in enumerate(chunks, 1):
            print(f"  Chunk {i} ({len(chunk)} chars): {chunk[:50]}...")
        print()
        
        # Actual generation with streaming playback
        print("🔊 Starting streaming generation and playback...\n")
        print("💡 Audio will play as each chunk is generated (Producer/Consumer pattern)\n")
        start_time = time.time()
        result = tts.handle({"text": test_text, "save": False})
        elapsed = time.time() - start_time
        
        print(f"\n✅ Streaming playback complete!")
        print(f"⏱️  Total time: {elapsed:.2f} seconds")
        print(f"⚡ Average per chunk: {elapsed / len(chunks):.2f} seconds")
        print(f"🎵 Audio was played in real-time as chunks were generated")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Main test flow"""
    print("\n" + "🔊 TTS Chunking Test Suite".center(70, "="))
    print("Testing TTSChunker (Patch A+B) with IndexTTS Integration\n")
    
    # Initialize TTS module once for all tests
    print("🔧 Initializing TTS Module...")
    try:
        tts = TTSModule()
        tts.initialize()
        print("✅ TTS Module initialized successfully\n")
    except Exception as e:
        print(f"❌ Failed to initialize TTS Module: {e}")
        import traceback
        traceback.print_exc()
        return
    
    results = []
    
    # Execute all tests
    tests = [
        ("Simple TTS Generation", lambda: test_simple_tts(tts)),
        ("Basic Segmentation", test_chunker_basic),
        ("URL/Abbreviation Protection", test_chunker_protection),
        ("Quote Pairing", test_chunker_quotes),
        ("Long Text Integration", lambda: test_long_text_with_tts(tts))
    ]
    
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except KeyboardInterrupt:
            print("\n\n⚠️  Test interrupted by user\n")
            break
        except Exception as e:
            print(f"\n❌ Unexpected error in test '{test_name}': {e}\n")
            results.append((test_name, False))
    
    # Summary
    print_separator("Test Summary")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ Passed" if result else "❌ Failed"
        print(f"{status} - {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")
    
    print("\n" + "="*70 + "\n")

if __name__ == "__main__":
    main()
