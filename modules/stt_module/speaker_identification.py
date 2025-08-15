#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
說話人識別模組 - 使用 pyannote.audio
"""

import os
import time
import pickle
import tempfile
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from collections import defaultdict

# 載入環境變數
load_dotenv()

try:
    import torch
    import torchaudio
    from pyannote.audio import Pipeline as PyannoteTPipeline
    from pyannote.audio import Model
    from pyannote.core import Segment
    from scipy.spatial.distance import cosine, euclidean
    from sklearn.preprocessing import normalize
    from sklearn.cluster import DBSCAN
    PYANNOTE_AVAILABLE = True
except ImportError:
    PYANNOTE_AVAILABLE = False

from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import working_context_manager, ContextType
from .schemas import SpeakerInfo

class SpeakerIdentification:
    """說話人識別和管理類 - 整合高相似度語者辨識系統"""
    
    def __init__(self, config: dict = None, model_name: str = "pyannote/speaker-diarization-3.1"):
        self.model_name = model_name
        self.pipeline = None
        self.embedding_model = None  # 用於提取說話人嵌入的模型
        self.sample_rate = 16000
        
        # 說話人資料庫
        self.speaker_database = {}  # {speaker_id: {'embeddings': [], 'metadata': {}}}
        self.speaker_counter = 0
        self.similarity_threshold = 0.999995  # 基於原始 pyannote 嵌入的閾值
        
        # 儲存路徑
        self.database_path = "memory/speaker_models.pkl"
        
        # 載入 HuggingFace Token
        self.hf_token = os.getenv("HUGGING_FACE_TOKEN")
        
        # 高相似度語者辨識系統配置
        self.use_multi_distance = True  # 使用多重距離計算
        self.use_enhanced_features = True  # 使用增強特徵
        self.use_magnitude_difference = True  # 使用向量大小差異
        
        # 多重距離計算權重
        self.distance_weights = {
            'cosine': 0.3,
            'euclidean': 0.3,
            'magnitude': 0.2,
            'correlation': 0.2
        }
        # DBSCAN 設定（用於高相似度實時匹配）
        self.dbscan_eps = 0.001
        self.dbscan_min_samples = 1
        self.dbscan_metric = 'cosine'

        # 置信度映射與閾值（避免 1.00 的過度自信）
        self.multi_distance_threshold = 0.15
        self.fallback_multi_distance_threshold = 0.25
        self.confidence_max_cap = 0.98
        
        # 工作上下文暫存樣本管理
        self.context_sample_threshold = 3  # 工作上下文中累積多少樣本後才考慮創建新說話人
        
        # 語者資料庫最小樣本數閾值（低於此數值的語者將被隱蔽）
        self.min_samples_for_recognition = 15

        # 從配置中讀取設置
        if config:
            speaker_config = config.get('speaker_recognition', {})
            self.use_multi_distance = speaker_config.get('use_multi_distance', self.use_multi_distance)
            self.use_enhanced_features = speaker_config.get('use_enhanced_features', self.use_enhanced_features)
            self.use_magnitude_difference = speaker_config.get('use_magnitude_difference', self.use_magnitude_difference)

            weights = speaker_config.get('distance_weights', {})
            if weights:
                for key, value in weights.items():
                    if key in self.distance_weights:
                        self.distance_weights[key] = value
            # DBSCAN 設定覆寫
            self.dbscan_eps = speaker_config.get('dbscan_eps', self.dbscan_eps)
            self.dbscan_min_samples = speaker_config.get('dbscan_min_samples', self.dbscan_min_samples)
            self.dbscan_metric = speaker_config.get('dbscan_metric', self.dbscan_metric)
            # 置信度映射與閾值覆寫
            self.multi_distance_threshold = speaker_config.get('multi_distance_threshold', self.multi_distance_threshold)
            self.fallback_multi_distance_threshold = speaker_config.get('fallback_multi_distance_threshold', self.fallback_multi_distance_threshold)
            self.confidence_max_cap = speaker_config.get('confidence_max_cap', self.confidence_max_cap)
            # 工作上下文配置覆寫
            self.context_sample_threshold = speaker_config.get('context_sample_threshold', self.context_sample_threshold)
            # 最小樣本數閾值覆寫
            self.min_samples_for_recognition = speaker_config.get('min_samples_for_recognition', self.min_samples_for_recognition)

        info_log("[Speaker] 說話人識別模組初始化 (已整合高相似度辨識功能)")
        
    def initialize(self) -> bool:
        """初始化說話人識別模型"""
        if not PYANNOTE_AVAILABLE:
            error_log("[Speaker] pyannote.audio 不可用，將使用 fallback 模式")
            # 載入說話人資料庫（即使沒有 pyannote 也可以使用基本功能）
            self._load_speaker_database()
            return True
            
        if not self.hf_token:
            error_log("[Speaker] HuggingFace Token 未設定，將使用 fallback 模式")
            # 載入說話人資料庫（即使沒有 token 也可以使用基本功能）
            self._load_speaker_database()
            return True
            
        try:
            info_log(f"[Speaker] 載入模型: {self.model_name}")
            
            # 載入說話人分離 pipeline
            self.pipeline = PyannoteTPipeline.from_pretrained(
                self.model_name,
                use_auth_token=self.hf_token
            )
            
            # 載入說話人嵌入模型
            try:
                self.embedding_model = Model.from_pretrained(
                    "pyannote/embedding", 
                    use_auth_token=self.hf_token
                )
                info_log("[Speaker] 嵌入模型載入成功")
            except Exception as e:
                error_log(f"[Speaker] 嵌入模型載入失敗: {e}")
                self.embedding_model = None
            
            # 檢查 pipeline 是否成功載入
            if self.pipeline is None:
                error_log("[Speaker] Pipeline 載入失敗，將使用 fallback 模式")
                self._load_speaker_database()
                return True
            
            # 設置設備
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.pipeline.to(torch.device(device))
            if self.embedding_model:
                self.embedding_model.to(torch.device(device))
            
            info_log(f"[Speaker] 模型載入成功 (設備: {device})")
            
            # 載入說話人資料庫
            self._load_speaker_database()
            
            return True
            
        except Exception as e:
            error_log(f"[Speaker] 模型載入失敗: {e}")
            error_log("[Speaker] 將使用 fallback 說話人識別模式")
            
            # 清理失敗的 pipeline
            self.pipeline = None
            
            # 載入說話人資料庫（即使模型失敗也可以使用基本功能）
            self._load_speaker_database()
            
            return True  # 返回 True 因為 fallback 模式仍然可用
    
    def identify_speaker(self, audio_data: np.ndarray) -> SpeakerInfo:
        """識別說話人"""
        if not self.pipeline:
            return self._fallback_speaker_identification(audio_data)
            
        try:
            debug_log(2, "[Speaker] 開始說話人識別...")
            
            # 將音頻數據轉換為暫存檔案
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                # 正規化音頻數據
                audio_float = audio_data.astype(np.float32) / 32768.0
                
                # 轉換為 torch tensor
                audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)
                
                # 保存為暫存音頻檔案
                torchaudio.save(temp_file.name, audio_tensor, self.sample_rate)
                temp_path = temp_file.name
            
            # 執行說話人分離
            diarization = self.pipeline(temp_path)
            
            # 清理暫存檔案
            try:
                os.unlink(temp_path)
            except:
                pass
            
            # 處理說話人分離結果
            speakers = []
            for segment, _, speaker in diarization.itertracks(yield_label=True):
                speakers.append({
                    'speaker': speaker,
                    'start': segment.start,
                    'end': segment.end,
                    'duration': segment.end - segment.start
                })
            
            if not speakers:
                debug_log(2, "[Speaker] pyannote 未檢測到說話人，使用嵌入模型直接識別")
                # 直接使用嵌入模型進行說話人識別
                return self._direct_embedding_identification(audio_data)
            
            # 找到持續時間最長的說話人
            main_speaker = max(speakers, key=lambda x: x['duration'])
            raw_speaker_id = main_speaker['speaker']
            duration_confidence = min(main_speaker['duration'] / (len(audio_data) / self.sample_rate), 1.0)
            
            # 映射到已知說話人或創建新說話人
            speaker_id, is_new, similarity = self._map_speaker(raw_speaker_id, audio_data)

            # 使用相似度映射為置信度（並施加上限避免 1.00）
            final_confidence = min(similarity, self.confidence_max_cap) if similarity > 0 else duration_confidence

            debug_log(2, f"[Speaker] 識別到說話人: {speaker_id}, 置信度: {final_confidence:.2f}, 時長信心度: {duration_confidence:.2f}")
            
            return SpeakerInfo(
                speaker_id=speaker_id,
                confidence=final_confidence,
                is_new_speaker=is_new,
                voice_features={
                    "duration": main_speaker['duration'], 
                    "segments": len(speakers),
                    "raw_id": raw_speaker_id,
                    "similarity": similarity,
                    "duration_confidence": duration_confidence
                }
            )
            
        except Exception as e:
            error_log(f"[Speaker] 說話人識別失敗: {str(e)}")
            return self._fallback_speaker_identification(audio_data)
    
    def _direct_embedding_identification(self, audio_data: np.ndarray) -> SpeakerInfo:
        """當 pyannote 說話人分離失敗時，直接使用嵌入模型進行說話人識別"""
        try:
            # 計算音頻嵌入
            embedding = self._compute_audio_embedding(audio_data)

            # 嘗試使用本地 DBSCAN 進行高相似度匹配（若配置啟用）
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, match_distances = dbscan_result
                    # 更新資料庫並回傳匹配結果
                    self.speaker_database[match_id]['embeddings'].append(embedding)
                    self.speaker_database[match_id]['metadata']['last_seen'] = time.time()
                    self.speaker_database[match_id]['metadata']['sample_count'] += 1
                    self._save_speaker_database()
                    voice_features = {
                        'method': 'dbscan_match',
                        'similarity': match_similarity,
                        'distances': match_distances
                    }
                    return SpeakerInfo(
                        speaker_id=match_id,
                        confidence=match_similarity,
                        is_new_speaker=False,
                        voice_features=voice_features
                    )
            except Exception:
                # 若 DBSCAN 出錯，繼續後續邏輯
                pass
            
            # 檢查是否為已知說話人（只檢查達到最小樣本數的說話人）
            qualified_speakers = self._get_qualified_speakers()
            best_match_id = None
            best_similarity = 0.0
            best_distance_score = float('inf')
            all_distances = {}
            
            # 使用多距離計算或單純餘弦相似度
            if self.use_multi_distance:
                debug_log(3, f"[Speaker] 使用多距離計算進行說話人識別")
                
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        # 計算多種距離
                        distances = self._calculate_multi_distance(embedding, known_embedding)
                        combined_score = self._combine_distances(distances)
                        
                        # 保存最佳匹配
                        if combined_score < best_distance_score:
                            best_distance_score = combined_score
                            best_match_id = known_id
                            
                            # 同時計算餘弦相似度以保持兼容性
                            cosine_similarity = 1 - distances['cosine']
                            best_similarity = cosine_similarity
                            all_distances = distances
                
                # 閾值判斷 - 多距離評分較低表示更相似
                similarity_check = best_distance_score < self.multi_distance_threshold
            else:
                # 傳統餘弦相似度計算
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        similarity = 1 - cosine(embedding, known_embedding)
                        if similarity > best_similarity:
                            best_similarity = similarity
                            best_match_id = known_id
                
                # 傳統閾值判斷
                similarity_check = best_similarity > self.similarity_threshold
            
            # 如果相似度符合要求，返回已知說話人
            if similarity_check:
                # 添加新的嵌入樣本
                self.speaker_database[best_match_id]['embeddings'].append(embedding)
                self.speaker_database[best_match_id]['metadata']['last_seen'] = time.time()
                self.speaker_database[best_match_id]['metadata']['sample_count'] += 1
                self._save_speaker_database()
                
                if self.use_multi_distance:
                    debug_log(3, f"[Speaker] [直接嵌入] 匹配已知說話人: {best_match_id} "
                             f"(多距離分數: {best_distance_score:.3f}, 餘弦相似度: {best_similarity:.3f})")
                    # 由多距離分數映射置信度並加上上限
                    confidence = self._confidence_from_scores(best_similarity, best_distance_score, self.multi_distance_threshold)

                    # 構建豐富的語音特徵
                    voice_features = {
                        "method": "multi_distance",
                        "similarity": best_similarity,
                        "combined_score": best_distance_score
                    }
                    
                    # 添加每種距離的詳細信息
                    for dist_type, value in all_distances.items():
                        voice_features[f"distance_{dist_type}"] = value
                else:
                    debug_log(3, f"[Speaker] [直接嵌入] 匹配已知說話人: {best_match_id} "
                             f"(相似度: {best_similarity:.3f}, 閾值: {self.similarity_threshold})")
                    confidence = min(best_similarity, self.confidence_max_cap)

                    voice_features = {
                        "method": "direct_embedding",
                        "similarity": best_similarity
                    }
                
                return SpeakerInfo(
                    speaker_id=best_match_id,
                    confidence=confidence,
                    is_new_speaker=False,
                    voice_features=voice_features
                )
            
            # 沒有匹配的合格說話人，使用工作上下文累積樣本
            context_id = working_context_manager.add_data_to_context(
                ContextType.SPEAKER_ACCUMULATION, 
                embedding,
                metadata={
                    "method": "multi_distance" if self.use_multi_distance else "direct_embedding",
                    "best_similarity": best_similarity,
                    "best_distance_score": best_distance_score if self.use_multi_distance else None,
                    "timestamp": time.time()
                }
            )
            
            # 獲取上下文信息
            context_info = working_context_manager.get_context_status(context_id)
            if context_info:
                data_count = context_info['data_count']
                is_ready = context_info['is_ready']
                
                debug_log(3, f"[Speaker] [直接嵌入] 樣本添加到工作上下文 {context_id} "
                         f"(樣本數: {data_count}/{self.context_sample_threshold})")
                
                return SpeakerInfo(
                    speaker_id=f"context_{context_id}",
                    confidence=0.5,  # 中等信心度，表示待確認
                    is_new_speaker=False,  # 不確定是否為新說話人
                    voice_features={
                        "method": "context_accumulation",
                        "context_id": context_id,
                        "data_count": data_count,
                        "threshold": self.context_sample_threshold,
                        "is_ready": is_ready,
                        "status": "accumulating"
                    }
                )
            else:
                # 如果無法獲取上下文信息，使用預設回應
                return SpeakerInfo(
                    speaker_id="context_pending",
                    confidence=0.5,
                    is_new_speaker=False,
                    voice_features={
                        "method": "context_pending",
                        "status": "unknown"
                    }
                )
            
        except Exception as e:
            error_log(f"[Speaker] 直接嵌入識別失敗: {str(e)}")
            return SpeakerInfo(
                speaker_id="unknown",
                confidence=0.0,
                is_new_speaker=False,
                voice_features=None
            )
    
    def _map_speaker(self, raw_speaker_id: str, audio_data: np.ndarray) -> Tuple[str, bool, float]:
        """將原始說話人ID映射到持久化的說話人ID，返回 (speaker_id, is_new, similarity)"""
        try:
            # 計算音頻特徵作為嵌入
            embedding = self._compute_audio_embedding(audio_data)

            # 嘗試使用本地 DBSCAN 進行高相似度匹配（若配置啟用）
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, match_distances = dbscan_result
                    return match_id, False, match_similarity
            except Exception:
                pass
            
            # 檢查是否為已知說話人（只檢查達到最小樣本數的說話人）
            qualified_speakers = self._get_qualified_speakers()
            best_match_id = None
            best_similarity = 0.0
            best_distance_score = float('inf')
            
            # 使用多距離計算或單純餘弦相似度
            if self.use_multi_distance:
                debug_log(3, f"[Speaker] 使用多距離計算進行說話人映射")
                
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        # 計算多種距離
                        distances = self._calculate_multi_distance(embedding, known_embedding)
                        combined_score = self._combine_distances(distances)
                        
                        # 保存最佳匹配
                        if combined_score < best_distance_score:
                            best_distance_score = combined_score
                            best_match_id = known_id
                            
                            # 同時計算餘弦相似度以保持兼容性
                            cosine_similarity = 1 - distances['cosine']
                            best_similarity = cosine_similarity
                
                # 閾值判斷 - 多距離評分較低表示更相似
                similarity_check = best_distance_score < self.multi_distance_threshold
                debug_log(3, f"[Speaker] 多距離分數: {best_distance_score:.3f}, 餘弦相似度: {best_similarity:.3f}")
            else:
                # 傳統餘弦相似度計算
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        similarity = 1 - cosine(embedding, known_embedding)
                        if similarity > best_similarity:
                            best_similarity = similarity
                            best_match_id = known_id
                
                # 傳統閾值判斷
                similarity_check = best_similarity > self.similarity_threshold
            
            # 如果相似度符合要求，返回已知說話人
            if similarity_check:
                # 添加新的嵌入樣本
                self.speaker_database[best_match_id]['embeddings'].append(embedding)
                self.speaker_database[best_match_id]['metadata']['last_seen'] = time.time()
                self.speaker_database[best_match_id]['metadata']['sample_count'] += 1
                # 添加原始ID到映射
                if 'raw_ids' not in self.speaker_database[best_match_id]['metadata']:
                    self.speaker_database[best_match_id]['metadata']['raw_ids'] = []
                if raw_speaker_id not in self.speaker_database[best_match_id]['metadata']['raw_ids']:
                    self.speaker_database[best_match_id]['metadata']['raw_ids'].append(raw_speaker_id)
                self._save_speaker_database()
                
                if self.use_multi_distance:
                    debug_log(3, f"[Speaker] 匹配已知說話人: {best_match_id} (多距離分數: {best_distance_score:.3f})")
                else:
                    debug_log(3, f"[Speaker] 匹配已知說話人: {best_match_id} (相似度: {best_similarity:.3f}, 閾值: {self.similarity_threshold})")
                    
                return best_match_id, False, best_similarity
            
            # 沒有匹配的合格說話人，使用工作上下文累積樣本
            context_id = working_context_manager.add_data_to_context(
                context_type=ContextType.SPEAKER_ACCUMULATION, 
                data_item=embedding,
                metadata={
                    "raw_speaker_id": raw_speaker_id,
                    "method": "speaker_mapping",
                    "best_similarity": best_similarity,
                    "timestamp": time.time()
                }
            )
            
            # 獲取上下文信息
            context_info = working_context_manager.get_context_status(context_id)
            if context_info:
                data_count = context_info['data_count']
                debug_log(3, f"[Speaker] 樣本添加到工作上下文 {context_id} (樣本數: {data_count})")
                
                return f"context_{context_id}", False, best_similarity
            else:
                return "context_pending", False, best_similarity
            
        except Exception as e:
            error_log(f"[Speaker] 說話人映射失敗: {str(e)}")
            return f"error_{int(time.time())}", True, 0.0
    
    def _compute_audio_embedding(self, audio_data: np.ndarray) -> np.ndarray:
        """計算音頻特徵嵌入 - 優先使用 pyannote 嵌入模型"""
        try:
            if self.embedding_model is not None:
                # 使用 pyannote 嵌入模型
                audio_float = audio_data.astype(np.float32) / 32768.0
                
                # 轉換為 torch tensor
                audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)
                
                # 確保音頻長度足夠（至少 0.5 秒）
                min_length = int(0.5 * self.sample_rate)
                if audio_tensor.shape[1] < min_length:
                    # 填充音頻
                    padding = min_length - audio_tensor.shape[1]
                    audio_tensor = torch.nn.functional.pad(audio_tensor, (0, padding))
                
                # 移到正確的設備
                device = next(self.embedding_model.parameters()).device
                audio_tensor = audio_tensor.to(device)
                
                # 使用嵌入模型提取特徵
                with torch.no_grad():
                    # pyannote 嵌入模型期望的輸入格式
                    embedding = self.embedding_model(audio_tensor)
                    
                # 轉換為 numpy
                if isinstance(embedding, torch.Tensor):
                    embedding_np = embedding.cpu().numpy()
                else:
                    # 如果返回的是字典，嘗試獲取嵌入
                    error_log(f"[Speaker] 意外的嵌入格式: {type(embedding)}")
                    raise ValueError(f"意外的嵌入格式: {type(embedding)}")
                    
                # 如果是多維的，取平均或壓平
                if len(embedding_np.shape) > 1:
                    if embedding_np.shape[0] == 1:  # batch dimension
                        embedding_np = embedding_np[0]
                    else:
                        embedding_np = np.mean(embedding_np, axis=0)
                
                # 不要強制正規化！保持 pyannote 的原始嵌入
                # 只有在嵌入為零向量時才需要處理
                if np.linalg.norm(embedding_np) == 0:
                    debug_log(3, "[Speaker] 警告：嵌入為零向量")
                    # 回退到簡化特徵
                    return self._compute_fallback_embedding(audio_data)
                
                debug_log(3, f"[Speaker] pyannote 原始嵌入維度: {embedding_np.shape}, 範數: {np.linalg.norm(embedding_np):.6f}")
                return embedding_np
                
            else:
                # 回退到簡化特徵
                debug_log(3, "[Speaker] 使用回退特徵提取")
                return self._compute_fallback_embedding(audio_data)
                
        except Exception as e:
            error_log(f"[Speaker] pyannote 嵌入計算失敗: {str(e)}")
            # 回退到簡化特徵
            return self._compute_fallback_embedding(audio_data)
    
    def _compute_fallback_embedding(self, audio_data: np.ndarray) -> np.ndarray:
        """Fallback 音頻特徵提取方法（改進版）"""
        try:
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 1. 基本統計特徵
            mean_amplitude = np.mean(np.abs(audio_float))
            rms_energy = np.sqrt(np.mean(audio_float**2))
            zero_crossing_rate = np.mean(np.abs(np.diff(np.sign(audio_float))))
            
            # 2. 頻譜特徵
            fft = np.fft.fft(audio_float)
            magnitude = np.abs(fft[:len(fft)//2])  # 只取正頻率部分
            
            # 防止除零
            if np.sum(magnitude) == 0:
                magnitude = np.ones_like(magnitude) * 1e-10
            
            # 頻譜重心
            freqs = np.arange(len(magnitude))
            spectral_centroid = np.sum(freqs * magnitude) / np.sum(magnitude)
            
            # 頻譜帶寬
            spectral_bandwidth = np.sqrt(np.sum(((freqs - spectral_centroid) ** 2) * magnitude) / np.sum(magnitude))
            
            # 3. 分段特徵（模擬 MFCC）
            n_segments = 8
            segment_length = max(1, len(audio_float) // n_segments)
            segment_features = []
            
            for i in range(n_segments):
                start = i * segment_length
                end = min((i + 1) * segment_length, len(audio_float))
                segment = audio_float[start:end]
                
                if len(segment) > 0:
                    seg_energy = np.sqrt(np.mean(segment**2))
                    seg_zcr = np.mean(np.abs(np.diff(np.sign(segment))))
                    
                    # 添加一些變化來區分不同的音頻
                    seg_std = np.std(segment)
                    seg_skew = np.mean((segment - np.mean(segment))**3) / (np.std(segment)**3 + 1e-10)
                    
                    segment_features.extend([seg_energy, seg_zcr, seg_std, seg_skew])
                else:
                    segment_features.extend([0, 0, 0, 0])
            
            # 4. 基頻估算（簡化版）
            autocorr = np.correlate(audio_float, audio_float, mode='full')
            autocorr = autocorr[len(autocorr)//2:]
            
            # 找峰值來估算基頻
            min_period = int(self.sample_rate / 800)  # 最高 800Hz
            max_period = int(self.sample_rate / 50)   # 最低 50Hz
            
            if len(autocorr) > max_period:
                pitch_autocorr = autocorr[min_period:max_period]
                if len(pitch_autocorr) > 0:
                    fundamental_period = np.argmax(pitch_autocorr) + min_period
                    fundamental_freq = self.sample_rate / fundamental_period
                else:
                    fundamental_freq = 150  # 預設值
            else:
                fundamental_freq = 150
            
            # 組合所有特徵
            embedding = np.array([
                mean_amplitude, rms_energy, zero_crossing_rate, 
                spectral_centroid, spectral_bandwidth, fundamental_freq
            ] + segment_features)
            
            # 添加隨機性以避免完全相同的嵌入
            noise = np.random.normal(0, 0.01, len(embedding))
            embedding = embedding + noise
            
            # 正規化（但不強制到單位長度）
            if np.std(embedding) > 0:
                embedding = (embedding - np.mean(embedding)) / np.std(embedding)
            
            debug_log(3, f"[Speaker] 使用 fallback 嵌入，維度: {embedding.shape}")
            return embedding
            
        except Exception as e:
            error_log(f"[Speaker] Fallback 嵌入計算失敗: {str(e)}")
            # 最後的備用方案：返回隨機向量
            return np.random.normal(0, 1, 32)
    
    def _fallback_speaker_identification(self, audio_data: np.ndarray) -> SpeakerInfo:
        """回退的說話人識別方法（當 pyannote 不可用時）"""
        try:
            debug_log(2, "[Speaker] 使用回退說話人識別...")
            
            # 計算基本特徵
            embedding = self._compute_audio_embedding(audio_data)

            # 嘗試使用本地 DBSCAN 進行高相似度匹配（若配置啟用）
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, match_distances = dbscan_result
                    # 保存樣本並回傳
                    self.speaker_database[match_id]['embeddings'].append(embedding)
                    self.speaker_database[match_id]['metadata']['last_seen'] = time.time()
                    self.speaker_database[match_id]['metadata']['sample_count'] += 1
                    self._save_speaker_database()
                    voice_features = {
                        'method': 'dbscan_match_fallback',
                        'similarity': match_similarity,
                        'distances': match_distances
                    }
                    return SpeakerInfo(
                        speaker_id=match_id,
                        confidence=match_similarity,
                        is_new_speaker=False,
                        voice_features=voice_features
                    )
            except Exception:
                pass
            
            # 檢查已知說話人（只檢查達到最小樣本數的說話人）
            qualified_speakers = self._get_qualified_speakers()
            best_match_id = None
            best_similarity = 0.0
            best_distance_score = float('inf')
            all_distances = {}
            
            # 使用多距離計算或單純餘弦相似度
            if self.use_multi_distance:
                debug_log(3, f"[Speaker] 使用多距離計算進行回退識別")
                
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        # 計算多種距離
                        distances = self._calculate_multi_distance(embedding, known_embedding)
                        combined_score = self._combine_distances(distances)
                        
                        # 保存最佳匹配
                        if combined_score < best_distance_score:
                            best_distance_score = combined_score
                            best_match_id = known_id
                            all_distances = distances
                            
                            # 同時計算餘弦相似度以保持兼容性
                            cosine_similarity = 1 - distances.get('cosine', 0)
                            best_similarity = cosine_similarity
                
                # 閾值判斷 - 多距離評分較低表示更相似
                # 在回退模式中我們使用較寬鬆的閾值
                similarity_check = best_distance_score < self.fallback_multi_distance_threshold
            else:
                # 傳統餘弦相似度計算
                for known_id, data in qualified_speakers.items():
                    for known_embedding in data['embeddings']:
                        similarity = 1 - cosine(embedding, known_embedding)
                        if similarity > best_similarity:
                            best_similarity = similarity
                            best_match_id = known_id
                
                # 傳統閾值判斷
                similarity_check = best_similarity > self.similarity_threshold
            
            if similarity_check:
                # 添加新的嵌入樣本到已知說話人
                self.speaker_database[best_match_id]['embeddings'].append(embedding)
                self.speaker_database[best_match_id]['metadata']['last_seen'] = time.time()
                self.speaker_database[best_match_id]['metadata']['sample_count'] += 1
                self._save_speaker_database()
                
                if self.use_multi_distance:
                    debug_log(3, f"[Speaker] [Fallback] 匹配已知說話人: {best_match_id} (多距離分數: {best_distance_score:.3f})")
                    confidence = self._confidence_from_scores(best_similarity, best_distance_score, self.fallback_multi_distance_threshold)
                    
                    # 構建豐富的語音特徵
                    voice_features = {
                        "method": "fallback_multi_distance",
                        "similarity": best_similarity,
                        "combined_score": best_distance_score
                    }
                    
                    # 添加每種距離的詳細信息
                    for dist_type, value in all_distances.items():
                        voice_features[f"distance_{dist_type}"] = value
                else:
                    debug_log(3, f"[Speaker] [Fallback] 匹配已知說話人: {best_match_id} (相似度: {best_similarity:.3f}, 閾值: {self.similarity_threshold})")
                    confidence = min(best_similarity, self.confidence_max_cap)
                    voice_features = {"method": "fallback", "similarity": best_similarity}
                
                return SpeakerInfo(
                    speaker_id=best_match_id,
                    confidence=confidence,
                    is_new_speaker=False,
                    voice_features=voice_features
                )
            
            # 沒有匹配的合格說話人，使用工作上下文累積樣本
            context_id = working_context_manager.add_data_to_context(
                context_type=ContextType.SPEAKER_ACCUMULATION, 
                data_item=embedding,
                metadata={
                    "method": "fallback",
                    "best_similarity": best_similarity,
                    "timestamp": time.time()
                }
            )
            
            # 獲取上下文信息
            context_info = working_context_manager.get_context_status(context_id)
            if context_info:
                data_count = context_info['data_count']
                debug_log(3, f"[Speaker] [Fallback] 樣本添加到工作上下文 {context_id} (樣本數: {data_count})")
                
                return SpeakerInfo(
                    speaker_id=f"context_{context_id}",
                    confidence=0.4,  # 較低信心度，表示 fallback 模式
                    is_new_speaker=False,  # 不確定是否為新說話人
                    voice_features={
                        "method": "fallback_context_accumulation",
                        "context_id": context_id,
                        "data_count": data_count,  # 使用正確的字段名
                        "threshold": self.context_sample_threshold,
                        "status": "accumulating"
                    }
                )
            else:
                return SpeakerInfo(
                    speaker_id="fallback_context_pending",
                    confidence=0.4,
                    is_new_speaker=False,
                    voice_features={
                        "method": "fallback_context_pending",
                        "status": "unknown"
                    }
                )
            
        except Exception as e:
            error_log(f"[Speaker] 回退識別失敗: {str(e)}")
            return SpeakerInfo(
                speaker_id="unknown",
                confidence=0.0,
                is_new_speaker=False,
                voice_features=None
            )
    
    def _load_speaker_database(self):
        """載入說話人資料庫"""
        try:
            if os.path.exists(self.database_path):
                with open(self.database_path, 'rb') as f:
                    data = pickle.load(f)
                    self.speaker_database = data.get('database', {})
                    self.speaker_counter = data.get('counter', 0)
                info_log(f"[Speaker] 載入說話人資料庫: {len(self.speaker_database)} 位說話人")
            else:
                info_log("[Speaker] 創建新的說話人資料庫")
        except Exception as e:
            error_log(f"[Speaker] 載入資料庫失敗: {e}")
            self.speaker_database = {}
            self.speaker_counter = 0
    
    def _save_speaker_database(self):
        """儲存說話人資料庫"""
        try:
            os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
            data = {
                'database': self.speaker_database,
                'counter': self.speaker_counter
            }
            with open(self.database_path, 'wb') as f:
                pickle.dump(data, f)
            debug_log(3, f"[Speaker] 儲存說話人資料庫: {len(self.speaker_database)} 位說話人")
        except Exception as e:
            error_log(f"[Speaker] 儲存資料庫失敗: {e}")
    
    def get_speaker_info(self, speaker_id: str) -> Optional[Dict]:
        """取得說話人詳細資訊"""
        return self.speaker_database.get(speaker_id)
    
    def list_speakers(self) -> Dict[str, Dict]:
        """列出所有說話人ID和詳細資訊"""
        result = {}
        for speaker_id, data in self.speaker_database.items():
            metadata = data.get('metadata', {})
            result[speaker_id] = {
                'embeddings': len(data.get('embeddings', [])),
                'created_at': metadata.get('created_at'),
                'last_seen': metadata.get('last_seen'),
                'sample_count': metadata.get('sample_count', 0)
            }
        return result
    
    def rename_speaker(self, old_id: str, new_id: str) -> bool:
        """重新命名說話人"""
        try:
            if old_id not in self.speaker_database:
                error_log(f"[Speaker] 說話人 '{old_id}' 不存在")
                return False
            
            if new_id in self.speaker_database:
                error_log(f"[Speaker] 說話人 '{new_id}' 已存在")
                return False
            
            # 移動數據
            self.speaker_database[new_id] = self.speaker_database.pop(old_id)
            self._save_speaker_database()
            
            info_log(f"[Speaker] 說話人 '{old_id}' 已重新命名為 '{new_id}'")
            return True
            
        except Exception as e:
            error_log(f"[Speaker] 重新命名失敗: {e}")
            return False
    
    def delete_speaker(self, speaker_id: str) -> bool:
        """刪除指定說話人"""
        try:
            if speaker_id not in self.speaker_database:
                error_log(f"[Speaker] 說話人 '{speaker_id}' 不存在")
                return False
            
            del self.speaker_database[speaker_id]
            self._save_speaker_database()
            
            info_log(f"[Speaker] 說話人 '{speaker_id}' 已刪除")
            return True
            
        except Exception as e:
            error_log(f"[Speaker] 刪除失敗: {e}")
            return False
    
    def clear_all_speakers(self) -> bool:
        """清空所有說話人數據"""
        try:
            self.speaker_database.clear()
            self.speaker_counter = 0
            self._save_speaker_database()
            
            info_log("[Speaker] 所有說話人數據已清空")
            return True
            
        except Exception as e:
            error_log(f"[Speaker] 清空失敗: {e}")
            return False
    
    def backup_speakers(self, backup_path: str) -> bool:
        """備份說話人數據到指定路徑"""
        try:
            import shutil
            
            if os.path.exists(self.database_path):
                shutil.copy2(self.database_path, backup_path)
                info_log(f"[Speaker] 說話人數據已備份至: {backup_path}")
                return True
            else:
                error_log("[Speaker] 原始數據庫不存在，無法備份")
                return False
                
        except Exception as e:
            error_log(f"[Speaker] 備份失敗: {e}")
            return False
    
    def restore_speakers(self, backup_path: str) -> bool:
        """從備份恢復說話人數據"""
        try:
            import shutil
            
            if not os.path.exists(backup_path):
                error_log(f"[Speaker] 備份檔案不存在: {backup_path}")
                return False
            
            # 備份當前數據（如果存在）
            if os.path.exists(self.database_path):
                current_backup = f"{self.database_path}.pre_restore"
                shutil.copy2(self.database_path, current_backup)
                info_log(f"[Speaker] 當前數據已備份至: {current_backup}")
            
            # 恢復備份
            shutil.copy2(backup_path, self.database_path)
            
            # 重新載入
            self._load_speaker_database()
            
            info_log(f"[Speaker] 說話人數據已從備份恢復: {backup_path}")
            return True
            
        except Exception as e:
            error_log(f"[Speaker] 恢復失敗: {e}")
            return False
    
    def get_database_info(self) -> Dict:
        """獲取說話人資料庫統計信息"""
        try:
            total_speakers = len(self.speaker_database)
            total_samples = sum(len(data.get('embeddings', [])) for data in self.speaker_database.values())
            
            # 計算資料庫檔案大小
            file_size = 0
            if os.path.exists(self.database_path):
                file_size = os.path.getsize(self.database_path)
            
            return {
                'total_speakers': total_speakers,
                'total_samples': total_samples,
                'file_size_bytes': file_size,
                'file_size_mb': file_size / (1024 * 1024),
                'database_path': self.database_path,
                'similarity_threshold': self.similarity_threshold,
                'multi_distance': {
                    'enabled': self.use_multi_distance,
                    'weights': self.distance_weights
                }
            }
            
        except Exception as e:
            error_log(f"[Speaker] 獲取資料庫信息失敗: {e}")
            return {}
    
    def update_similarity_threshold(self, threshold: float):
        """更新相似度閾值"""
        clamped = max(0.0, min(1.0, threshold))
        if clamped != threshold:
            info_log(f"[Speaker] 輸入的相似度閾值 {threshold} 超出範圍，已自動調整為 {clamped}")
        self.similarity_threshold = clamped
        info_log(f"[Speaker] 更新相似度閾值: {self.similarity_threshold}")
    
    def _calculate_skewness(self, data: np.ndarray) -> float:
        """計算偏態"""
        mean = np.mean(data)
        std = np.std(data)
        if std == 0:
            return 0
        return np.mean(((data - mean) / std) ** 3)
    
    def _calculate_kurtosis(self, data: np.ndarray) -> float:
        """計算峰態"""
        mean = np.mean(data)
        std = np.std(data)
        if std == 0:
            return 0
        return np.mean(((data - mean) / std) ** 4) - 3
        
    def _extract_enhanced_features(self, embedding: np.ndarray) -> Dict[str, float]:
        """提取增強特徵來區分高相似度嵌入"""
        features = {}
        
        # 1. 向量大小（範數）
        features['magnitude'] = np.linalg.norm(embedding)
        
        # 2. 各個維度的統計特徵
        features['mean'] = np.mean(embedding)
        features['std'] = np.std(embedding)
        features['skewness'] = self._calculate_skewness(embedding)
        features['kurtosis'] = self._calculate_kurtosis(embedding)
        
        # 3. 正負值比例
        features['positive_ratio'] = np.sum(embedding > 0) / len(embedding)
        
        # 4. 最大值和最小值
        features['max_value'] = np.max(embedding)
        features['min_value'] = np.min(embedding)
        
        # 5. 能量分佈（分段能量）
        segments = np.array_split(embedding, 4)
        for i, segment in enumerate(segments):
            features[f'segment_{i}_energy'] = np.sum(segment ** 2)
        
        return features
    
    def _calculate_multi_distance(self, emb1: np.ndarray, emb2: np.ndarray) -> Dict[str, float]:
        """計算多種距離指標以提高語者辨識準確度"""
        distances = {}
        
        # 標準化嵌入
        norm_emb1 = normalize([emb1])[0] if 'sklearn.preprocessing' in globals() else emb1 / np.linalg.norm(emb1)
        norm_emb2 = normalize([emb2])[0] if 'sklearn.preprocessing' in globals() else emb2 / np.linalg.norm(emb2)
        
        # 1. 餘弦距離
        distances['cosine'] = cosine(norm_emb1, norm_emb2)
        
        # 2. 歐幾里得距離（原始）
        distances['euclidean'] = euclidean(emb1, emb2)
        
        # 3. 向量大小差異
        if self.use_magnitude_difference:
            distances['magnitude'] = abs(np.linalg.norm(emb1) - np.linalg.norm(emb2))
        
        # 4. 皮爾森相關距離
        if np.std(emb1) > 0 and np.std(emb2) > 0:
            correlation = np.corrcoef(emb1, emb2)[0, 1]
            distances['correlation'] = 1 - abs(correlation)
        else:
            distances['correlation'] = 1.0
        
        # 5. 增強特徵距離
        if self.use_enhanced_features:
            features1 = self._extract_enhanced_features(emb1)
            features2 = self._extract_enhanced_features(emb2)
            
            feature_diff = 0
            for key in features1:
                if key in features2:
                    feature_diff += (features1[key] - features2[key]) ** 2
            distances['enhanced'] = np.sqrt(feature_diff)
        
        return distances
    
    def _combine_distances(self, distances: Dict[str, float]) -> float:
        """結合多種距離成單一分數"""
        combined = 0
        total_weight = 0
        
        for dist_type, weight in self.distance_weights.items():
            if dist_type in distances:
                # 正規化不同類型的距離
                if dist_type == 'magnitude':
                    # 向量大小差異正規化
                    normalized_dist = min(distances[dist_type] / 50.0, 1.0)
                elif dist_type == 'euclidean':
                    # 歐幾里得距離正規化
                    normalized_dist = min(distances[dist_type] / 100.0, 1.0)
                else:
                    # 餘弦和相關距離已經在 [0, 1] 範圍內
                    normalized_dist = distances[dist_type]
                
                combined += weight * normalized_dist
                total_weight += weight
        
        return combined / total_weight if total_weight > 0 else 1.0

    def _confidence_from_scores(self, cosine_similarity: float, combined_score: float, threshold: float) -> float:
        """將多距離分數與餘弦相似度映射為 0-1 之間的置信度並加上上限。
        - 當 combined_score 越接近 0 代表越相似；使用 (1 - min(1, combined/threshold)) 做一個粗略映射。
        - 同時參考餘弦相似度，取兩者的平均以降低單一量測的偏差。
        - 最後以 confidence_max_cap 限制上限，避免 1.00。
        """
        if threshold <= 0:
            base = 0.0
        else:
            normalized = max(0.0, 1.0 - min(1.0, combined_score / threshold))
            base = normalized
        conf = (base + max(0.0, min(1.0, cosine_similarity))) / 2.0
        return min(conf, self.confidence_max_cap)

    def _find_best_speaker_match(self, session_embeddings: List[np.ndarray], qualified_speakers: Dict[str, Dict]) -> Optional[Tuple[str, float]]:
        """找到與 session 樣本最匹配的合格說話人"""
        best_speaker = None
        best_avg_similarity = 0.0
        
        for speaker_id in qualified_speakers.keys():
            speaker_data = self.speaker_database[speaker_id]
            speaker_embeddings = speaker_data['embeddings']
            
            # 計算 session 樣本與該說話人的平均相似度
            similarities = []
            for session_emb in session_embeddings:
                for speaker_emb in speaker_embeddings:
                    if self.use_multi_distance:
                        distances = self._calculate_multi_distance(session_emb, speaker_emb)
                        combined_score = self._combine_distances(distances)
                        similarity = 1 - min(1.0, combined_score / self.multi_distance_threshold)
                    else:
                        similarity = 1 - cosine(session_emb, speaker_emb)
                    similarities.append(similarity)
            
            if similarities:
                avg_similarity = np.mean(similarities)
                if avg_similarity > best_avg_similarity:
                    best_avg_similarity = avg_similarity
                    best_speaker = speaker_id
        
        # 只有相似度夠高才返回匹配
        if best_avg_similarity > 0.7:  # 可調整的閾值
            return best_speaker, best_avg_similarity
        return None

    def update_distance_weights(self, weights: Dict[str, float]):
        """更新距離權重設置"""
        for key, value in weights.items():
            if key in self.distance_weights:
                self.distance_weights[key] = value
        info_log(f"[Speaker] 更新距離權重: {self.distance_weights}")

    def _find_best_speaker_match(self, embeddings: List[np.ndarray], 
                               qualified_speakers: Optional[Dict] = None) -> Optional[Tuple[str, float]]:
        """為工作上下文中的樣本找到最佳的說話人匹配"""
        if qualified_speakers is None:
            qualified_speakers = self._get_qualified_speakers()
        
        if not qualified_speakers or not embeddings:
            return None
        
        # 計算樣本的平均嵌入
        avg_embedding = np.mean(embeddings, axis=0)
        
        best_match_id = None
        best_similarity = 0.0
        best_distance_score = float('inf')
        
        # 使用多距離計算或單純餘弦相似度
        if self.use_multi_distance:
            for known_id, data in qualified_speakers.items():
                for known_embedding in data['embeddings']:
                    distances = self._calculate_multi_distance(avg_embedding, known_embedding)
                    combined_score = self._combine_distances(distances)
                    
                    if combined_score < best_distance_score:
                        best_distance_score = combined_score
                        best_match_id = known_id
                        cosine_similarity = 1 - distances['cosine']
                        best_similarity = cosine_similarity
            
            # 檢查是否滿足閾值
            if best_distance_score < self.multi_distance_threshold:
                return best_match_id, best_similarity
        else:
            for known_id, data in qualified_speakers.items():
                for known_embedding in data['embeddings']:
                    similarity = 1 - cosine(avg_embedding, known_embedding)
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match_id = known_id
            
            # 檢查是否滿足閾值
            if best_similarity > self.similarity_threshold:
                return best_match_id, best_similarity
        
        return None

    def get_working_context_status(self) -> Dict[str, Any]:
        """獲取工作上下文狀態"""
        context_info = working_context_manager.get_all_contexts_info()
        speaker_contexts = [
            ctx for ctx in context_info 
            if ctx.get('type') == ContextType.SPEAKER_ACCUMULATION.value
        ]
        
        return {
            'active_speaker_contexts': len(speaker_contexts),
            'contexts': speaker_contexts,
            'context_threshold': self.context_sample_threshold
        }
    
    def reset_speaker_database(self, backup: bool = True):
        """重建說話人資料庫，可選擇是否備份"""
        if backup and self.speaker_database:
            # 備份現有資料庫
            backup_path = f"{self.database_path}.backup_{int(time.time())}"
            try:
                with open(backup_path, 'wb') as f:
                    data = {
                        'database': self.speaker_database,
                        'counter': self.speaker_counter
                    }
                    pickle.dump(data, f)
                info_log(f"[Speaker] 資料庫已備份至: {backup_path}")
            except Exception as e:
                error_log(f"[Speaker] 備份失敗: {e}")
        
        # 清空資料庫
        self.speaker_database = {}
        self.speaker_counter = 0
        self._save_speaker_database()
        info_log("[Speaker] 說話人資料庫已重建")
    
    def get_database_status(self) -> Dict:
        """取得資料庫狀態資訊"""
        qualified_speakers = self._get_qualified_speakers()
        total_samples = sum(len(data.get('embeddings', [])) for data in self.speaker_database.values())
        qualified_samples = sum(len(data.get('embeddings', [])) for data in qualified_speakers.values())
        
        return {
            'total_speakers': len(self.speaker_database),
            'qualified_speakers': len(qualified_speakers),
            'min_samples_threshold': self.min_samples_for_recognition,
            'total_samples': total_samples,
            'qualified_samples': qualified_samples,
            'unqualified_speakers': len(self.speaker_database) - len(qualified_speakers)
        }

    def _get_qualified_speakers(self) -> Dict[str, Dict]:
        """返回達到最小樣本數要求的說話人資料"""
        qualified = {}
        for speaker_id, data in self.speaker_database.items():
            sample_count = data.get('metadata', {}).get('sample_count', len(data.get('embeddings', [])))
            if sample_count >= self.min_samples_for_recognition:
                qualified[speaker_id] = data
        return qualified

    def _dbscan_match(self, embedding: np.ndarray) -> Optional[Tuple[str, float, Dict[str, float]]]:
        """使用 DBSCAN 在本地 speaker_database 上進行快速聚類匹配，回傳 (speaker_id, similarity, distances) 或 None
        這個方法構造一個暫時的嵌入列表，執行 DBSCAN，並檢查新 embedding 是否被分到某個已有的類別。
        若找到類別，會回傳該類別最接近的新樣本的 speaker_id 與相似度/距離細節。
        """
        try:
            # 只使用達到最小樣本數要求的說話人
            qualified_speakers = self._get_qualified_speakers()
            if not qualified_speakers:
                return None

            # 構建現有嵌入矩陣與對應說話人映射
            embeddings = []
            labels = []  # 對應的 speaker_id
            for spk_id, data in qualified_speakers.items():
                for emb in data.get('embeddings', []):
                    embeddings.append(emb)
                    labels.append(spk_id)

            if len(embeddings) == 0:
                return None

            X = np.vstack(embeddings)

            # 使用 sklearn DBSCAN
            db = DBSCAN(eps=self.dbscan_eps, min_samples=self.dbscan_min_samples, metric=self.dbscan_metric)
            clusters = db.fit_predict(X)

            # 把新 embedding 與所有 embeddings 做距離比較，看是否屬於某個 cluster
            # 若某些點形成 cluster >=0，找出這些 cluster 中最接近新 embedding 的點
            dists = np.array([cosine(embedding, e) if self.dbscan_metric == 'cosine' else euclidean(embedding, e) for e in embeddings])
            # 取得最小距離索引
            min_idx = int(np.argmin(dists))
            min_cluster = clusters[min_idx]
            if min_cluster == -1:
                return None

            # 在此 cluster 中，取得最接近的新樣本索引並回傳其 speaker_id
            cluster_indices = np.where(clusters == min_cluster)[0]
            best_local_idx = cluster_indices[np.argmin(dists[cluster_indices])]
            matched_speaker = labels[best_local_idx]

            # 計算多重距離細項供回傳
            matched_embedding = embeddings[best_local_idx]
            distances = self._calculate_multi_distance(embedding, matched_embedding) if self.use_multi_distance else {'cosine': cosine(embedding, matched_embedding)}
            similarity = 1 - distances.get('cosine', 0)

            return matched_speaker, similarity, distances
        except Exception as e:
            debug_log(2, f"[Speaker] DBSCAN 匹配失敗: {e}")
            return None
    
    def shutdown(self):
        """關閉說話人識別模組"""
        self._save_speaker_database()
        info_log("[Speaker] 說話人識別模組已關閉")
