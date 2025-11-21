#!/usr/bin/env python3
"""
工作流驗證器
對 WORK 意圖進行二次驗證，確保其可信度
"""

from typing import List, Dict, Any, Optional
from pathlib import Path
import yaml
from utils.debug_helper import debug_log, info_log, error_log


class WorkflowValidator:
    """工作流驗證器"""
    
    # 相似度閾值
    HIGH_SIMILARITY_THRESHOLD = 0.6  # 明確匹配
    LOW_SIMILARITY_THRESHOLD = 0.4   # 低於此值轉為 CHAT
    
    def __init__(self, workflow_definitions_path: Optional[str] = None):
        """
        初始化驗證器
        
        Args:
            workflow_definitions_path: workflow_definitions.yaml 路徑
        """
        if workflow_definitions_path is None:
            # 默認路徑
            workflow_definitions_path = "modules/sys_module/workflows/workflow_definitions.yaml"
        
        self.workflow_definitions_path = Path(workflow_definitions_path)
        self.workflows: Dict[str, Dict[str, Any]] = {}
        self._load_workflow_definitions()
    
    def _load_workflow_definitions(self):
        """載入工作流定義"""
        try:
            if not self.workflow_definitions_path.exists():
                error_log(f"[WorkflowValidator] 工作流定義文件不存在: {self.workflow_definitions_path}")
                return
            
            with open(self.workflow_definitions_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if 'workflows' in data:
                self.workflows = data['workflows']
                info_log(f"[WorkflowValidator] 成功載入 {len(self.workflows)} 個工作流定義")
            else:
                error_log("[WorkflowValidator] workflow_definitions.yaml 格式錯誤")
        
        except Exception as e:
            error_log(f"[WorkflowValidator] 載入工作流定義失敗: {e}")
    
    def validate(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        驗證並調整 WORK 意圖分段
        
        Args:
            segments: 後處理後的分段列表
        
        Returns:
            驗證後的分段列表
        """
        if not self.workflows:
            debug_log(2, "[WorkflowValidator] 無工作流定義，跳過驗證")
            return segments
        
        validated = []
        
        for seg in segments:
            if seg['intent'] in ['direct_work', 'background_work']:
                # 對 WORK 意圖進行驗證
                validated_seg = self._validate_work_segment(seg)
                validated.append(validated_seg)
            else:
                # 非 WORK 意圖，直接保留
                validated.append(seg)
        
        return validated
    
    def _validate_work_segment(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        """
        驗證單個 WORK 分段
        
        邏輯：
        1. 計算與所有工作流 name 的相似度（名稱更簡潔明確）
        2. 根據相似度調整 confidence
        3. 只有當 confidence 降到很低時才轉為 CHAT
        """
        seg_text = segment['text'].lower()
        max_similarity = 0.0
        best_match_workflow = None
        
        # 計算與每個工作流名稱的相似度
        for workflow_name, workflow_def in self.workflows.items():
            # 將 workflow_name 轉換為可讀形式（如 drop_and_read → drop and read）
            readable_name = workflow_name.replace('_', ' ')
            
            # 計算相似度
            similarity = self._calculate_similarity(seg_text, readable_name)
            
            if similarity > max_similarity:
                max_similarity = similarity
                best_match_workflow = workflow_name
        
        debug_log(3, f"[WorkflowValidator] WORK 分段 '{segment['text']}' 最佳匹配: {best_match_workflow} (相似度={max_similarity:.3f})")
        
        original_confidence = segment['confidence']
        
        # 根據相似度調整 confidence
        if max_similarity >= self.HIGH_SIMILARITY_THRESHOLD:
            # 高相似度 → 提升 confidence 10%
            new_confidence = min(original_confidence * 1.1, 0.999)
            segment['confidence'] = round(new_confidence, 3)
            debug_log(3, f"[WorkflowValidator] 高相似度匹配，confidence 提升: {original_confidence:.3f} → {segment['confidence']}")
        
        elif max_similarity < self.LOW_SIMILARITY_THRESHOLD:
            # 低相似度 → 降低 confidence 30%
            new_confidence = original_confidence * 0.7
            segment['confidence'] = round(new_confidence, 3)
            
            # 只有當降低後的 confidence < CHAT 的典型 confidence (0.8) 時才轉換
            CHAT_THRESHOLD = 0.8
            if segment['confidence'] < CHAT_THRESHOLD:
                original_intent = segment['intent']
                segment['intent'] = 'chat'
                
                # 添加降級標記到 metadata
                if 'metadata' not in segment:
                    segment['metadata'] = {}
                segment['metadata']['degraded_from_work'] = True
                segment['metadata']['original_intent'] = original_intent
                segment['metadata']['degradation_reason'] = 'no_matching_workflow'
                
                debug_log(2, f"[WorkflowValidator] 低相似度 + 低信心度，WORK → CHAT (confidence={segment['confidence']}) [標記為降級]")
            else:
                debug_log(3, f"[WorkflowValidator] 低相似度但信心度足夠，保持 WORK (confidence={segment['confidence']})")
        
        else:
            # 中等相似度 → 保持不變
            debug_log(3, f"[WorkflowValidator] 中等相似度，保持 WORK 意圖 (confidence={segment['confidence']})")
        
        return segment
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        計算兩段文本的相似度（改進版：適合短文本與工作流名稱比對）
        
        使用改進的匹配策略：
        1. 直接詞匹配
        2. 同義詞/相關詞匹配
        3. 計算用戶輸入詞的覆蓋率
        
        Args:
            text1: 用戶輸入（短文本）
            text2: 工作流名稱（短文本）
        
        Returns:
            相似度 (0.0 - 1.0)
        """
        # 停用詞列表
        stop_words = {
            'a', 'an', 'the', 'for', 'to', 'with', 'using', 'in', 'on', 'at',
            'by', 'from', 'of', 'and', 'or', 'but', 'is', 'are', 'was', 'were',
            'this', 'that', 'these', 'those', 'my', 'your', 'me', 'you', 'it',
            'some', 'please'
        }
        
        # 同義詞/相關詞映射（擴展版）
        synonyms = {
            'music': {'media', 'audio', 'song', 'playback', 'play'},
            'media': {'music', 'audio', 'video', 'playback'},
            'play': {'playback', 'start', 'run', 'music', 'media'},
            'playback': {'play', 'music', 'media'},
            'file': {'document', 'doc'},
            'document': {'file', 'doc'},
            'archive': {'save', 'store', 'backup'},
            'time': {'clock', 'hour', 'minute', 'world', 'get'},  # "get time" = "get_world_time"
            'clock': {'time'},
            'world': {'time', 'global', 'international'},
            'get': {'show', 'display', 'check', 'time'},
            'weather': {'forecast', 'temperature', 'climate'},
            'translate': {'translation', 'convert', 'document'},
            'clean': {'clear', 'remove', 'delete'},
            'trash': {'bin', 'recycle', 'garbage', 'clean'},
            'bin': {'trash', 'recycle', 'clean'},
            'script': {'code', 'program', 'file'},
            'backup': {'archive', 'save', 'generate'},
            'generate': {'create', 'make', 'backup'},
            'library': {'music', 'media', 'collection'}  # "music library"
        }
        
        # 分詞並移除停用詞
        words1 = set(w for w in text1.split() if w not in stop_words and len(w) > 2)
        words2 = set(w for w in text2.split() if w not in stop_words and len(w) > 2)
        
        if not words1 or not words2:
            return 0.0
        
        # 直接匹配
        direct_matches = words1 & words2
        
        # 同義詞匹配
        synonym_matches = set()
        for w1 in words1:
            if w1 in synonyms:
                # 檢查是否有同義詞在 words2 中
                if words2 & synonyms[w1]:
                    synonym_matches.add(w1)
        
        # 總匹配數
        total_matches = len(direct_matches) + len(synonym_matches)
        
        # 計算覆蓋率
        coverage = total_matches / len(words1) if len(words1) > 0 else 0.0
        
        # 如果覆蓋率高，給予額外權重
        if coverage >= 0.5:  # 至少一半的詞匹配
            match_bonus = min(total_matches * 0.1, 0.3)  # 最多+0.3
            similarity = min(coverage + match_bonus, 1.0)
        else:
            similarity = coverage * 0.8  # 覆蓋率低，降低信心
        
        return similarity
