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
                    # 首先嘗試在較好的語意邊界分割
                    chunks_from_sentence = self._smart_split_long_text(sentence, self.max_chars)
                    chunks.extend(chunks_from_sentence)
                else:
                    # Force split by character count with smart boundaries
                    smart_chunks = self._smart_split_long_text(sentence, self.max_chars)
                    chunks.extend(smart_chunks)
            
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
            
                if not isinstance(chunk, str):
                    error_log(f"[TTSChunker] Invalid chunk type: {type(chunk)}. Skipping...")
                    continue

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
    
    def _smart_split_long_text(self, text: str, max_chars: int) -> List[str]:
        """
        智能分割長文本，盡量保持語意完整
        
        Args:
            text: 要分割的長文本
            max_chars: 每段最大字符數
            
        Returns:
            分割後的文本段落列表
        """
        if len(text) <= max_chars:
            return [text]
        
        chunks = []
        remaining = text
        
        while len(remaining) > max_chars:
            # 在max_chars範圍內尋找最佳切割點
            chunk_end = max_chars
            best_split = -1
            
            # 尋找最佳分割點（按優先級排序）
            # 1. 句號、問號、感嘆號後的空格
            for i in range(min(chunk_end, len(remaining)), max(chunk_end - 50, 0), -1):
                if i > 0 and i < len(remaining) and remaining[i-1:i+1] in ['. ', '? ', '! ', '。 ', '？ ', '！ ']:
                    best_split = i
                    break
            
            # 2. 連詞和從句分界點（更自然的語意切分）
            if best_split == -1:
                # 尋找 ", and", ", but", ", or", ", that", ", which" 等結構
                conjunction_patterns = [', and ', ', but ', ', or ', ', so ', ', yet ', ', that ', ', which ', ', who ', ', when ', ', where ', ', while ']
                for i in range(min(chunk_end, len(remaining)), max(chunk_end - 80, 0), -1):
                    for pattern in conjunction_patterns:
                        if i >= len(pattern) and i <= len(remaining):
                            if remaining[i-len(pattern):i].lower() == pattern:
                                best_split = i - len(pattern) + 1  # 在逗號後分割，保持"and"在下一段
                                break
                    if best_split != -1:
                        break
            
            # 3. 破折號、分號等標點
            if best_split == -1:
                for i in range(min(chunk_end, len(remaining)), max(chunk_end - 30, 0), -1):
                    if i > 0 and i < len(remaining) and remaining[i-1:i+1] in ['— ', '- ', '; ', ': ', '； ', '： ']:
                        best_split = i
                        break
            
            # 4. 介詞短語的邊界
            if best_split == -1:
                preposition_patterns = [' of ', ' in ', ' on ', ' at ', ' by ', ' for ', ' with ', ' from ', ' to ', ' into ']
                for i in range(min(chunk_end, len(remaining)), max(chunk_end - 40, 0), -1):
                    for pattern in preposition_patterns:
                        if i >= len(pattern) and i <= len(remaining):
                            if remaining[i-len(pattern):i] == pattern:
                                best_split = i - len(pattern)
                                break
                    if best_split != -1:
                        break
            
            # 5. 任何空格
            if best_split == -1:
                for i in range(min(chunk_end, len(remaining)), max(chunk_end - 20, 0), -1):
                    if i < len(remaining) and remaining[i] == ' ':
                        best_split = i
                        break
            
            # 6. 如果都找不到，就在80%位置強制切割（避免切斷單詞）
            if best_split == -1:
                best_split = int(max_chars * 0.8)
                # 確保不在單詞中間切割
                while best_split > 0 and best_split < len(remaining) and remaining[best_split] != ' ':
                    best_split -= 1
                if best_split <= 0:
                    best_split = max_chars
            
            # 確保分割點在有效範圍內
            best_split = max(1, min(best_split, len(remaining)))
            
            # 提取chunk並清理
            chunk = remaining[:best_split].strip()
            if chunk:  # 只添加非空段落
                chunks.append(chunk)
            
            # 更新剩餘文本
            remaining = remaining[best_split:].strip()
            
            # 防止無限循環
            if len(remaining) == len(text):
                break
        
        # 添加剩餘部分
        if remaining.strip():
            chunks.append(remaining.strip())
        
        return chunks

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