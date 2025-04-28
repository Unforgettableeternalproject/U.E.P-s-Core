import re
import asyncio
from typing import List, Optional, Dict, Any
from collections import deque
import time
from utils.debug_helper import info_log, error_log, debug_log

class TTSChunker:
    def __init__(self, 
                 max_chars: int = 150,
                 min_chars: int = 50,
                 respect_punctuation: bool = True,
                 pause_between_chunks: float = 0.2):
        """
        Initialize the TTS text chunker
        
        Args:
            max_chars: Maximum characters per chunk
            min_chars: Minimum characters for a chunk before it needs to be combined
            respect_punctuation: Whether to split only at punctuation boundaries
            pause_between_chunks: Pause time between chunk playback (seconds)
        """
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.respect_punctuation = respect_punctuation
        self.pause_between_chunks = pause_between_chunks
        self.queue = deque()
        self.is_playing = False
        self.stop_requested = False

    def split_text(self, text: str) -> List[str]:
        """
        Split text into appropriate chunks for TTS processing
        
        Args:
            text: Input text to split
            
        Returns:
            List of text chunks ready for processing
        """
        # Clean the input text - remove excessive whitespace and line breaks
        text = re.sub(r'\s+', ' ', text).strip()
        
        # If text is already short enough, return as is
        if len(text) <= self.max_chars:
            return [text]
            
        chunks = []
        
        # Primary splitting pattern - sentence boundaries with punctuation
        sentences = re.split(r'(?<=[.!?。？！…]) ', text)
        
        current_chunk = ""
        for sentence in sentences:
            # If current sentence exceeds max_chars, we need to split it further
            if len(sentence) > self.max_chars:
                # First add any accumulated content as its own chunk
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                
                # Then split this long sentence into subchunks
                if self.respect_punctuation:
                    # Try splitting at commas, semicolons, etc.
                    subparts = re.split(r'(?<=[,;:，、；：]) ', sentence)
                    
                    sub_current = ""
                    for part in subparts:
                        if len(sub_current) + len(part) + 1 <= self.max_chars:
                            sub_current += (" " + part if sub_current else part)
                        else:
                            if sub_current:
                                chunks.append(sub_current)
                            
                            # If a single part is still too long, force split by character count
                            if len(part) > self.max_chars:
                                for i in range(0, len(part), self.max_chars):
                                    subchunk = part[i:i + self.max_chars]
                                    chunks.append(subchunk)
                                sub_current = ""
                            else:
                                sub_current = part
                    
                    if sub_current:
                        chunks.append(sub_current)
                else:
                    # Force split by character count
                    for i in range(0, len(sentence), self.max_chars):
                        chunks.append(sentence[i:i + self.max_chars])
            
            # Normal case: try to add this sentence to the current chunk
            elif len(current_chunk) + len(sentence) + 1 <= self.max_chars:
                current_chunk += (" " + sentence if current_chunk else sentence)
            else:
                chunks.append(current_chunk)
                current_chunk = sentence
        
        # Add any remaining content
        if current_chunk:
            chunks.append(current_chunk)
        
        # Post-processing: merge very small chunks
        if self.min_chars > 0:
            merged_chunks = []
            current = ""
            
            for chunk in chunks:
                if len(current) + len(chunk) <= self.max_chars:
                    current += (" " + chunk if current else chunk)
                elif len(chunk) < self.min_chars and len(merged_chunks) > 0:
                    # Merge with the previous chunk if this one is too small
                    merged_chunks[-1] += " " + chunk
                else:
                    if current:
                        merged_chunks.append(current)
                    current = chunk
            
            if current:
                merged_chunks.append(current)
                
            chunks = merged_chunks
        
        debug_log(2, f"[TTSChunker] Split text into {len(chunks)} chunks")
        return chunks

    async def process_text(self, text: str, tts_processor, tts_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process text by splitting into chunks and queuing for sequential TTS processing
        
        Args:
            text: Text to process
            tts_processor: Function to process individual chunks
            tts_args: Arguments to pass to the TTS processor
            
        Returns:
            Dictionary with status and message
        """
        chunks = self.split_text(text)
        
        for chunk in chunks:
            self.queue.append((chunk, tts_args.copy()))
        
        info_log(f"[TTSChunker] Added {len(chunks)} chunks to the queue")
        
        # Start processing if not already running
        if not self.is_playing:
            # Create task using current event loop
            loop = asyncio.get_event_loop()
            loop.create_task(self._process_queue(tts_processor))
        
        return {
            "status": "queued",
            "message": f"Text split into {len(chunks)} chunks and queued for processing",
            "chunk_count": len(chunks)
        }
    
    async def _process_queue(self, tts_processor):
        """
        Process the queue of text chunks
    
        Args:
            tts_processor: Async function to process individual chunks
        """
        if self.is_playing:
            return
            
        self.is_playing = True
        self.stop_requested = False
    
        try:
            while self.queue and not self.stop_requested:
                chunk, args = self.queue.popleft()
            
                debug_log(2, f"[TTSChunker] Processing chunk: '{chunk[:30]}...' ({len(self.queue)} remaining)")
            
                args["save"] = False
            
                result = await tts_processor(text=chunk, **args)
            
                if result["status"] != "success":
                    error_log(f"[TTSChunker] Error processing chunk: {result['message']}")
            
                if self.queue and not self.stop_requested:
                    await asyncio.sleep(self.pause_between_chunks)
        
            info_log("[TTSChunker] Queue processing completed")
    
        except Exception as e:
            error_log(f"[TTSChunker] Error in queue processing: {str(e)}")
    
        finally:
            self.is_playing = False
    
    def stop(self):
        """Stop processing and clear the queue"""
        self.stop_requested = True
        self.queue.clear()
        info_log("[TTSChunker] Processing stopped and queue cleared")
    
    def get_queue_status(self) -> Dict[str, Any]:
        """Get the current status of the queue"""
        return {
            "is_playing": self.is_playing,
            "queue_length": len(self.queue)
        }