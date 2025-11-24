"""
Emotion Mapper - 將 Status Manager 的情感值映射到 IndexTTS 的 8D 情感向量

情感維度映射:
Status Manager (Plutchik's Wheel) → IndexTTS 8D Vector
- happiness → happy (0)
- anger → angry (1)
- sadness → sad (2)
- fear → afraid (3)
- disgust → disgusted (4)
- (sadness + low energy) → melancholic (5)
- surprise → surprised (6)
- (trust + calm) → calm (7)
"""

from typing import List, Dict, Optional
from utils.debug_helper import debug_log


class EmotionMapper:
    """
    將 Status Manager 的 Plutchik 情感輪映射到 IndexTTS 的 8 維情感向量
    
    IndexTTS Emotion Vector: [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
    """
    
    # Plutchik 情感輪的 8 個基本情感
    PLUTCHIK_EMOTIONS = [
        "joy", "trust", "fear", "surprise",
        "sadness", "disgust", "anger", "anticipation"
    ]
    
    # 映射權重配置
    EMOTION_MAPPING = {
        # IndexTTS維度 → [(Status情感, 權重), ...]
        "happy": [("joy", 0.8), ("trust", 0.2)],
        "angry": [("anger", 1.0)],
        "sad": [("sadness", 0.7), ("fear", 0.3)],
        "afraid": [("fear", 0.8), ("surprise", 0.2)],
        "disgusted": [("disgust", 1.0)],
        "melancholic": [("sadness", 0.6), ("anticipation", -0.4)],  # 悲傷但低能量
        "surprised": [("surprise", 0.8), ("anticipation", 0.2)],
        "calm": [("trust", 0.6), ("joy", 0.2), ("anger", -0.3), ("fear", -0.3)]  # 高信任、低喚醒
    }
    
    def __init__(self, max_strength: float = 0.3, default_neutral: bool = True):
        """
        初始化 Emotion Mapper
        
        Args:
            max_strength: 情感向量的最大總和 (保留原始聲音特徵)
            default_neutral: 當輸入為空時是否使用中性向量
        """
        self.max_strength = max_strength
        self.default_neutral = default_neutral
        
        debug_log(2, f"[EmotionMapper] 初始化完成, max_strength={max_strength}")
    
    def map_from_status_manager(
        self,
        mood: float,
        pride: float,
        helpfulness: float,
        boredom: float
    ) -> List[float]:
        """
        從 Status Manager 的 4 個數值映射到 IndexTTS 的 8D 向量
        
        這是 U.E.P 系統的主要映射方法
        
        Args:
            mood: 整體情緒狀態 (-1.0 到 +1.0)
                -1.0 = 非常負面, 0.0 = 中性, +1.0 = 非常正面
            pride: 自尊心 (0.0 到 +1.0)
                0.0 = 沒有自信, +1.0 = 非常自信
            helpfulness: 助人意願 (0.0 到 +1.0)
                0.0 = 不願意幫助, +1.0 = 非常願意幫助
            boredom: 無聊程度 (0.0 到 +1.0)
                0.0 = 不無聊, +1.0 = 非常無聊
        
        Returns:
            8D 情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
        
        映射邏輯:
            - Mood > 0: 增強正面情感 (happy, surprised, calm)
            - Mood < 0: 增強負面情感 (sad, afraid, disgusted)
            - **高 Pride + 負 Mood = angry** (有自信地反駁/生氣)
            - 高 Pride + 正 Mood: 增強 happy 和 calm
            - 低 Pride: 增強 sad 和 melancholic, 減少 angry (沒自信不敢生氣)
            - Helpfulness 高: 增強 calm 和 happy
            - Helpfulness 低: 增強 melancholic 和 sad
            - Boredom 高: 增強 melancholic 和 calm, 減少其他活躍情感
        """
        # 初始化 8D 向量
        emotion_vector = [0.0] * 8  # [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
        
        # === Mood 的影響 (-1 到 +1) ===
        if mood > 0:
            # 正向 Mood: 增強正面情感
            emotion_vector[0] += mood * 0.5  # happy
            emotion_vector[6] += mood * 0.3  # surprised
            emotion_vector[7] += mood * 0.2  # calm
        else:
            # 負向 Mood: 增強負面情感
            # 但是 angry 要看 Pride 的影響 (高 Pride + 低 Mood = angry)
            abs_mood = abs(mood)
            emotion_vector[2] += abs_mood * 0.4  # sad
            emotion_vector[3] += abs_mood * 0.2  # afraid
            emotion_vector[4] += abs_mood * 0.1  # disgusted
        
        # === Pride 的影響 (0 到 +1) ===
        if pride > 0.5:
            # 高 Pride: 自信
            if mood < 0:
                # 高 Pride + 負 Mood = 有自信地生氣/反駁
                emotion_vector[1] += (pride - 0.5) * abs(mood) * 0.8  # angry
                # 減少軟弱的負面情感
                emotion_vector[2] = max(0, emotion_vector[2] - (pride - 0.5) * 0.4)  # 減少 sad
                emotion_vector[3] = max(0, emotion_vector[3] - (pride - 0.5) * 0.5)  # 減少 afraid
            else:
                # 高 Pride + 正 Mood = 愉快自信
                emotion_vector[0] += (pride - 0.5) * 0.4  # happy
                emotion_vector[7] += (pride - 0.5) * 0.3  # calm
        else:
            # 低 Pride: 缺乏自信、憂鬱
            low_pride = 0.5 - pride
            emotion_vector[2] += low_pride * 0.3  # sad
            emotion_vector[5] += low_pride * 0.4  # melancholic
            emotion_vector[3] += low_pride * 0.2  # afraid
            # 低 Pride 時不會生氣，只會難過
            emotion_vector[1] = max(0, emotion_vector[1] - low_pride * 0.3)  # 減少 angry
        
        # === Helpfulness 的影響 (0 到 +1) ===
        if helpfulness > 0.5:
            # 高 Helpfulness: 積極、平和
            emotion_vector[7] += (helpfulness - 0.5) * 0.4  # calm
            emotion_vector[0] += (helpfulness - 0.5) * 0.2  # happy
        else:
            # 低 Helpfulness: 消極、憂鬱
            low_help = 0.5 - helpfulness
            emotion_vector[5] += low_help * 0.4  # melancholic
            emotion_vector[2] += low_help * 0.2  # sad
        
        # === Boredom 的影響 (0 到 +1) ===
        if boredom > 0.5:
            # 高 Boredom: 憂鬱、平靜但無趣
            high_boredom = boredom - 0.5
            emotion_vector[5] += high_boredom * 0.5  # melancholic
            emotion_vector[7] += high_boredom * 0.3  # calm
            # 減少活躍情感
            emotion_vector[0] = max(0, emotion_vector[0] - high_boredom * 0.3)  # 減少 happy
            emotion_vector[6] = max(0, emotion_vector[6] - high_boredom * 0.4)  # 減少 surprised
        
        # 確保所有值在 [0, 1] 範圍內
        emotion_vector = [max(0.0, min(1.0, v)) for v in emotion_vector]
        
        # 歸一化
        emotion_vector = self.normalize_vector(emotion_vector, self.max_strength)
        
        debug_log(3, f"[EmotionMapper] Status Manager 映射:")
        debug_log(3, f"  輸入: mood={mood:.2f}, pride={pride:.2f}, help={helpfulness:.2f}, boredom={boredom:.2f}")
        debug_log(3, f"  輸出: {[f'{v:.3f}' for v in emotion_vector]}")
        debug_log(3, f"  總強度: {sum(emotion_vector):.3f}")
        
        return emotion_vector
    
    def map_from_status(
        self,
        status_emotions: Dict[str, float],
        arousal: float = 0.5,
        valence: float = 0.5
    ) -> List[float]:
        """
        從 Status Manager 的情感值映射到 IndexTTS 的 8D 向量
        
        Args:
            status_emotions: Status Manager 的情感字典
                格式: {"joy": 0.0-1.0, "anger": 0.0-1.0, ...}
            arousal: 喚醒度 (0=低能量, 1=高能量)
            valence: 效價 (0=負面, 1=正面)
            
        Returns:
            8D 情感向量 [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
        """
        # 初始化 8D 向量
        emotion_vector = [0.0] * 8
        emotion_names = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
        
        # 映射每個維度
        for idx, emotion_name in enumerate(emotion_names):
            if emotion_name in self.EMOTION_MAPPING:
                value = 0.0
                for source_emotion, weight in self.EMOTION_MAPPING[emotion_name]:
                    source_value = status_emotions.get(source_emotion, 0.0)
                    value += source_value * weight
                
                # 確保在 [0, 1] 範圍內
                emotion_vector[idx] = max(0.0, min(1.0, value))
        
        # 應用 arousal 和 valence 調整
        emotion_vector = self._apply_arousal_valence(
            emotion_vector,
            arousal,
            valence
        )
        
        # 歸一化
        emotion_vector = self.normalize_vector(emotion_vector, self.max_strength)
        
        debug_log(3, f"[EmotionMapper] 映射結果: {emotion_vector}")
        debug_log(3, f"  原始情感: {status_emotions}")
        debug_log(3, f"  總強度: {sum(emotion_vector):.3f}")
        
        return emotion_vector
    
    def _apply_arousal_valence(
        self,
        vector: List[float],
        arousal: float,
        valence: float
    ) -> List[float]:
        """
        根據 arousal 和 valence 調整情感向量
        
        Arousal 影響:
        - 高 arousal: 增強 angry, afraid, surprised, happy
        - 低 arousal: 增強 calm, melancholic, sad
        
        Valence 影響:
        - 正向: 增強 happy, calm, surprised
        - 負向: 增強 angry, sad, afraid, disgusted
        """
        adjusted = vector.copy()
        
        # Arousal 調整
        if arousal > 0.5:
            # 高喚醒:增強活躍情感
            high_arousal_indices = [0, 1, 3, 6]  # happy, angry, afraid, surprised
            boost = (arousal - 0.5) * 0.3
            for idx in high_arousal_indices:
                adjusted[idx] = min(1.0, adjusted[idx] * (1 + boost))
        else:
            # 低喚醒:增強平靜情感
            low_arousal_indices = [2, 5, 7]  # sad, melancholic, calm
            boost = (0.5 - arousal) * 0.3
            for idx in low_arousal_indices:
                adjusted[idx] = min(1.0, adjusted[idx] * (1 + boost))
        
        # Valence 調整
        if valence > 0.5:
            # 正向情感
            positive_indices = [0, 6, 7]  # happy, surprised, calm
            boost = (valence - 0.5) * 0.3
            for idx in positive_indices:
                adjusted[idx] = min(1.0, adjusted[idx] * (1 + boost))
        else:
            # 負向情感
            negative_indices = [1, 2, 3, 4]  # angry, sad, afraid, disgusted
            boost = (0.5 - valence) * 0.3
            for idx in negative_indices:
                adjusted[idx] = min(1.0, adjusted[idx] * (1 + boost))
        
        return adjusted
    
    def normalize_vector(
        self,
        vector: List[float],
        max_strength: Optional[float] = None
    ) -> List[float]:
        """
        歸一化情感向量,確保總和不超過 max_strength
        
        這確保了原始聲音特徵得以保留
        例如: max_strength=0.3 表示保留 70% 的原始聲音
        
        Args:
            vector: 原始情感向量
            max_strength: 最大強度 (None 使用默認值)
            
        Returns:
            歸一化後的向量
        """
        max_str = max_strength if max_strength is not None else self.max_strength
        current_sum = sum(vector)
        
        if current_sum == 0:
            # 全零向量,返回中性
            return [0.0] * 8 if self.default_neutral else vector
        
        if current_sum <= max_str:
            # 已經在範圍內
            return vector
        
        # 按比例縮放
        scale_factor = max_str / current_sum
        normalized = [v * scale_factor for v in vector]
        
        debug_log(3, f"[EmotionMapper] 歸一化: {current_sum:.3f} → {sum(normalized):.3f}")
        debug_log(3, f"  原始聲音保留: {(1 - sum(normalized)) * 100:.1f}%")
        
        return normalized
    
    def get_neutral_vector(self) -> List[float]:
        """返回中性情感向量 (全零)"""
        return [0.0] * 8
    
    def get_preset_emotion(self, emotion_name: str, strength: float = 0.3) -> List[float]:
        """
        獲取預設情感向量
        
        Args:
            emotion_name: 情感名稱 (happy, angry, sad, calm, etc.)
            strength: 強度 (0.0-1.0)
            
        Returns:
            8D 情感向量
        """
        presets = {
            "happy": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "angry": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "sad": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "afraid": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            "disgusted": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            "melancholic": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "surprised": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            "calm": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            "neutral": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            # 混合預設
            "excited": [0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4, 0.0],
            "worried": [0.0, 0.0, 0.3, 0.7, 0.0, 0.0, 0.0, 0.0],
            "content": [0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.7],
        }
        
        if emotion_name.lower() not in presets:
            debug_log(1, f"[EmotionMapper] 未知的預設情感: {emotion_name}, 使用中性")
            return self.get_neutral_vector()
        
        vector = presets[emotion_name.lower()]
        return self.normalize_vector(vector, strength)
    
    def blend_emotions(
        self,
        emotions: List[tuple[str, float]],
        normalize: bool = True
    ) -> List[float]:
        """
        混合多個預設情感
        
        Args:
            emotions: [(emotion_name, weight), ...] 
                例如: [("happy", 0.6), ("excited", 0.4)]
            normalize: 是否歸一化結果
            
        Returns:
            混合後的 8D 向量
        """
        result = [0.0] * 8
        
        for emotion_name, weight in emotions:
            preset = self.get_preset_emotion(emotion_name, strength=1.0)
            for i in range(8):
                result[i] += preset[i] * weight
        
        if normalize:
            result = self.normalize_vector(result, self.max_strength)
        
        return result


