# -*- coding: utf-8 -*-
"""
LLM Mock服務 - 用於MEM模組測試

提供模擬的LLM Prompt API功能，支持：
1. 記憶指令處理
2. 對話回應生成
3. 記憶更新建議
4. 用戶特質分析
"""

import json
import random
from typing import Dict, Any, List, Optional
from datetime import datetime
from dataclasses import dataclass


@dataclass
class MockLLMResponse:
    """模擬LLM回應結構"""
    response_text: str
    confidence: float
    processing_time: float
    memory_updates: List[Dict[str, Any]]
    user_model_updates: Dict[str, Any]
    conversation_metadata: Dict[str, Any]


class LLMMockService:
    """LLM模擬服務"""
    
    def __init__(self):
        """初始化Mock服務"""
        self.response_templates = {
            "learning_guidance": [
                "根據您的學習目標，我建議從基礎概念開始...",
                "考慮到您的學習風格，推薦您採用實作導向的方法...",
                "基於您的進度，接下來可以專注於..."
            ],
            "technical_explanation": [
                "讓我為您詳細解釋這個概念...",
                "這個技術的核心原理是...",
                "從實作的角度來看..."
            ],
            "problem_solving": [
                "針對您遇到的問題，我們可以這樣解決...",
                "這種情況下，建議您嘗試以下方法...",
                "讓我們一步步分析這個問題..."
            ],
            "general_conversation": [
                "我理解您的想法...",
                "這是一個很好的問題...",
                "讓我們繼續深入討論..."
            ]
        }
        
        self.memory_update_patterns = {
            "user_preference": [
                "偏好視覺化學習方式",
                "喜歡實作導向的教學",
                "傾向於系統化學習路徑"
            ],
            "interaction_pattern": [
                "經常提出深入的問題",
                "喜歡獲得詳細的解釋",
                "習慣驗證理解程度"
            ],
            "progress_marker": [
                "完成了基礎概念學習",
                "掌握了核心技能",
                "準備進入下一階段"
            ]
        }
    
    def process_memory_instruction(self, memory_instruction: Dict[str, Any]) -> MockLLMResponse:
        """處理記憶指令並生成回應"""
        
        # 解析記憶指令
        context_summary = memory_instruction.get("context_summary", "")
        key_facts = memory_instruction.get("key_facts", [])
        user_preferences = memory_instruction.get("user_preferences", {})
        conversation_history = memory_instruction.get("conversation_history", [])
        
        # 根據上下文選擇回應類型
        response_type = self._determine_response_type(context_summary, key_facts)
        
        # 生成主要回應
        response_text = self._generate_response_text(response_type, context_summary, key_facts)
        
        # 生成記憶更新建議
        memory_updates = self._generate_memory_updates(response_type, key_facts)
        
        # 生成用戶模型更新
        user_model_updates = self._generate_user_model_updates(user_preferences, conversation_history)
        
        # 生成對話元數據
        conversation_metadata = self._generate_conversation_metadata(response_type)
        
        return MockLLMResponse(
            response_text=response_text,
            confidence=random.uniform(0.8, 0.95),
            processing_time=random.uniform(0.5, 2.0),
            memory_updates=memory_updates,
            user_model_updates=user_model_updates,
            conversation_metadata=conversation_metadata
        )
    
    def generate_conversation_summary(self, conversation_text: str) -> Dict[str, Any]:
        """生成對話總結"""
        
        # 分析對話內容
        lines = conversation_text.strip().split('\n')
        user_messages = [line for line in lines if line.startswith('用戶:') or line.startswith('使用者:')]
        system_messages = [line for line in lines if line.startswith('系統:') or line.startswith('助手:')]
        
        # 提取關鍵主題
        topics = self._extract_topics_from_conversation(conversation_text)
        
        # 生成總結
        summary = {
            "conversation_summary": f"本次對話包含 {len(user_messages)} 個用戶訊息和 {len(system_messages)} 個系統回應",
            "main_topics": topics,
            "key_outcomes": [
                "用戶獲得了相關資訊",
                "建立了學習路徑",
                "確認了下一步行動"
            ],
            "emotional_journey": [
                {"stage": "開始", "emotion": "好奇", "confidence": 0.8},
                {"stage": "中期", "emotion": "專注", "confidence": 0.9},
                {"stage": "結束", "emotion": "滿意", "confidence": 0.85}
            ],
            "follow_up_suggestions": [
                "提供更多相關資源",
                "安排實作練習",
                "制定學習時程表"
            ],
            "memory_importance": "medium"
        }
        
        return summary
    
    def analyze_user_characteristics(self, user_interactions: List[str]) -> Dict[str, Any]:
        """分析用戶特質"""
        
        # 模擬用戶特質分析
        characteristics = {
            "learning_style": {
                "primary": random.choice(["visual", "auditory", "kinesthetic", "reading"]),
                "secondary": random.choice(["systematic", "exploratory", "social", "independent"]),
                "confidence": 0.75
            },
            "interaction_patterns": {
                "question_frequency": "high" if len(user_interactions) > 5 else "medium",
                "detail_preference": random.choice(["high", "medium", "low"]),
                "pace_preference": random.choice(["fast", "steady", "slow"])
            },
            "technical_proficiency": {
                "current_level": random.choice(["beginner", "intermediate", "advanced"]),
                "learning_curve": random.choice(["steep", "gradual", "variable"]),
                "confidence": 0.8
            },
            "motivation_indicators": {
                "engagement_level": "high",
                "goal_orientation": "strong",
                "persistence": "good"
            }
        }
        
        return characteristics
    
    def _determine_response_type(self, context_summary: str, key_facts: List[Dict]) -> str:
        """根據上下文確定回應類型"""
        
        context_lower = context_summary.lower()
        
        if any(word in context_lower for word in ["學習", "教學", "課程", "指導"]):
            return "learning_guidance"
        elif any(word in context_lower for word in ["技術", "程式", "代碼", "演算法"]):
            return "technical_explanation"
        elif any(word in context_lower for word in ["問題", "錯誤", "解決", "debugging"]):
            return "problem_solving"
        else:
            return "general_conversation"
    
    def _generate_response_text(self, response_type: str, context_summary: str, key_facts: List[Dict]) -> str:
        """生成回應文本"""
        
        base_response = random.choice(self.response_templates[response_type])
        
        # 根據關鍵事實客製化回應
        if key_facts:
            fact_info = f"考慮到您提到的{len(key_facts)}個重要點，"
            base_response = fact_info + base_response
        
        # 添加具體建議
        specific_suggestions = self._generate_specific_suggestions(response_type)
        
        full_response = f"{base_response}\n\n具體建議：\n{specific_suggestions}"
        
        return full_response
    
    def _generate_specific_suggestions(self, response_type: str) -> str:
        """生成具體建議"""
        
        suggestions_map = {
            "learning_guidance": [
                "1. 制定每日學習計劃",
                "2. 尋找實作專案機會",
                "3. 加入學習社群",
                "4. 定期複習已學內容"
            ],
            "technical_explanation": [
                "1. 閱讀官方文檔",
                "2. 實際編寫代碼測試",
                "3. 查看範例程式",
                "4. 與其他開發者討論"
            ],
            "problem_solving": [
                "1. 分析錯誤訊息",
                "2. 檢查代碼邏輯",
                "3. 搜尋相關解決方案",
                "4. 嘗試除錯工具"
            ],
            "general_conversation": [
                "1. 深入思考這個話題",
                "2. 尋找更多相關資訊",
                "3. 與他人分享看法",
                "4. 實際應用所學知識"
            ]
        }
        
        suggestions = suggestions_map.get(response_type, suggestions_map["general_conversation"])
        return "\n".join(suggestions)
    
    def _generate_memory_updates(self, response_type: str, key_facts: List[Dict]) -> List[Dict[str, Any]]:
        """生成記憶更新建議"""
        
        updates = []
        
        # 基於回應類型的更新
        if response_type == "learning_guidance":
            updates.append({
                "type": "user_preference",
                "content": random.choice(self.memory_update_patterns["user_preference"]),
                "importance": "medium",
                "category": "learning_style"
            })
        
        # 基於互動模式的更新
        updates.append({
            "type": "interaction_pattern",
            "content": random.choice(self.memory_update_patterns["interaction_pattern"]),
            "importance": "low",
            "category": "behavior"
        })
        
        # 基於進度的更新
        if len(key_facts) > 2:
            updates.append({
                "type": "progress_marker",
                "content": random.choice(self.memory_update_patterns["progress_marker"]),
                "importance": "high",
                "category": "achievement"
            })
        
        return updates
    
    def _generate_user_model_updates(self, user_preferences: Dict, conversation_history: List[Dict]) -> Dict[str, Any]:
        """生成用戶模型更新"""
        
        return {
            "learning_progress": {
                "current_topic": "Python基礎",
                "completion_percentage": random.randint(20, 80),
                "next_milestone": "完成第一個專案"
            },
            "engagement_metrics": {
                "session_length": f"{random.randint(15, 60)}分鐘",
                "question_count": len(conversation_history),
                "interaction_quality": random.choice(["high", "medium", "low"])
            },
            "adaptation_suggestions": {
                "pace_adjustment": random.choice(["maintain", "accelerate", "slow_down"]),
                "content_difficulty": random.choice(["increase", "maintain", "decrease"]),
                "teaching_method": random.choice(["more_examples", "more_theory", "more_practice"])
            }
        }
    
    def _generate_conversation_metadata(self, response_type: str) -> Dict[str, Any]:
        """生成對話元數據"""
        
        return {
            "response_type": response_type,
            "confidence_level": "high",
            "estimated_user_satisfaction": random.uniform(0.7, 0.95),
            "follow_up_recommended": True,
            "topics_covered": [response_type.replace("_", " ")],
            "session_quality": "good"
        }
    
    def _extract_topics_from_conversation(self, conversation_text: str) -> List[str]:
        """從對話中提取主題"""
        
        # 簡單的關鍵詞提取
        keywords_map = {
            "學習": ["學習", "教學", "課程", "指導"],
            "程式設計": ["程式", "代碼", "編程", "開發"],
            "Python": ["Python", "python", "py"],
            "AI": ["AI", "人工智慧", "機器學習", "深度學習"],
            "專案": ["專案", "項目", "實作", "練習"]
        }
        
        extracted_topics = []
        text_lower = conversation_text.lower()
        
        for topic, keywords in keywords_map.items():
            if any(keyword.lower() in text_lower for keyword in keywords):
                extracted_topics.append(topic)
        
        return extracted_topics if extracted_topics else ["一般對話"]


# 便利函數
def create_mock_llm_service() -> LLMMockService:
    """創建Mock LLM服務實例"""
    return LLMMockService()


def simulate_llm_processing(memory_instruction: Dict[str, Any]) -> Dict[str, Any]:
    """模擬LLM處理過程"""
    
    service = create_mock_llm_service()
    response = service.process_memory_instruction(memory_instruction)
    
    return {
        "response_text": response.response_text,
        "confidence": response.confidence,
        "memory_updates": response.memory_updates,
        "user_model_updates": response.user_model_updates,
        "metadata": response.conversation_metadata
    }


if __name__ == "__main__":
    # 測試Mock服務
    test_instruction = {
        "context_summary": "用戶正在學習Python程式設計",
        "key_facts": [
            {"content": "用戶是初學者", "importance": "high"},
            {"content": "偏好實作學習", "importance": "medium"}
        ],
        "user_preferences": {"learning_style": "hands_on"},
        "conversation_history": [
            {"content": "想學習Python", "timestamp": "2024-01-01"}
        ]
    }
    
    result = simulate_llm_processing(test_instruction)
    print("LLM Mock測試結果:")
    print(json.dumps(result, indent=2, ensure_ascii=False))