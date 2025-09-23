# modules/mem_module/analysis/memory_analyzer.py
"""
記憶分析器 - 記憶內容分析和洞察提取

功能：
- 記憶重要性評估
- 主題提取和分類
- 意圖標籤生成
- 記憶關聯分析
- 使用者行為模式分析
"""

import re
import json
from typing import Dict, Any, List, Optional, Set, Tuple
from datetime import datetime, timedelta
from collections import Counter, defaultdict

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import MemoryEntry, MemoryType, MemoryImportance


class MemoryAnalyzer:
    """記憶分析器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 分析配置
        self.importance_keywords = config.get("importance_keywords", {
            "critical": ["error", "failed", "urgent", "important", "critical", "problem", "issue", "bug", "crash"],
            "high": ["learn", "remember", "note", "warning", "advice", "success", "achievement", "milestone"],
            "medium": ["discuss", "analyze", "think", "consider", "try", "test", "explore", "review"],
            "low": ["chat", "casual", "maybe", "perhaps", "might", "could", "possibly", "general"]
        })
        
        self.topic_keywords = config.get("topic_keywords", {
            "technology": ["program", "code", "development", "API", "database", "algorithm", "architecture", "software"],
            "learning": ["learn", "education", "course", "knowledge", "skill", "training", "research", "study"],
            "work": ["work", "project", "task", "meeting", "report", "progress", "plan", "business"],
            "personal": ["life", "daily", "hobby", "interest", "entertainment", "leisure", "health", "personal"],
            "problem_solving": ["problem", "solve", "fix", "debug", "troubleshoot", "error", "help", "support"]
        })
        
        self.intent_patterns = config.get("intent_patterns", {
            "question": ["what", "how", "why", "when", "where", "which", "?"],
            "request": ["please", "can you", "could you", "help", "assist", "support"],
            "statement": ["I think", "I believe", "in my opinion", "actually", "the fact is"],
            "learning": ["learn", "understand", "master", "familiar", "research", "explore"],
            "feedback": ["good", "great", "thanks", "thank you", "nice", "awesome", "excellent"]
        })
        
        # 分析緩存
        self._analysis_cache: Dict[str, Dict[str, Any]] = {}
        self._pattern_cache: Dict[str, List[str]] = {}
        
        # 統計資訊
        self.stats = {
            "memories_analyzed": 0,
            "topics_extracted": 0,
            "intents_identified": 0,
            "importance_evaluations": 0,
            "pattern_matches": 0
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化記憶分析器"""
        try:
            info_log("[MemoryAnalyzer] 初始化記憶分析器...")
            
            # 編譯正則表達式模式
            self._compile_patterns()
            
            # 載入分析模型（如果需要）
            self._load_analysis_models()
            
            self.is_initialized = True
            info_log("[MemoryAnalyzer] 記憶分析器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 初始化失敗: {e}")
            return False
    
    def _compile_patterns(self):
        """編譯分析模式"""
        try:
            debug_log(3, "[MemoryAnalyzer] 編譯分析模式...")
            
            # 編譯意圖模式
            for intent, patterns in self.intent_patterns.items():
                compiled_patterns = []
                for pattern in patterns:
                    # 轉換為正則表達式
                    regex_pattern = re.escape(pattern).replace(r'\?', '.')
                    compiled_patterns.append(re.compile(regex_pattern, re.IGNORECASE))
                self._pattern_cache[intent] = compiled_patterns
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 編譯模式失敗: {e}")
    
    def _load_analysis_models(self):
        """載入分析模型"""
        try:
            debug_log(3, "[MemoryAnalyzer] 載入分析模型...")
            
            # 這裡可以載入NLP模型，例如：
            # - jieba分詞
            # - 情感分析模型
            # - 主題分類模型
            
        except Exception as e:
            info_log("WARNING", f"[MemoryAnalyzer] 載入分析模型失敗: {e}")
    
    def analyze_memory(self, memory_entry: MemoryEntry) -> Dict[str, Any]:
        """分析記憶內容"""
        try:
            self.stats["memories_analyzed"] += 1
            
            # 檢查緩存
            cache_key = f"{memory_entry.memory_id}_{memory_entry.created_at.isoformat()}"
            if cache_key in self._analysis_cache:
                return self._analysis_cache[cache_key]
            
            analysis_result = {
                "memory_id": memory_entry.memory_id,
                "analyzed_at": datetime.now(),
                "content_length": len(memory_entry.content),
                "analysis": {}
            }
            
            # 重要性分析
            importance_analysis = self.evaluate_importance(memory_entry.content)
            analysis_result["analysis"]["importance"] = importance_analysis
            
            # 主題分析
            topic_analysis = self.extract_topics(memory_entry.content)
            analysis_result["analysis"]["topics"] = topic_analysis
            
            # 意圖分析
            intent_analysis = self.identify_intents(memory_entry.content)
            analysis_result["analysis"]["intents"] = intent_analysis
            
            # 情感分析
            sentiment_analysis = self.analyze_sentiment(memory_entry.content)
            analysis_result["analysis"]["sentiment"] = sentiment_analysis
            
            # 關鍵字提取
            keywords = self.extract_keywords(memory_entry.content)
            analysis_result["analysis"]["keywords"] = keywords
            
            # 複雜度分析
            complexity = self.analyze_complexity(memory_entry.content)
            analysis_result["analysis"]["complexity"] = complexity
            
            # 緩存結果
            self._analysis_cache[cache_key] = analysis_result
            
            debug_log(4, f"[MemoryAnalyzer] 分析完成: {memory_entry.memory_id}")
            return analysis_result
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 記憶分析失敗: {e}")
            return {"error": str(e)}
    
    def evaluate_importance(self, content: str) -> Dict[str, Any]:
        """評估記憶重要性"""
        try:
            self.stats["importance_evaluations"] += 1
            
            importance_scores = {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0
            }
            
            content_lower = content.lower()
            
            # 關鍵字匹配計分
            for level, keywords in self.importance_keywords.items():
                for keyword in keywords:
                    count = content_lower.count(keyword)
                    importance_scores[level] += count
            
            # 計算總分
            total_score = sum(importance_scores.values())
            
            # 確定重要性等級
            if importance_scores["critical"] > 0:
                importance_level = MemoryImportance.CRITICAL
                confidence = 0.9
            elif importance_scores["high"] > importance_scores["medium"] + importance_scores["low"]:
                importance_level = MemoryImportance.HIGH
                confidence = 0.7
            elif importance_scores["medium"] > importance_scores["low"]:
                importance_level = MemoryImportance.MEDIUM
                confidence = 0.5
            else:
                importance_level = MemoryImportance.LOW
                confidence = 0.3
            
            # 長度調整
            if len(content) > 200:
                confidence += 0.1
            if len(content) < 50:
                confidence -= 0.1
            
            confidence = max(0.0, min(1.0, confidence))
            
            return {
                "level": importance_level,
                "confidence": confidence,
                "scores": importance_scores,
                "total_score": total_score,
                "factors": {
                    "content_length": len(content),
                    "keyword_matches": total_score
                }
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 重要性評估失敗: {e}")
            return {"level": MemoryImportance.MEDIUM, "confidence": 0.0}
    
    def extract_topics(self, content: str) -> Dict[str, Any]:
        """提取主題"""
        try:
            self.stats["topics_extracted"] += 1
            
            topic_scores = {}
            content_lower = content.lower()
            
            # 關鍵字匹配
            for topic, keywords in self.topic_keywords.items():
                score = 0
                matched_keywords = []
                
                for keyword in keywords:
                    count = content_lower.count(keyword)
                    if count > 0:
                        score += count
                        matched_keywords.append(keyword)
                
                if score > 0:
                    topic_scores[topic] = {
                        "score": score,
                        "keywords": matched_keywords,
                        "confidence": min(score / len(keywords), 1.0)
                    }
            
            # 排序主題
            sorted_topics = sorted(topic_scores.items(), 
                                 key=lambda x: x[1]["score"], reverse=True)
            
            # 提取關鍵名詞（簡單版本）
            key_terms = self._extract_key_terms(content)
            
            return {
                "primary_topic": sorted_topics[0][0] if sorted_topics else "general",
                "all_topics": dict(sorted_topics),
                "topic_count": len(topic_scores),
                "key_terms": key_terms,
                "confidence": sorted_topics[0][1]["confidence"] if sorted_topics else 0.0
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 主題提取失敗: {e}")
            return {"primary_topic": "general", "confidence": 0.0}
    
    def identify_intents(self, content: str) -> Dict[str, Any]:
        """識別意圖"""
        try:
            self.stats["intents_identified"] += 1
            
            intent_scores = {}
            
            # 模式匹配
            for intent, patterns in self._pattern_cache.items():
                matches = 0
                for pattern in patterns:
                    if pattern.search(content):
                        matches += 1
                
                if matches > 0:
                    intent_scores[intent] = {
                        "matches": matches,
                        "confidence": min(matches / len(patterns), 1.0)
                    }
            
            # 句法分析
            sentence_analysis = self._analyze_sentence_structure(content)
            
            # 確定主要意圖
            if intent_scores:
                primary_intent = max(intent_scores.items(), 
                                   key=lambda x: x[1]["confidence"])[0]
            else:
                primary_intent = "statement"  # 預設意圖
            
            return {
                "primary_intent": primary_intent,
                "all_intents": intent_scores,
                "intent_count": len(intent_scores),
                "sentence_structure": sentence_analysis,
                "confidence": intent_scores.get(primary_intent, {}).get("confidence", 0.3)
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 意圖識別失敗: {e}")
            return {"primary_intent": "statement", "confidence": 0.0}
    
    def analyze_sentiment(self, content: str) -> Dict[str, Any]:
        """分析情感"""
        try:
            # 簡單情感分析
            positive_words = ["good", "great", "excellent", "success", "satisfied", "happy", "like", "love", 
                             "awesome", "amazing", "fantastic", "wonderful", "perfect", "brilliant"]
            negative_words = ["bad", "terrible", "failed", "problem", "error", "angry", "hate", "dislike", 
                             "awful", "horrible", "disappointed", "frustrated", "annoyed", "upset"]
            neutral_words = ["maybe", "perhaps", "normal", "ordinary", "okay", "fine", "average", "typical"]
            
            content_lower = content.lower()
            
            positive_score = sum(content_lower.count(word) for word in positive_words)
            negative_score = sum(content_lower.count(word) for word in negative_words)
            neutral_score = sum(content_lower.count(word) for word in neutral_words)
            
            total_score = positive_score + negative_score + neutral_score
            
            if total_score == 0:
                sentiment = "neutral"
                confidence = 0.3
            elif positive_score > negative_score:
                sentiment = "positive"
                confidence = positive_score / total_score
            elif negative_score > positive_score:
                sentiment = "negative"
                confidence = negative_score / total_score
            else:
                sentiment = "neutral"
                confidence = 0.5
            
            return {
                "sentiment": sentiment,
                "confidence": min(confidence, 1.0),
                "scores": {
                    "positive": positive_score,
                    "negative": negative_score,
                    "neutral": neutral_score
                }
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 情感分析失敗: {e}")
            return {"sentiment": "neutral", "confidence": 0.0}
    
    def extract_keywords(self, content: str, max_keywords: int = 10) -> List[str]:
        """提取關鍵字"""
        try:
            # 簡單關鍵字提取
            # 移除標點符號，分割成詞
            import string
            
            # 清理文本
            cleaned_content = content.translate(str.maketrans('', '', string.punctuation))
            words = cleaned_content.split()
            
            # 過濾停用詞（英文版本）
            stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", 
                         "by", "from", "up", "about", "into", "through", "during", "before", "after", 
                         "above", "below", "between", "among", "is", "am", "are", "was", "were", "be", 
                         "been", "being", "have", "has", "had", "do", "does", "did", "will", "would", 
                         "could", "should", "may", "might", "must", "can", "this", "that", "these", 
                         "those", "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them"}
            
            # 統計詞頻
            word_freq = Counter()
            for word in words:
                if len(word) > 1 and word.lower() not in stop_words:
                    word_freq[word] += 1
            
            # 返回最常見的關鍵字
            keywords = [word for word, freq in word_freq.most_common(max_keywords)]
            
            return keywords
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 關鍵字提取失敗: {e}")
            return []
    
    def extract_topic(self, content: str) -> str:
        """提取主要主題"""
        try:
            topics_result = self.extract_topics(content)
            
            if topics_result and "all_topics" in topics_result:
                topics = topics_result["all_topics"]
                if topics:
                    # 返回置信度最高的主題
                    return max(topics.items(), key=lambda x: x[1]["score"])[0]
            
            return "general"
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 主題提取失敗: {e}")
            return "unknown"
    
    def generate_summary(self, content: str, max_length: int = 100) -> str:
        """生成內容摘要"""
        try:
            # 簡單摘要生成 - 取前幾句
            sentences = content.replace('.', '.|').replace('!', '!|').replace('?', '?|').split('|')
            sentences = [s.strip() for s in sentences if s.strip()]
            
            summary = ""
            for sentence in sentences:
                if len(summary + sentence) <= max_length:
                    summary += sentence
                    if not sentence.endswith(('.', '!', '?')):
                        summary += "."
                else:
                    break
            
            if not summary and content:
                # 如果沒有生成摘要，直接截取
                summary = content[:max_length]
                if len(content) > max_length:
                    summary += "..."
            
            return summary or "No content summary"
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 摘要生成失敗: {e}")
            return "Summary generation failed"
            return keywords
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 關鍵字提取失敗: {e}")
            return []
    
    def analyze_complexity(self, content: str) -> Dict[str, Any]:
        """分析內容複雜度"""
        try:
            # 簡單複雜度指標
            char_count = len(content)
            word_count = len(content.split())
            sentence_count = content.count('.') + content.count('!') + content.count('?')
            
            if sentence_count == 0:
                sentence_count = 1  # 避免除零
            
            avg_word_length = char_count / word_count if word_count > 0 else 0
            avg_sentence_length = word_count / sentence_count
            
            # 複雜度評分
            complexity_score = 0
            if avg_sentence_length > 20:
                complexity_score += 2
            elif avg_sentence_length > 10:
                complexity_score += 1
            
            if avg_word_length > 3:
                complexity_score += 1
            
            if word_count > 100:
                complexity_score += 1
            
            complexity_level = "simple"
            if complexity_score >= 3:
                complexity_level = "complex"
            elif complexity_score >= 2:
                complexity_level = "medium"
            
            return {
                "level": complexity_level,
                "score": complexity_score,
                "metrics": {
                    "char_count": char_count,
                    "word_count": word_count,
                    "sentence_count": sentence_count,
                    "avg_word_length": avg_word_length,
                    "avg_sentence_length": avg_sentence_length
                }
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 複雜度分析失敗: {e}")
            return {"level": "simple", "score": 0}
    
    def _extract_key_terms(self, content: str) -> List[str]:
        """提取關鍵術語"""
        try:
            # 簡單的關鍵術語提取
            # 尋找可能的專業術語或重要概念
            
            # 大寫詞組（可能是專有名詞）
            import re
            capitalized_terms = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', content)
            
            # 英文術語
            english_terms = re.findall(r'\b[a-zA-Z]{3,}\b', content)
            
            # 數字相關術語
            number_terms = re.findall(r'\d+[a-zA-Z]+|\b\d+\.\d+\b', content)
            
            all_terms = capitalized_terms + english_terms + number_terms
            
            # 去重並限制數量
            unique_terms = list(set(all_terms))[:10]
            
            return unique_terms
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 關鍵術語提取失敗: {e}")
            return []
    
    def _analyze_sentence_structure(self, content: str) -> Dict[str, Any]:
        """分析句子結構"""
        try:
            # 句法分析
            questions = content.count('?')
            exclamations = content.count('!')
            statements = content.count('.')
            
            total_sentences = questions + exclamations + statements
            if total_sentences == 0:
                total_sentences = 1
            
            return {
                "questions": questions,
                "exclamations": exclamations,
                "statements": statements,
                "total": total_sentences,
                "question_ratio": questions / total_sentences,
                "exclamation_ratio": exclamations / total_sentences,
                "statement_ratio": statements / total_sentences
            }
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 句子結構分析失敗: {e}")
            return {"total": 1, "question_ratio": 0, "exclamation_ratio": 0, "statement_ratio": 1}
    
    def analyze_memory_patterns(self, memories: List[MemoryEntry], 
                               identity_token: str) -> Dict[str, Any]:
        """分析記憶模式"""
        try:
            if not memories:
                return {"error": "無記憶數據"}
            
            # 時間模式分析
            time_patterns = self._analyze_time_patterns(memories)
            
            # 主題模式分析
            topic_patterns = self._analyze_topic_patterns(memories)
            
            # 重要性分布
            importance_distribution = self._analyze_importance_distribution(memories)
            
            # 互動模式
            interaction_patterns = self._analyze_interaction_patterns(memories)
            
            return {
                "identity_token": identity_token,
                "analysis_period": {
                    "start": min(m.created_at for m in memories),
                    "end": max(m.created_at for m in memories),
                    "total_memories": len(memories)
                },
                "time_patterns": time_patterns,
                "topic_patterns": topic_patterns,
                "importance_distribution": importance_distribution,
                "interaction_patterns": interaction_patterns,
                "analyzed_at": datetime.now()
            }
            
        except Exception as e:
            error_log(f"[MemoryAnalyzer] 記憶模式分析失敗: {e}")
            return {"error": str(e)}
    
    def _analyze_time_patterns(self, memories: List[MemoryEntry]) -> Dict[str, Any]:
        """分析時間模式"""
        try:
            # 按小時統計
            hour_counts = defaultdict(int)
            day_counts = defaultdict(int)
            
            for memory in memories:
                hour_counts[memory.created_at.hour] += 1
                day_counts[memory.created_at.strftime('%A')] += 1
            
            # 找出活躍時段
            peak_hour = max(hour_counts.items(), key=lambda x: x[1])[0] if hour_counts else 0
            peak_day = max(day_counts.items(), key=lambda x: x[1])[0] if day_counts else "Monday"
            
            return {
                "hourly_distribution": dict(hour_counts),
                "daily_distribution": dict(day_counts),
                "peak_hour": peak_hour,
                "peak_day": peak_day,
                "total_active_hours": len(hour_counts),
                "total_active_days": len(day_counts)
            }
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 時間模式分析失敗: {e}")
            return {}
    
    def _analyze_topic_patterns(self, memories: List[MemoryEntry]) -> Dict[str, Any]:
        """分析主題模式"""
        try:
            topic_counts = defaultdict(int)
            topic_evolution = []
            
            for memory in memories:
                if memory.topic:
                    topic_counts[memory.topic] += 1
                    topic_evolution.append({
                        "timestamp": memory.created_at,
                        "topic": memory.topic
                    })
            
            # 主要興趣主題
            main_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            
            return {
                "topic_distribution": dict(topic_counts),
                "main_topics": main_topics,
                "topic_diversity": len(topic_counts),
                "topic_evolution": topic_evolution[-20:]  # 最近20個主題演變
            }
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 主題模式分析失敗: {e}")
            return {}
    
    def _analyze_importance_distribution(self, memories: List[MemoryEntry]) -> Dict[str, Any]:
        """分析重要性分布"""
        try:
            importance_counts = defaultdict(int)
            
            for memory in memories:
                importance_counts[memory.importance.value] += 1
            
            total = len(memories)
            importance_ratios = {k: v/total for k, v in importance_counts.items()}
            
            return {
                "distribution": dict(importance_counts),
                "ratios": importance_ratios,
                "total_memories": total
            }
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 重要性分布分析失敗: {e}")
            return {}
    
    def _analyze_interaction_patterns(self, memories: List[MemoryEntry]) -> Dict[str, Any]:
        """分析互動模式"""
        try:
            # 會話模式
            session_counts = defaultdict(int)
            memory_types = defaultdict(int)
            
            for memory in memories:
                if memory.session_id:
                    session_counts[memory.session_id] += 1
                memory_types[memory.memory_type.value] += 1
            
            # 平均會話長度
            avg_session_length = sum(session_counts.values()) / len(session_counts) if session_counts else 0
            
            return {
                "session_distribution": dict(session_counts),
                "memory_type_distribution": dict(memory_types),
                "total_sessions": len(session_counts),
                "avg_session_length": avg_session_length,
                "memory_type_diversity": len(memory_types)
            }
            
        except Exception as e:
            debug_log(1, f"[MemoryAnalyzer] 互動模式分析失敗: {e}")
            return {}
    
    def clear_cache(self):
        """清理分析緩存"""
        self._analysis_cache.clear()
        debug_log(3, "[MemoryAnalyzer] 分析緩存已清理")
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        return {
            **self.stats,
            "cached_analyses": len(self._analysis_cache),
            "compiled_patterns": len(self._pattern_cache)
        }