# 便捷函數
def create_emotion_mapper(max_strength: float = 0.3) -> EmotionMapper:
    """創建一個 EmotionMapper 實例"""
    return EmotionMapper(max_strength=max_strength)


def map_from_status_manager(
    mood: float,
    pride: float,
    helpfulness: float,
    boredom: float,
    max_strength: float = 0.3
) -> List[float]:
    """
    從 Status Manager 數值快速映射到 8D 情感向量
    
    這是 U.E.P 系統的主要便捷函數
    
    Args:
        mood: 整體情緒狀態 (-1.0 到 +1.0)
        pride: 自尊心 (0.0 到 +1.0)
        helpfulness: 助人意願 (0.0 到 +1.0)
        boredom: 無聊程度 (0.0 到 +1.0)
        max_strength: 最大強度
        
    Returns:
        8D 情感向量
    """
    mapper = EmotionMapper(max_strength=max_strength)
    return mapper.map_from_status_manager(mood, pride, helpfulness, boredom)


def quick_map(
    joy: float = 0.0,
    anger: float = 0.0,
    sadness: float = 0.0,
    fear: float = 0.0,
    max_strength: float = 0.3
) -> List[float]:
    """
    快速映射,適用於簡單場景 (Plutchik 情感輪)
    
    注意: 這個函數用於測試或特殊場景
    U.E.P 系統應該使用 map_from_status_manager()
    
    Args:
        joy, anger, sadness, fear: 基本情感值 (0.0-1.0)
        max_strength: 最大強度
        
    Returns:
        8D 情感向量
    """
    mapper = EmotionMapper(max_strength=max_strength)
    status_emotions = {
        "joy": joy,
        "anger": anger,
        "sadness": sadness,
        "fear": fear,
        "trust": 0.0,
        "disgust": 0.0,
        "surprise": 0.0,
        "anticipation": 0.0
    }
    return mapper.map_from_status(status_emotions)
