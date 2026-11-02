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
    
    def _restore_protected(self, text: str, protect_map: Dict[str, str]) -> str:
        """
        還原被保護的片段（URL、縮寫、數字）
        
        Args:
            text: 包含placeholder的文本
            protect_map: placeholder到原始內容的映射
            
        Returns:
            還原後的文本
        """
        # 還原特殊符號
        text = text.replace('∯', '.').replace('∮', ',')
        
        # 還原 URL / email 等 placeholder
        for key, value in protect_map.items():
            text = text.replace(key, value)
        
        return text.strip()

    def split_text(self, text: str) -> List[str]:
        """
        Split text into appropriate chunks for TTS processing
        改進版：包含 URL/縮寫保護、引號配對、更智能的中英混合斷句
        
        Args:
            text: Input text to split
            
        Returns:
            List of text chunks ready for processing
        """
        # --- [PATCH A] 更聰明的預清理與斷句 ---
        text = text.strip()
        
        # 暫存保護的片段
        _protect_map = {}
        protect_counter = [0]  # 使用列表來在閉包中修改
        
        def _make_placeholder(prefix):
            key = f"<<{prefix}_{protect_counter[0]}>>"
            protect_counter[0] += 1
            return key
        
        # 1) 保護 URL 和 email
        def _protect_url(match):
            key = _make_placeholder("URL")
            _protect_map[key] = match.group(0)
            return key
        text = re.sub(r'https?://\S+|www\.\S+|\S+@\S+', _protect_url, text)
        
        # 2) 保護常見英文縮寫（避免句點被當成句末）
        # e.g., i.e., etc., Mr., Dr., Prof., vs.
        abbr_pattern = r'\b(e\.g\.|i\.e\.|etc\.|vs\.|Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.)'
        text = re.sub(abbr_pattern, lambda m: m.group(0).replace('.', '∯'), text)
        
        # 3) 保護數字中的逗號和小數點（避免 1,234.56 被切割）
        text = re.sub(r'(\d),(?=\d{3}\b)', r'\1∮', text)  # 千分位
        text = re.sub(r'(\d)\.(?=\d)', r'\1∯', text)  # 小數點
        
        # 4) 清理多餘空白
        text = re.sub(r'\s+', ' ', text)
        
        # If text is already short enough, restore and return
        if len(text) <= self.max_chars:
            return [self._restore_protected(text, _protect_map)]
        
        chunks = []
        
        # 5) 更穩健的中英混合斷句
        # 支援中文無空格、引號/括號後斷句
        # 在 .!?。？！… 後面，若是引號/括號/結尾，視為句邊界
        sentence_pattern = r'(?<=[.!?。？！…])(?=[\s」』）》）\]\'\"]*(?:\s|$))'
        sentences = re.split(sentence_pattern, text)
        
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
        
        # 還原所有保護的片段
        chunks = [self._restore_protected(chunk, _protect_map) for chunk in chunks]
        
        # Post-processing: merge very small chunks
        if self.min_chars > 0:
            merged_chunks = []
            current = ""
            
            for chunk in chunks:
                # 嘗試合併到 current
                if len(current) + len(chunk) + 1 <= self.max_chars:
                    current += (" " + chunk if current else chunk)
                else:
                    # current 已滿,需要保存
                    if current:
                        merged_chunks.append(current)
                    
                    # 檢查新 chunk 是否太小
                    if len(chunk) < self.min_chars and len(merged_chunks) > 0:
                        # 嘗試合併到前一個已保存的 chunk
                        if len(merged_chunks[-1]) + len(chunk) + 1 <= self.max_chars:
                            merged_chunks[-1] += " " + chunk
                            current = ""
                        else:
                            # 無法合併,作為新 current
                            current = chunk
                    else:
                        # chunk 大小正常,作為新 current
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
        改進版：加入引號/括號配對、更好的切點優先級
        
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
            # --- [PATCH B] 更穩健的切點搜尋 ---
            chunk_end = max_chars
            best_split = -1
            
            # 定義搜尋窗口（往前最多120個字符）
            window_start = max(0, min(chunk_end, len(remaining)) - 120)
            window_end = min(len(remaining), chunk_end)
            window = remaining[window_start:window_end]
            
            def abs_pos(local_i):
                """轉換局部索引到全局索引"""
                return window_start + local_i
            
            # 0) 引號/括號配對檢查：避免切斷配對符號
            # 區分不對稱括號和對稱引號
            asymmetric_pairs = {
                '（': '）', '(': ')', 
                '【': '】', '「': '」', '『': '』', 
                '[': ']', '《': '》'
            }
            symmetric_quotes = {'"', "'"}  # 對稱引號需要特殊處理
            
            stack = []
            quote_state = {}  # 追蹤對稱引號的開閉狀態
            
            for i, ch in enumerate(window):
                # 處理不對稱括號
                if ch in asymmetric_pairs:
                    stack.append(asymmetric_pairs[ch])
                elif ch in asymmetric_pairs.values():
                    if stack and stack[-1] == ch:
                        stack.pop()
                # 處理對稱引號（toggle 狀態）
                elif ch in symmetric_quotes:
                    if quote_state.get(ch, False):
                        quote_state[ch] = False  # 關閉引號
                        # 從 stack 中移除這個引號
                        if ch in stack:
                            stack.remove(ch)
                    else:
                        quote_state[ch] = True  # 開啟引號
                        stack.append(ch)  # 標記有未閉合的引號
            
            # 如果有未閉合的引號/括號，往前找最後一個已閉合的句點
            if stack:
                for i in range(len(window)-1, -1, -1):
                    if window[i] in '。？！….;；,':
                        best_split = abs_pos(i+1)
                        break
            
            # 1) 強句界：句末標點 + 空白/引號/括號/結尾
            if best_split == -1:
                for i in range(len(window)-1, -1, -1):
                    if window[i] in '.!?。？！…':
                        j = i + 1
                        if j >= len(window) or window[j] in ' 」』）》）]\'\"':
                            best_split = abs_pos(i+1)
                            break
            
            # 2) 次級標點：分號、冒號、破折號、中文頓號
            if best_split == -1:
                for i in range(len(window)-1, -1, -1):
                    if window[i] in ';；:：—、':
                        best_split = abs_pos(i+1)
                        break
            
            # 3) 逗號（避開數字千分位）
            if best_split == -1:
                for i in range(len(window)-1, -1, -1):
                    if window[i] == ',':
                        # 避免 1,234 類型
                        if i-1 >= 0 and i+1 < len(window) and window[i-1].isdigit() and window[i+1].isdigit():
                            continue
                        best_split = abs_pos(i+1)
                        break
            
            # 4) 連詞（, and, but, etc.）
            if best_split == -1:
                conjunction_patterns = [', and ', ', but ', ', or ', ', so ', ', yet ', 
                                      ', that ', ', which ', ', who ', ', when ', ', where ', ', while ']
                for i in range(len(window)-1, -1, -1):
                    for pattern in conjunction_patterns:
                        start = i - len(pattern) + 1
                        if start >= 0 and window[start:i+1].lower() == pattern:
                            best_split = abs_pos(i+1)
                            break
                    if best_split != -1:
                        break
            
            # 5) 空白
            if best_split == -1:
                for i in range(len(window)-1, -1, -1):
                    if window[i] == ' ':
                        best_split = abs_pos(i+1)
                        break
            
            # 6) 80% 強制切，避免切在連字符詞中
            if best_split == -1:
                best_split = int(window_start + max_chars * 0.8)
                # 往回找到最近的非字母數字字符
                while best_split > 0 and best_split < len(remaining) and remaining[best_split].isalnum():
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