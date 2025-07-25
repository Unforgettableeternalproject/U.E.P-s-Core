"""
modules/stt_module/speaker_identification.py
說話人識別 (Speaker Identification) 實現
"""

import numpy as np
import librosa
import pickle
import os
import uuid
import time
from typing import Dict, List, Optional, Tuple
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.cluster import KMeans
from utils.debug_helper import debug_log, info_log, error_log
from .schemas import SpeakerInfo, SpeakerModel

class SpeakerIdentifier:
    """說話人識別器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.si_config = config.get("speaker_identification", {})
        
        # 特徵提取參數
        self.n_mfcc = self.si_config.get("mfcc_features", 13)
        self.sample_rate = self.si_config.get("sample_rate", 16000)
        self.frame_length = self.si_config.get("frame_length", 2048)
        self.hop_length = self.si_config.get("hop_length", 512)
        
        # 識別參數 - 更嚴格的閾值來更好區分不同說話人
        self.similarity_threshold = self.si_config.get("similarity_threshold", 0.85)  # 提高門檻
        self.new_speaker_threshold = self.si_config.get("new_speaker_threshold", 0.7)  # 提高門檻
        self.min_samples = self.si_config.get("min_samples_for_model", 3)  # 減少需要的樣本數
        
        # 說話人模型存儲
        self.speaker_models: Dict[str, SpeakerModel] = {}
        self.models_file = os.path.abspath("memory/speaker_models.pkl")
        
        # 確保目錄存在
        os.makedirs(os.path.dirname(self.models_file), exist_ok=True)
        
        # 初始化
        self._load_speaker_models()
        info_log(f"[SpeakerID] 初始化完成，已載入 {len(self.speaker_models)} 個說話人模型")
        info_log(f"[SpeakerID] 模型存儲位置: {self.models_file}")
        
    def extract_voice_features(self, audio_data: np.ndarray) -> np.ndarray:
        """
        提取語音特徵 (MFCC)
        
        Args:
            audio_data: 音頻數據
            
        Returns:
            np.ndarray: 特徵向量
        """
        try:
            # 確保音頻數據是浮點型
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)
            
            # 提取 MFCC 特徵
            mfccs = librosa.feature.mfcc(
                y=audio_data,
                sr=self.sample_rate,
                n_mfcc=self.n_mfcc,
                n_fft=self.frame_length,
                hop_length=self.hop_length
            )
            
            # 計算統計特徵 (均值和標準差)
            mfcc_mean = np.mean(mfccs, axis=1)
            mfcc_std = np.std(mfccs, axis=1)
            
            # 組合特徵
            features = np.concatenate([mfcc_mean, mfcc_std])
            
            debug_log(3, f"[SpeakerID] 提取特徵維度: {features.shape}")
            return features
            
        except Exception as e:
            error_log(f"[SpeakerID] 特徵提取失敗: {str(e)}")
            return np.array([])
            
    def calculate_similarity(self, features1: np.ndarray, features2: np.ndarray) -> float:
        """計算兩個特徵向量的相似度"""
        try:
            # 重塑為 2D 數組以適應 cosine_similarity
            f1 = features1.reshape(1, -1)
            f2 = features2.reshape(1, -1)
            
            similarity = cosine_similarity(f1, f2)[0][0]
            return float(similarity)
            
        except Exception as e:
            error_log(f"[SpeakerID] 相似度計算失敗: {str(e)}")
            return 0.0
            
    def identify_speaker(self, audio_data: np.ndarray) -> SpeakerInfo:
        """
        識別說話人 - 改進版本，更好地區分不同說話人
        
        Args:
            audio_data: 音頻數據
            
        Returns:
            SpeakerInfo: 說話人信息
        """
        # 提取特徵
        features = self.extract_voice_features(audio_data)
        if features.size == 0:
            return SpeakerInfo(
                speaker_id="unknown",
                confidence=0.0,
                is_new_speaker=False,
                voice_features=None
            )
        
        debug_log(3, f"[SpeakerID] 提取特徵維度: {features.shape}")
        
        # 紀錄每個說話人模型的平均相似度 (不只是最高值)
        speaker_similarities = {}
        
        # 與已知說話人比較
        for speaker_id, model in self.speaker_models.items():
            if not model.feature_vectors or len(model.feature_vectors) < 2:
                continue
                
            # 計算與所有已知特徵的相似度
            similarities = []
            for stored_features in model.feature_vectors:
                sim = self.calculate_similarity(features, np.array(stored_features))
                similarities.append(sim)
                
            # 使用平均相似度而不是最高值，更能代表整體匹配程度
            avg_similarity = sum(similarities) / len(similarities)
            speaker_similarities[speaker_id] = avg_similarity
        
        # 尋找最佳匹配    
        if speaker_similarities:
            best_match = max(speaker_similarities.items(), key=lambda x: x[1])
            best_speaker_id, best_similarity = best_match
        else:
            best_speaker_id, best_similarity = None, 0.0
        
        # 計算與其他模型的相似度差異，確保此說話人明顯區別於其他人
        is_distinct = True
        if len(speaker_similarities) > 1 and best_similarity > 0:
            # 排除最佳匹配後的第二高相似度
            other_similarities = [sim for spk, sim in speaker_similarities.items() if spk != best_speaker_id]
            if other_similarities:
                second_best = max(other_similarities)
                # 如果最佳匹配與第二佳匹配相似度差異小於20%，表示不夠明顯
                if (best_similarity - second_best) / best_similarity < 0.2:
                    is_distinct = False
                    debug_log(2, f"[SpeakerID] 最佳匹配不夠明顯: {best_similarity:.2f} vs {second_best:.2f}")
                
        # 判斷是否為已知說話人
        if best_similarity >= self.similarity_threshold and is_distinct:
            # 已知說話人，更新模型
            self._update_speaker_model(best_speaker_id, features)
            
            debug_log(2, f"[SpeakerID] 識別出已知說話人: {best_speaker_id} (信心度: {best_similarity:.2f})")
            
            return SpeakerInfo(
                speaker_id=best_speaker_id,
                confidence=best_similarity,
                is_new_speaker=False,
                voice_features={"mfcc": features.tolist()}
            )
            
        elif best_similarity < self.new_speaker_threshold or not is_distinct:
            # 相似度太低或匹配不夠明顯，認為是新說話人
            new_speaker_id = self._create_new_speaker(features)
            
            return SpeakerInfo(
                speaker_id=new_speaker_id,
                confidence=1.0,  # 新說話人的信心度設為1.0
                is_new_speaker=True,
                voice_features={"mfcc": features.tolist()}
            )
            
        else:
            # 不確定，返回最佳匹配但標記為低信心度
            return SpeakerInfo(
                speaker_id=best_speaker_id or "uncertain",
                confidence=best_similarity,
                is_new_speaker=False,
                voice_features={"mfcc": features.tolist()}
            )
            
    def _create_new_speaker(self, features: np.ndarray) -> str:
        """創建新說話人模型"""
        speaker_id = f"speaker_{str(uuid.uuid4())[:8]}"
        current_time = time.time()
        
        new_model = SpeakerModel(
            speaker_id=speaker_id,
            feature_vectors=[features.tolist()],
            sample_count=1,
            created_at=current_time,
            last_updated=current_time
        )
        
        self.speaker_models[speaker_id] = new_model
        self._save_speaker_models()
        
        info_log(f"[SpeakerID] 創建新說話人: {speaker_id}")
        return speaker_id
        
    def _update_speaker_model(self, speaker_id: str, features: np.ndarray):
        """更新說話人模型"""
        if speaker_id not in self.speaker_models:
            return
            
        model = self.speaker_models[speaker_id]
        model.feature_vectors.append(features.tolist())
        model.sample_count += 1
        model.last_updated = time.time()
        
        # 限制特徵向量數量（保留最新的）
        max_features = 20
        if len(model.feature_vectors) > max_features:
            model.feature_vectors = model.feature_vectors[-max_features:]
            
        self._save_speaker_models()
        debug_log(2, f"[SpeakerID] 更新說話人模型: {speaker_id}, 樣本數: {model.sample_count}")
        
    def _load_speaker_models(self):
        """載入說話人模型"""
        try:
            if os.path.exists(self.models_file):
                with open(self.models_file, 'rb') as f:
                    models_data = pickle.load(f)
                    
                # 轉換為 SpeakerModel 對象
                for speaker_id, data in models_data.items():
                    if isinstance(data, dict):
                        self.speaker_models[speaker_id] = SpeakerModel(**data)
                    else:
                        self.speaker_models[speaker_id] = data
                        
                info_log(f"[SpeakerID] 載入 {len(self.speaker_models)} 個說話人模型")
            else:
                info_log("[SpeakerID] 未發現現有模型，將創建新的模型庫")
                
        except Exception as e:
            error_log(f"[SpeakerID] 載入模型失敗: {str(e)}")
            self.speaker_models = {}
            
    def _save_speaker_models(self):
        """保存說話人模型"""
        try:
            # 確保目錄存在
            os.makedirs(os.path.dirname(self.models_file), exist_ok=True)
            
            # 轉換為字典格式保存
            models_data = {}
            for speaker_id, model in self.speaker_models.items():
                models_data[speaker_id] = model.dict()
                
            with open(self.models_file, 'wb') as f:
                pickle.dump(models_data, f)
                
            debug_log(3, f"[SpeakerID] 保存 {len(self.speaker_models)} 個說話人模型")
            
        except Exception as e:
            error_log(f"[SpeakerID] 保存模型失敗: {str(e)}")
            
    def get_speaker_stats(self) -> Dict[str, Dict]:
        """獲取說話人統計信息"""
        stats = {}
        for speaker_id, model in self.speaker_models.items():
            stats[speaker_id] = {
                "sample_count": model.sample_count,
                "created_at": model.created_at,
                "last_updated": model.last_updated
            }
        return stats
