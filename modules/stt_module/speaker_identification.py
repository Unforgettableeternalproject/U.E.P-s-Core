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
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

try:
    import torch
    import torchaudio
    from pyannote.audio import Pipeline as PyannoteTPipeline
    from pyannote.core import Segment
    from scipy.spatial.distance import cosine
    PYANNOTE_AVAILABLE = True
except ImportError:
    PYANNOTE_AVAILABLE = False

from utils.debug_helper import debug_log, info_log, error_log
from .schemas import SpeakerInfo

class SpeakerIdentification:
    """說話人識別和管理類"""
    
    def __init__(self, model_name: str = "pyannote/speaker-diarization-3.1"):
        self.model_name = model_name
        self.pipeline = None
        self.embedding_model = None
        self.sample_rate = 16000
        
        # 說話人資料庫
        self.speaker_database = {}  # {speaker_id: {'embeddings': [], 'metadata': {}}}
        self.speaker_counter = 0
        self.similarity_threshold = 0.85  # 說話人相似度閾值
        
        # 儲存路徑
        self.database_path = "memory/speaker_models.pkl"
        
        # 載入 HuggingFace Token
        self.hf_token = os.getenv("HUGGING_FACE_TOKEN")
        
        info_log("[Speaker] 說話人識別模組初始化")
        
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
            
            # 檢查 pipeline 是否成功載入
            if self.pipeline is None:
                error_log("[Speaker] Pipeline 載入失敗，將使用 fallback 模式")
                self._load_speaker_database()
                return True
            
            # 設置設備
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.pipeline.to(torch.device(device))
            
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
                debug_log(2, "[Speaker] 未檢測到說話人")
                return SpeakerInfo(
                    speaker_id="no_speaker",
                    confidence=0.0,
                    is_new_speaker=False,
                    voice_features=None
                )
            
            # 找到持續時間最長的說話人
            main_speaker = max(speakers, key=lambda x: x['duration'])
            raw_speaker_id = main_speaker['speaker']
            confidence = min(main_speaker['duration'] / (len(audio_data) / self.sample_rate), 1.0)
            
            # 映射到已知說話人或創建新說話人
            speaker_id, is_new = self._map_speaker(raw_speaker_id, audio_data)
            
            debug_log(2, f"[Speaker] 識別到說話人: {speaker_id}, 信心度: {confidence:.2f}")
            
            return SpeakerInfo(
                speaker_id=speaker_id,
                confidence=confidence,
                is_new_speaker=is_new,
                voice_features={
                    "duration": main_speaker['duration'], 
                    "segments": len(speakers),
                    "raw_id": raw_speaker_id
                }
            )
            
        except Exception as e:
            error_log(f"[Speaker] 說話人識別失敗: {str(e)}")
            return self._fallback_speaker_identification(audio_data)
    
    def _map_speaker(self, raw_speaker_id: str, audio_data: np.ndarray) -> Tuple[str, bool]:
        """將原始說話人ID映射到持久化的說話人ID"""
        try:
            # 計算音頻特徵作為嵌入
            embedding = self._compute_audio_embedding(audio_data)
            
            # 檢查是否為已知說話人
            best_match_id = None
            best_similarity = 0.0
            
            for known_id, data in self.speaker_database.items():
                for known_embedding in data['embeddings']:
                    similarity = 1 - cosine(embedding, known_embedding)
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match_id = known_id
            
            # 如果相似度超過閾值，返回已知說話人
            if best_similarity > self.similarity_threshold:
                # 添加新的嵌入樣本
                self.speaker_database[best_match_id]['embeddings'].append(embedding)
                self.speaker_database[best_match_id]['metadata']['last_seen'] = time.time()
                self.speaker_database[best_match_id]['metadata']['sample_count'] += 1
                self._save_speaker_database()
                
                debug_log(3, f"[Speaker] 匹配已知說話人: {best_match_id} (相似度: {best_similarity:.3f})")
                return best_match_id, False
            
            # 創建新說話人
            new_speaker_id = f"speaker_{self.speaker_counter:03d}"
            self.speaker_counter += 1
            
            self.speaker_database[new_speaker_id] = {
                'embeddings': [embedding],
                'metadata': {
                    'created_at': time.time(),
                    'last_seen': time.time(),
                    'sample_count': 1,
                    'raw_ids': [raw_speaker_id]
                }
            }
            
            self._save_speaker_database()
            
            debug_log(3, f"[Speaker] 創建新說話人: {new_speaker_id}")
            return new_speaker_id, True
            
        except Exception as e:
            error_log(f"[Speaker] 說話人映射失敗: {str(e)}")
            return f"error_{int(time.time())}", True
    
    def _compute_audio_embedding(self, audio_data: np.ndarray) -> np.ndarray:
        """計算音頻特徵嵌入"""
        try:
            audio_float = audio_data.astype(np.float32) / 32768.0
            
            # 計算基本音頻特徵
            mean_amplitude = np.mean(np.abs(audio_float))
            rms_energy = np.sqrt(np.mean(audio_float**2))
            zero_crossing_rate = np.mean(np.diff(np.sign(audio_float)) != 0)
            
            # 計算頻譜特徵
            fft = np.fft.fft(audio_float)
            magnitude = np.abs(fft)
            spectral_centroid = np.sum(np.arange(len(magnitude)) * magnitude) / np.sum(magnitude)
            
            # 計算 MFCC 風格的特徵
            # 簡化版本，實際應用中應該使用更複雜的特徵
            n_segments = 10
            segment_length = len(audio_float) // n_segments
            segment_features = []
            
            for i in range(n_segments):
                start = i * segment_length
                end = min((i + 1) * segment_length, len(audio_float))
                segment = audio_float[start:end]
                
                if len(segment) > 0:
                    seg_energy = np.sqrt(np.mean(segment**2))
                    seg_zcr = np.mean(np.diff(np.sign(segment)) != 0)
                    segment_features.extend([seg_energy, seg_zcr])
            
            # 組合所有特徵
            embedding = np.array([
                mean_amplitude, rms_energy, zero_crossing_rate, spectral_centroid
            ] + segment_features)
            
            # 正規化
            if np.linalg.norm(embedding) > 0:
                embedding = embedding / np.linalg.norm(embedding)
            
            return embedding
            
        except Exception as e:
            error_log(f"[Speaker] 計算嵌入失敗: {str(e)}")
            return np.random.random(24)  # 回退到隨機特徵
    
    def _fallback_speaker_identification(self, audio_data: np.ndarray) -> SpeakerInfo:
        """回退的說話人識別方法（當 pyannote 不可用時）"""
        try:
            debug_log(2, "[Speaker] 使用回退說話人識別...")
            
            # 計算基本特徵
            embedding = self._compute_audio_embedding(audio_data)
            
            # 檢查已知說話人
            best_match_id = None
            best_similarity = 0.0
            
            for known_id, data in self.speaker_database.items():
                for known_embedding in data['embeddings']:
                    similarity = 1 - cosine(embedding, known_embedding)
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match_id = known_id
            
            if best_similarity > self.similarity_threshold:
                return SpeakerInfo(
                    speaker_id=best_match_id,
                    confidence=best_similarity,
                    is_new_speaker=False,
                    voice_features={"method": "fallback", "similarity": best_similarity}
                )
            
            # 創建新說話人
            new_speaker_id = f"speaker_{self.speaker_counter:03d}"
            self.speaker_counter += 1
            
            self.speaker_database[new_speaker_id] = {
                'embeddings': [embedding],
                'metadata': {
                    'created_at': time.time(),
                    'last_seen': time.time(),
                    'sample_count': 1,
                    'method': 'fallback'
                }
            }
            
            self._save_speaker_database()
            
            return SpeakerInfo(
                speaker_id=new_speaker_id,
                confidence=0.7,  # 中等信心度
                is_new_speaker=True,
                voice_features={"method": "fallback"}
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
                'similarity_threshold': self.similarity_threshold
            }
            
        except Exception as e:
            error_log(f"[Speaker] 獲取資料庫信息失敗: {e}")
            return {}
    
    def update_similarity_threshold(self, threshold: float):
        """更新相似度閾值"""
        self.similarity_threshold = max(0.0, min(1.0, threshold))
        info_log(f"[Speaker] 更新相似度閾值: {self.similarity_threshold}")
    
    def shutdown(self):
        """關閉說話人識別模組"""
        self._save_speaker_database()
        info_log("[Speaker] 說話人識別模組已關閉")
