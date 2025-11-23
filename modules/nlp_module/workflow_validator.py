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
    
    # 相似度閾值（放寬）
    HIGH_SIMILARITY_THRESHOLD = 0.45  # 明確匹配（從 0.6 降至 0.45）
    LOW_SIMILARITY_THRESHOLD = 0.15   # 極低相似度（從 0.4 降至 0.15）
    CRITICAL_THRESHOLD = 0.05         # 完全不相關才降級
    
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
        3. 根據匹配到的工作流校正 work_mode (direct/background)
        4. 只有當 confidence 降到很低時才轉為 CHAT
        """
        seg_text = segment['text'].lower()
        max_similarity = 0.0
        best_match_workflow = None
        best_match_workflow_def = None
        
        # 計算與每個工作流名稱和描述的相似度
        for workflow_name, workflow_def in self.workflows.items():
            # 將 workflow_name 轉換為可讀形式（如 drop_and_read → drop and read）
            readable_name = workflow_name.replace('_', ' ')
            
            # 計算與工作流名稱的相似度
            name_similarity = self._calculate_similarity(seg_text, readable_name)
            
            # 計算與工作流描述的相似度（如果有）
            desc_similarity = 0.0
            if 'description' in workflow_def:
                description = workflow_def['description'].lower()
                desc_similarity = self._calculate_similarity(seg_text, description)
            
            # 取名稱和描述相似度的最大值
            similarity = max(name_similarity, desc_similarity)
            
            if similarity > max_similarity:
                max_similarity = similarity
                best_match_workflow = workflow_name
                best_match_workflow_def = workflow_def
        
        debug_log(3, f"[WorkflowValidator] WORK 分段 '{segment['text']}' 最佳匹配: {best_match_workflow} (相似度={max_similarity:.3f})")
        
        original_confidence = segment['confidence']
        original_intent = segment['intent']
        
        # 🔧 檢查是否有關鍵詞強匹配（即使相似度低，只要有關鍵詞就信任）
        has_strong_keyword = self._has_strong_keyword_match(seg_text, best_match_workflow_def)
        
        # 根據相似度調整 confidence
        if max_similarity >= self.HIGH_SIMILARITY_THRESHOLD or has_strong_keyword:
            # 高相似度或關鍵詞匹配 → 提升 confidence 15%
            new_confidence = min(original_confidence * 1.15, 0.999)
            segment['confidence'] = round(new_confidence, 3)
            match_reason = "關鍵詞強匹配" if has_strong_keyword else "高相似度"
            debug_log(3, f"[WorkflowValidator] {match_reason}，confidence 提升: {original_confidence:.3f} → {segment['confidence']}")
            
            # ✅ 校正 work_mode：使用匹配到的工作流的 work_mode
            if best_match_workflow_def and 'work_mode' in best_match_workflow_def:
                matched_work_mode = best_match_workflow_def['work_mode']
                
                # 映射到 intent
                if matched_work_mode == 'background':
                    corrected_intent = 'background_work'
                else:  # 'direct' or other
                    corrected_intent = 'direct_work'
                
                if corrected_intent != original_intent:
                    segment['intent'] = corrected_intent
                    
                    # ✅ 同步更新 metadata 中的 work_mode
                    if 'metadata' not in segment:
                        segment['metadata'] = {}
                    
                    original_work_mode = segment['metadata'].get('work_mode', 'unknown')
                    segment['metadata']['work_mode'] = matched_work_mode  # 🔧 校正 work_mode
                    
                    # 添加校正標記到 metadata
                    segment['metadata']['workflow_mode_corrected'] = True
                    segment['metadata']['original_intent'] = original_intent
                    segment['metadata']['original_work_mode'] = original_work_mode
                    segment['metadata']['corrected_intent'] = corrected_intent
                    segment['metadata']['corrected_work_mode'] = matched_work_mode
                    segment['metadata']['matched_workflow'] = best_match_workflow
                    
                    debug_log(2, f"[WorkflowValidator] 🔧 工作模式校正: {original_intent}(work_mode={original_work_mode}) → {corrected_intent}(work_mode={matched_work_mode}) [匹配工作流: {best_match_workflow}]")
                else:
                    # 模式一致，但仍然更新 metadata 中的 work_mode 確保同步
                    if 'metadata' not in segment:
                        segment['metadata'] = {}
                    segment['metadata']['work_mode'] = matched_work_mode  # 確保 work_mode 正確
                    segment['metadata']['matched_workflow'] = best_match_workflow
                    debug_log(3, f"[WorkflowValidator] 工作模式一致: {original_intent}(work_mode={matched_work_mode}) [匹配工作流: {best_match_workflow}]")
        
        elif max_similarity < self.LOW_SIMILARITY_THRESHOLD:
            # 低相似度 → 降低 confidence 30%
            new_confidence = original_confidence * 0.7
            segment['confidence'] = round(new_confidence, 3)
            
            # 只有當降低後的 confidence < CHAT 的典型 confidence (0.8) 時才轉換
            CHAT_THRESHOLD = 0.8
            if segment['confidence'] < CHAT_THRESHOLD:
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
            # 中等相似度 → 保持不變，但嘗試記錄匹配信息
            if best_match_workflow_def:
                if 'metadata' not in segment:
                    segment['metadata'] = {}
                segment['metadata']['potential_workflow'] = best_match_workflow
                segment['metadata']['similarity'] = round(max_similarity, 3)
            debug_log(3, f"[WorkflowValidator] 中等相似度，保持 WORK 意圖 (confidence={segment['confidence']})")
        
        return segment
    
    def _has_strong_keyword_match(self, text: str, workflow_def: Optional[Dict[str, Any]]) -> bool:
        """
        檢查文本是否包含工作流的強關鍵詞
        
        強關鍵詞：明確指向特定工作流的詞彙
        例如："weather" → get_weather, "translate" → translate_document
        """
        if not workflow_def:
            return False
        
        # 工作流強關鍵詞映射
        strong_keywords = {
            'get_weather': {'weather', 'forecast', 'temperature', 'climate'},
            'news_summary': {'news', 'headlines', 'articles'},
            'translate_document': {'translate', 'translation'},
            'get_world_time': {'time', 'clock', 'timezone'},
            'drop_and_read': {'read', 'file', 'document', 'drop'},
            'summarize_and_tag': {'summarize', 'summary', 'tag', 'tags'},
            'clipboard_tracker': {'clipboard', 'history', 'copy'},
            'clean_trash_bin': {'trash', 'bin', 'clean', 'garbage'},
            'code_analysis': {'code', 'analysis', 'analyze', 'quality'},
            'ocr_image': {'ocr', 'image', 'recognize', 'text'},
        }
        
        # 從工作流定義中獲取名稱
        workflow_name = workflow_def.get('name', '')
        
        if workflow_name in strong_keywords:
            keywords = strong_keywords[workflow_name]
            text_words = set(text.lower().split())
            
            # 只要有一個強關鍵詞匹配就返回 True
            if text_words & keywords:
                debug_log(3, f"[WorkflowValidator] 🎯 強關鍵詞匹配: {text_words & keywords} → {workflow_name}")
                return True
        
        return False
    
    def _has_any_workflow_keyword(self, text: str) -> bool:
        """
        檢查文本是否包含任何工作流相關的關鍵詞
        用於判斷是否應該保持 WORK 意圖
        """
        # 通用工作流關鍵詞（涵蓋大部分工作流場景）
        general_workflow_keywords = {
            # 動作詞
            'read', 'write', 'create', 'generate', 'translate', 'analyze',
            'check', 'get', 'show', 'display', 'search', 'find', 'clean',
            'delete', 'remove', 'save', 'archive', 'backup', 'copy',
            'summarize', 'tag', 'recognize', 'extract',
            # 對象詞
            'file', 'document', 'image', 'code', 'script', 'weather',
            'news', 'time', 'clipboard', 'trash', 'bin', 'media', 'music',
        }
        
        text_words = set(text.lower().split())
        return bool(text_words & general_workflow_keywords)
    
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
            'time': {'clock', 'hour', 'minute', 'world', 'get'},
            'clock': {'time'},
            'world': {'time', 'global', 'international'},
            'get': {'show', 'display', 'check', 'time', 'weather'},
            'weather': {'forecast', 'temperature', 'climate', 'get', 'check', 'show'},  # 🔧 擴展
            'forecast': {'weather', 'temperature', 'climate'},
            'temperature': {'weather', 'forecast', 'climate'},
            'climate': {'weather', 'forecast', 'temperature'},
            'tell': {'show', 'display', 'get', 'check'},  # 🔧 新增："tell me" = "show me"
            'about': {'regarding', 'concerning'},  # 🔧 新增
            'translate': {'translation', 'convert', 'document'},
            'clean': {'clear', 'remove', 'delete'},
            'trash': {'bin', 'recycle', 'garbage', 'clean'},
            'bin': {'trash', 'recycle', 'clean'},
            'script': {'code', 'program', 'file'},
            'backup': {'archive', 'save', 'generate'},
            'generate': {'create', 'make', 'backup'},
            'library': {'music', 'media', 'collection'},
            'news': {'headlines', 'summary', 'articles', 'latest', 'show'},
            'headlines': {'news', 'summary', 'latest'},
            'summary': {'news', 'headlines', 'summarize'},
            'latest': {'news', 'recent', 'new'},
            'show': {'display', 'get', 'check', 'news', 'tell'},  # 🔧 擴展
            'check': {'show', 'display', 'get', 'weather', 'tell'},  # 🔧 擴展
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
