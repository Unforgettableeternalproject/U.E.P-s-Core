#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
說話人識別模組 — 使用 pyannote.audio

從 U.E.P Core 遷出：
- 移除 core.working_context 依賴 → 可選注入 context_manager
- 移除 utils.debug_helper → 標準 logging
- 修復 Bug #1: context_info UnboundLocalError（兩處）
- 修復 Bug #3: _compute_fallback_embedding 移除隨機噪聲
"""

import os
import time
import pickle
import logging
import tempfile
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()

from echo_stt.speaker.context_handler import ContextType, ContextManagerProtocol

# 延遲導入避免循環依賴
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from echo_stt.core.schemas import SpeakerInfo

logger = logging.getLogger(__name__)

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
    torch = None  # type: ignore
    torchaudio = None  # type: ignore
    PyannoteTPipeline = None  # type: ignore
    Model = None  # type: ignore
    Segment = None  # type: ignore
    cosine = None  # type: ignore
    euclidean = None  # type: ignore
    normalize = None  # type: ignore
    DBSCAN = None  # type: ignore
    PYANNOTE_AVAILABLE = False


def _get_speaker_info_class():
    """延遲導入 SpeakerInfo 避免循環依賴"""
    from echo_stt.core.schemas import SpeakerInfo
    return SpeakerInfo


class SpeakerIdentification:
    """說話人識別和管理類 — 整合高相似度語者辨識系統"""

    def __init__(self, config: Optional[dict] = None,
                 model_name: str = "pyannote/speaker-diarization-3.1",
                 context_manager: Optional[Any] = None):
        self.model_name = model_name
        self.pipeline = None
        self.embedding_model = None
        self.sample_rate = 16000

        # 可選的 context manager（替代原本直接引用的 working_context_manager）
        self.context_manager: Optional[ContextManagerProtocol] = context_manager

        # 說話人資料庫
        self.speaker_database: Dict[str, Dict] = {}
        self.speaker_counter = 0
        self.similarity_threshold = 0.999995

        # 儲存路徑
        self.database_path = "memory/speaker_models.pkl"

        # HuggingFace Token
        self.hf_token = os.getenv("HUGGING_FACE_TOKEN")

        # 高相似度語者辨識系統配置
        self.use_multi_distance = True
        self.use_enhanced_features = True
        self.use_magnitude_difference = True

        self.distance_weights = {
            'cosine': 0.3,
            'euclidean': 0.3,
            'magnitude': 0.2,
            'correlation': 0.2,
        }
        self.dbscan_eps = 0.001
        self.dbscan_min_samples = 1
        self.dbscan_metric = 'cosine'

        self.multi_distance_threshold = 0.15
        self.fallback_multi_distance_threshold = 0.25
        self.confidence_max_cap = 0.98

        self.context_sample_threshold = 15
        self.min_samples_for_recognition = 15

        # 從配置中讀取設置
        if config:
            speaker_config = config.get('speaker_recognition', config.get('speaker', {}))
            self.use_multi_distance = speaker_config.get('use_multi_distance', self.use_multi_distance)
            self.use_enhanced_features = speaker_config.get('use_enhanced_features', self.use_enhanced_features)
            self.use_magnitude_difference = speaker_config.get('use_magnitude_difference', self.use_magnitude_difference)

            weights = speaker_config.get('distance_weights', {})
            if weights:
                for key, value in weights.items():
                    if key in self.distance_weights:
                        self.distance_weights[key] = value

            self.dbscan_eps = speaker_config.get('dbscan_eps', self.dbscan_eps)
            self.dbscan_min_samples = speaker_config.get('dbscan_min_samples', self.dbscan_min_samples)
            self.dbscan_metric = speaker_config.get('dbscan_metric', self.dbscan_metric)
            self.multi_distance_threshold = speaker_config.get('multi_distance_threshold', self.multi_distance_threshold)
            self.fallback_multi_distance_threshold = speaker_config.get('fallback_multi_distance_threshold', self.fallback_multi_distance_threshold)
            self.confidence_max_cap = speaker_config.get('confidence_max_cap', self.confidence_max_cap)
            self.context_sample_threshold = speaker_config.get('context_sample_threshold', self.context_sample_threshold)
            self.min_samples_for_recognition = speaker_config.get('min_samples_for_recognition', self.min_samples_for_recognition)

        logger.info("[Speaker] 說話人識別模組初始化 (已整合高相似度辨識功能)")

    # ──────────────────────────────────────────────
    #  初始化
    # ──────────────────────────────────────────────

    def initialize(self) -> bool:
        """初始化說話人識別模型"""
        if not PYANNOTE_AVAILABLE:
            logger.error("[Speaker] pyannote.audio 不可用，將使用 fallback 模式")
            self._load_speaker_database()
            return True

        if not self.hf_token:
            logger.error("[Speaker] HuggingFace Token 未設定，將使用 fallback 模式")
            self._load_speaker_database()
            return True

        try:
            logger.info("[Speaker] 載入模型: %s", self.model_name)

            self.pipeline = PyannoteTPipeline.from_pretrained(  # type: ignore
                self.model_name, use_auth_token=self.hf_token,
            )

            try:
                self.embedding_model = Model.from_pretrained(  # type: ignore
                    "pyannote/embedding", use_auth_token=self.hf_token,
                )
                logger.info("[Speaker] 嵌入模型載入成功")
            except Exception as e:
                logger.error("[Speaker] 嵌入模型載入失敗: %s", e)
                self.embedding_model = None

            if self.pipeline is None:
                logger.error("[Speaker] Pipeline 載入失敗，將使用 fallback 模式")
                self._load_speaker_database()
                return True

            device = "cuda" if torch.cuda.is_available() else "cpu"  # type: ignore
            self.pipeline.to(torch.device(device))  # type: ignore
            if self.embedding_model:
                self.embedding_model.to(torch.device(device))  # type: ignore

            logger.info("[Speaker] 模型載入成功 (設備: %s)", device)
            self._load_speaker_database()
            return True

        except Exception as e:
            logger.error("[Speaker] 模型載入失敗: %s", e)
            logger.error("[Speaker] 將使用 fallback 說話人識別模式")
            self.pipeline = None
            self._load_speaker_database()
            return True

    # ──────────────────────────────────────────────
    #  核心匹配引擎（重構：共用邏輯提取）
    # ──────────────────────────────────────────────

    def _match_against_database(
        self,
        embedding: np.ndarray,
        *,
        use_fallback_threshold: bool = False,
    ) -> Tuple[Optional[str], float, float, Dict[str, float], bool]:
        """在 qualified speakers 中尋找最佳匹配。

        Returns:
            (best_match_id, best_similarity, best_distance_score, all_distances, matched)
        """
        qualified_speakers = self._get_qualified_speakers()
        best_match_id: Optional[str] = None
        best_similarity = 0.0
        best_distance_score = float('inf')
        all_distances: Dict[str, float] = {}
        matched = False

        if self.use_multi_distance:
            for known_id, data in qualified_speakers.items():
                for known_embedding in data['embeddings']:
                    distances = self._calculate_multi_distance(embedding, known_embedding)
                    combined_score = self._combine_distances(distances)

                    if combined_score < best_distance_score:
                        best_distance_score = combined_score
                        best_match_id = known_id
                        best_similarity = 1 - distances.get('cosine', 1.0)
                        all_distances = distances

            threshold = self.fallback_multi_distance_threshold if use_fallback_threshold else self.multi_distance_threshold
            matched = best_distance_score < threshold
        else:
            for known_id, data in qualified_speakers.items():
                for known_embedding in data['embeddings']:
                    similarity = 1 - cosine(embedding, known_embedding)  # type: ignore
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match_id = known_id

            matched = best_similarity > self.similarity_threshold

        return best_match_id, best_similarity, best_distance_score, all_distances, matched

    def _update_speaker_with_embedding(self, speaker_id: str, embedding: np.ndarray,
                                       raw_speaker_id: Optional[str] = None) -> None:
        """將新的 embedding 添加到已知說話人。"""
        self.speaker_database[speaker_id]['embeddings'].append(embedding)
        self.speaker_database[speaker_id]['metadata']['last_seen'] = time.time()
        self.speaker_database[speaker_id]['metadata']['sample_count'] += 1
        if raw_speaker_id is not None:
            if 'raw_ids' not in self.speaker_database[speaker_id]['metadata']:
                self.speaker_database[speaker_id]['metadata']['raw_ids'] = []
            if raw_speaker_id not in self.speaker_database[speaker_id]['metadata']['raw_ids']:
                self.speaker_database[speaker_id]['metadata']['raw_ids'].append(raw_speaker_id)
        self._save_speaker_database()

    def _build_matched_voice_features(
        self,
        method: str,
        best_similarity: float,
        best_distance_score: float,
        all_distances: Dict[str, float],
    ) -> Dict[str, Any]:
        """構建已匹配說話人的 voice_features dict。"""
        if self.use_multi_distance:
            features: Dict[str, Any] = {
                "method": method,
                "similarity": best_similarity,
                "combined_score": best_distance_score,
            }
            for dist_type, value in all_distances.items():
                features[f"distance_{dist_type}"] = value
            return features
        else:
            return {"method": method, "similarity": best_similarity}

    def _accumulate_to_context(
        self,
        embedding: np.ndarray,
        metadata: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """將 embedding 加入 context manager（若可用）。

        Returns:
            (context_id, context_info) — 若 context_manager 不可用，兩者皆為 None。
        """
        if self.context_manager is None:
            return None, None

        context_id = self.context_manager.add_data_to_context(
            ContextType.SPEAKER_ACCUMULATION,
            embedding,
            metadata=metadata,
        )

        # Bug #1 fix: 確保 context_info 有預設值
        context_info: Optional[Dict[str, Any]] = None
        if context_id:
            context_info = self.context_manager.get_context_status(context_id)

        return context_id, context_info

    def _build_context_speaker_info(
        self,
        context_id: Optional[str],
        context_info: Optional[Dict[str, Any]],
        base_confidence: float,
        method_prefix: str,
    ) -> "SpeakerInfo":
        """根據 context 累積狀態構建 SpeakerInfo。"""
        SpeakerInfo = _get_speaker_info_class()

        if context_info:
            data_count = context_info['data_count']
            is_ready = context_info.get('is_ready', False)
            logger.debug(
                "[Speaker] [%s] 樣本添加到工作上下文 %s (樣本數: %d/%d)",
                method_prefix, context_id, data_count, self.context_sample_threshold,
            )
            return SpeakerInfo(
                speaker_id=f"context_{context_id}",
                confidence=base_confidence,
                is_new_speaker=False,
                voice_features={
                    "method": f"{method_prefix}_context_accumulation",
                    "context_id": context_id,
                    "data_count": data_count,
                    "threshold": self.context_sample_threshold,
                    "is_ready": is_ready,
                    "status": "accumulating",
                },
            )
        else:
            logger.debug("[Speaker] [%s] 工作上下文不可用或無法取得狀態", method_prefix)
            return SpeakerInfo(
                speaker_id=f"{method_prefix}_context_pending",
                confidence=base_confidence,
                is_new_speaker=False,
                voice_features={
                    "method": f"{method_prefix}_context_pending",
                    "status": "unknown",
                },
            )

    # ──────────────────────────────────────────────
    #  主要識別路徑
    # ──────────────────────────────────────────────

    def identify_speaker(self, audio_data: np.ndarray) -> "SpeakerInfo":
        """識別說話人"""
        if not self.pipeline:
            return self._fallback_speaker_identification(audio_data)

        try:
            logger.debug("[Speaker] 開始說話人識別...")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                audio_float = audio_data.astype(np.float32) / 32768.0
                audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)  # type: ignore
                torchaudio.save(temp_file.name, audio_tensor, self.sample_rate)  # type: ignore
                temp_path = temp_file.name

            diarization = self.pipeline(temp_path)

            try:
                os.unlink(temp_path)
            except Exception:
                pass

            speakers = []
            for segment, _, speaker in diarization.itertracks(yield_label=True):
                speakers.append({
                    'speaker': speaker,
                    'start': segment.start,
                    'end': segment.end,
                    'duration': segment.end - segment.start,
                })

            if not speakers:
                logger.debug("[Speaker] pyannote 未檢測到說話人，使用嵌入模型直接識別")
                return self._direct_embedding_identification(audio_data)

            main_speaker = max(speakers, key=lambda x: x['duration'])
            raw_speaker_id = main_speaker['speaker']
            duration_confidence = min(main_speaker['duration'] / (len(audio_data) / self.sample_rate), 1.0)

            speaker_id, is_new, similarity = self._map_speaker(raw_speaker_id, audio_data)
            final_confidence = min(similarity, self.confidence_max_cap) if similarity > 0 else duration_confidence

            logger.debug(
                "[Speaker] 識別到說話人: %s, 置信度: %.2f",
                speaker_id, final_confidence,
            )

            SpeakerInfo = _get_speaker_info_class()
            return SpeakerInfo(
                speaker_id=speaker_id,
                confidence=final_confidence,
                is_new_speaker=is_new,
                voice_features={
                    "duration": main_speaker['duration'],
                    "segments": len(speakers),
                    "raw_id": raw_speaker_id,
                    "similarity": similarity,
                    "duration_confidence": duration_confidence,
                },
            )

        except Exception as e:
            logger.error("[Speaker] 說話人識別失敗: %s", e)
            return self._fallback_speaker_identification(audio_data)

    def _direct_embedding_identification(self, audio_data: np.ndarray) -> "SpeakerInfo":
        """當 pyannote 說話人分離失敗時，直接使用嵌入模型進行說話人識別"""
        try:
            embedding = self._compute_audio_embedding(audio_data)

            # DBSCAN 嘗試
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, match_distances = dbscan_result
                    self._update_speaker_with_embedding(match_id, embedding)
                    SpeakerInfo = _get_speaker_info_class()
                    return SpeakerInfo(
                        speaker_id=match_id,
                        confidence=match_similarity,
                        is_new_speaker=False,
                        voice_features={'method': 'dbscan_match', 'similarity': match_similarity, 'distances': match_distances},
                    )
            except Exception:
                pass

            # 資料庫匹配
            best_match_id, best_similarity, best_distance_score, all_distances, matched = \
                self._match_against_database(embedding)

            if matched and best_match_id:
                self._update_speaker_with_embedding(best_match_id, embedding)

                if self.use_multi_distance:
                    confidence = self._confidence_from_scores(float(best_similarity), best_distance_score, self.multi_distance_threshold)
                else:
                    confidence = min(best_similarity, self.confidence_max_cap)

                voice_features = self._build_matched_voice_features(
                    "multi_distance" if self.use_multi_distance else "direct_embedding",
                    best_similarity, best_distance_score, all_distances,
                )

                SpeakerInfo = _get_speaker_info_class()
                return SpeakerInfo(
                    speaker_id=best_match_id,
                    confidence=float(confidence),
                    is_new_speaker=False,
                    voice_features=voice_features,
                )

            # 未匹配 → context 累積
            context_id, context_info = self._accumulate_to_context(
                embedding,
                metadata={
                    "method": "multi_distance" if self.use_multi_distance else "direct_embedding",
                    "best_similarity": best_similarity,
                    "best_distance_score": best_distance_score if self.use_multi_distance else None,
                    "timestamp": time.time(),
                },
            )
            return self._build_context_speaker_info(context_id, context_info, 0.5, "direct")

        except Exception as e:
            logger.error("[Speaker] 直接嵌入識別失敗: %s", e)
            return self._fallback_speaker_identification(audio_data)

    def _map_speaker(self, raw_speaker_id: str, audio_data: np.ndarray) -> Tuple[str, bool, float]:
        """將原始說話人 ID 映射到持久化的說話人 ID"""
        try:
            embedding = self._compute_audio_embedding(audio_data)

            # DBSCAN 嘗試
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, _ = dbscan_result
                    return match_id, False, match_similarity
            except Exception:
                pass

            # 資料庫匹配
            best_match_id, best_similarity, best_distance_score, all_distances, matched = \
                self._match_against_database(embedding)

            if matched and best_match_id:
                self._update_speaker_with_embedding(best_match_id, embedding, raw_speaker_id)
                return best_match_id, False, float(best_similarity)

            # 未匹配 → context 累積
            context_id, context_info = self._accumulate_to_context(
                embedding,
                metadata={
                    "raw_speaker_id": raw_speaker_id,
                    "method": "speaker_mapping",
                    "best_similarity": best_similarity,
                    "timestamp": time.time(),
                },
            )

            if context_info:
                data_count = context_info['data_count']
                logger.debug("[Speaker] 樣本添加到工作上下文 %s (樣本數: %d)", context_id, data_count)
                return f"context_{context_id}", False, float(best_similarity)
            else:
                return "context_pending", False, float(best_similarity)

        except Exception as e:
            logger.error("[Speaker] 說話人映射失敗: %s", e)
            return f"error_{int(time.time())}", True, 0.0

    def _fallback_speaker_identification(self, audio_data: np.ndarray) -> "SpeakerInfo":
        """回退的說話人識別方法（當 pyannote 不可用時）"""
        try:
            logger.debug("[Speaker] 使用回退說話人識別...")
            embedding = self._compute_audio_embedding(audio_data)

            # DBSCAN 嘗試
            try:
                dbscan_result = self._dbscan_match(embedding)
                if dbscan_result is not None:
                    match_id, match_similarity, match_distances = dbscan_result
                    self._update_speaker_with_embedding(match_id, embedding)
                    SpeakerInfo = _get_speaker_info_class()
                    return SpeakerInfo(
                        speaker_id=match_id,
                        confidence=match_similarity,
                        is_new_speaker=False,
                        voice_features={'method': 'dbscan_match_fallback', 'similarity': match_similarity, 'distances': match_distances},
                    )
            except Exception:
                pass

            # 資料庫匹配
            best_match_id, best_similarity, best_distance_score, all_distances, matched = \
                self._match_against_database(embedding, use_fallback_threshold=True)

            if matched and best_match_id:
                self._update_speaker_with_embedding(best_match_id, embedding)

                if self.use_multi_distance:
                    confidence = self._confidence_from_scores(float(best_similarity), best_distance_score, self.fallback_multi_distance_threshold)
                else:
                    confidence = min(best_similarity, self.confidence_max_cap)

                voice_features = self._build_matched_voice_features(
                    "fallback_multi_distance" if self.use_multi_distance else "fallback",
                    best_similarity, best_distance_score, all_distances,
                )

                SpeakerInfo = _get_speaker_info_class()
                return SpeakerInfo(
                    speaker_id=best_match_id,
                    confidence=float(confidence),
                    is_new_speaker=False,
                    voice_features=voice_features,
                )

            # 未匹配 → context 累積
            context_id, context_info = self._accumulate_to_context(
                embedding,
                metadata={
                    "method": "fallback",
                    "best_similarity": best_similarity,
                    "timestamp": time.time(),
                },
            )
            return self._build_context_speaker_info(context_id, context_info, 0.4, "fallback")

        except Exception as e:
            logger.error("[Speaker] 回退識別失敗: %s", e)
            SpeakerInfo = _get_speaker_info_class()
            return SpeakerInfo(
                speaker_id="unknown",
                confidence=0.0,
                is_new_speaker=False,
                voice_features=None,
            )

    # ──────────────────────────────────────────────
    #  Embedding 計算
    # ──────────────────────────────────────────────

    def _compute_audio_embedding(self, audio_data: np.ndarray) -> np.ndarray:
        """計算音頻特徵嵌入 — 優先使用 pyannote 嵌入模型"""
        try:
            if self.embedding_model is not None:
                audio_float = audio_data.astype(np.float32) / 32768.0
                audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)  # type: ignore

                min_length = int(0.5 * self.sample_rate)
                if audio_tensor.shape[1] < min_length:
                    padding = min_length - audio_tensor.shape[1]
                    audio_tensor = torch.nn.functional.pad(audio_tensor, (0, padding))  # type: ignore

                device = next(self.embedding_model.parameters()).device
                audio_tensor = audio_tensor.to(device)

                with torch.no_grad():  # type: ignore
                    embedding = self.embedding_model(audio_tensor)

                if isinstance(embedding, torch.Tensor):  # type: ignore
                    embedding_np = embedding.cpu().numpy()
                else:
                    logger.error("[Speaker] 意外的嵌入格式: %s", type(embedding))
                    raise ValueError(f"意外的嵌入格式: {type(embedding)}")

                if len(embedding_np.shape) > 1:
                    if embedding_np.shape[0] == 1:
                        embedding_np = embedding_np[0]
                    else:
                        embedding_np = np.mean(embedding_np, axis=0)

                if np.linalg.norm(embedding_np) == 0:
                    logger.debug("[Speaker] 警告：嵌入為零向量")
                    return self._compute_fallback_embedding(audio_data)

                logger.debug(
                    "[Speaker] pyannote 原始嵌入維度: %s, 範數: %.6f",
                    embedding_np.shape, np.linalg.norm(embedding_np),
                )
                return embedding_np

            else:
                logger.debug("[Speaker] 使用回退特徵提取")
                return self._compute_fallback_embedding(audio_data)

        except Exception as e:
            logger.error("[Speaker] pyannote 嵌入計算失敗: %s", e)
            return self._compute_fallback_embedding(audio_data)

    def _compute_fallback_embedding(self, audio_data: np.ndarray) -> np.ndarray:
        """Fallback 音頻特徵提取方法（確定性版本）

        Bug #3 修復：移除隨機噪聲，確保同一音訊輸入產生相同的嵌入。
        """
        try:
            audio_float = audio_data.astype(np.float32) / 32768.0

            # 1. 基本統計特徵
            mean_amplitude = np.mean(np.abs(audio_float))
            rms_energy = np.sqrt(np.mean(audio_float**2))
            zero_crossing_rate = np.mean(np.abs(np.diff(np.sign(audio_float))))

            # 2. 頻譜特徵
            fft = np.fft.fft(audio_float)
            magnitude = np.abs(fft[:len(fft)//2])

            if np.sum(magnitude) == 0:
                magnitude = np.ones_like(magnitude) * 1e-10

            freqs = np.arange(len(magnitude))
            spectral_centroid = np.sum(freqs * magnitude) / np.sum(magnitude)
            spectral_bandwidth = np.sqrt(np.sum(((freqs - spectral_centroid) ** 2) * magnitude) / np.sum(magnitude))

            # 3. 分段特徵
            n_segments = 8
            segment_length = max(1, len(audio_float) // n_segments)
            segment_features: List[float] = []

            for i in range(n_segments):
                start = i * segment_length
                end = min((i + 1) * segment_length, len(audio_float))
                segment = audio_float[start:end]

                if len(segment) > 0:
                    seg_energy = np.sqrt(np.mean(segment**2))
                    seg_zcr = np.mean(np.abs(np.diff(np.sign(segment))))
                    seg_std = np.std(segment)
                    seg_skew = float(np.mean((segment - np.mean(segment))**3) / (np.std(segment)**3 + 1e-10))
                    segment_features.extend([float(seg_energy), float(seg_zcr), float(seg_std), seg_skew])
                else:
                    segment_features.extend([0.0, 0.0, 0.0, 0.0])

            # 4. 基頻估算
            autocorr = np.correlate(audio_float, audio_float, mode='full')
            autocorr = autocorr[len(autocorr)//2:]

            min_period = int(self.sample_rate / 800)
            max_period = int(self.sample_rate / 50)

            if len(autocorr) > max_period:
                pitch_autocorr = autocorr[min_period:max_period]
                if len(pitch_autocorr) > 0:
                    fundamental_period = np.argmax(pitch_autocorr) + min_period
                    fundamental_freq = self.sample_rate / fundamental_period
                else:
                    fundamental_freq = 150.0
            else:
                fundamental_freq = 150.0

            # 組合所有特徵
            embedding = np.array([
                float(mean_amplitude), float(rms_energy), float(zero_crossing_rate),
                float(spectral_centroid), float(spectral_bandwidth), float(fundamental_freq),
            ] + segment_features)

            # Bug #3 修復：不再添加隨機噪聲，保持確定性嵌入
            # 正規化
            if np.std(embedding) > 0:
                embedding = (embedding - np.mean(embedding)) / np.std(embedding)

            logger.debug("[Speaker] 使用 fallback 嵌入，維度: %s", embedding.shape)
            return embedding

        except Exception as e:
            logger.error("[Speaker] Fallback 嵌入計算失敗: %s", e)
            # 最後的備用方案：返回零向量（非隨機，確定性）
            return np.zeros(38)

    # ──────────────────────────────────────────────
    #  距離計算與 DBSCAN
    # ──────────────────────────────────────────────

    def _calculate_skewness(self, data: np.ndarray) -> float:
        mean = np.mean(data)
        std = np.std(data)
        if std == 0:
            return 0.0
        return float(np.mean(((data - mean) / std) ** 3))

    def _calculate_kurtosis(self, data: np.ndarray) -> float:
        mean = np.mean(data)
        std = np.std(data)
        if std == 0:
            return 0.0
        return float(np.mean(((data - mean) / std) ** 4) - 3)

    def _extract_enhanced_features(self, embedding: np.ndarray) -> Dict[str, float]:
        """提取增強特徵來區分高相似度嵌入"""
        features: Dict[str, float] = {}
        features['magnitude'] = float(np.linalg.norm(embedding))
        features['mean'] = float(np.mean(embedding))
        features['std'] = float(np.std(embedding))
        features['skewness'] = self._calculate_skewness(embedding)
        features['kurtosis'] = self._calculate_kurtosis(embedding)
        features['positive_ratio'] = float(np.sum(embedding > 0) / len(embedding))
        features['max_value'] = float(np.max(embedding))
        features['min_value'] = float(np.min(embedding))

        segments = np.array_split(embedding, 4)
        for i, segment in enumerate(segments):
            features[f'segment_{i}_energy'] = float(np.sum(segment ** 2))

        return features

    def _calculate_multi_distance(self, emb1: np.ndarray, emb2: np.ndarray) -> Dict[str, float]:
        """計算多種距離指標"""
        distances: Dict[str, float] = {}

        if normalize is not None:
            norm_emb1 = normalize([emb1])[0]  # type: ignore
            norm_emb2 = normalize([emb2])[0]  # type: ignore
        else:
            norm_emb1 = emb1 / np.linalg.norm(emb1)
            norm_emb2 = emb2 / np.linalg.norm(emb2)

        if cosine is None or euclidean is None:
            return distances

        distances['cosine'] = float(cosine(norm_emb1, norm_emb2))  # type: ignore
        distances['euclidean'] = float(euclidean(norm_emb1, norm_emb2))  # type: ignore

        if self.use_magnitude_difference:
            distances['magnitude'] = float(abs(np.linalg.norm(emb1) - np.linalg.norm(emb2)))

        if np.std(emb1) > 0 and np.std(emb2) > 0:
            correlation = np.corrcoef(emb1, emb2)[0, 1]
            distances['correlation'] = float(1 - abs(correlation))
        else:
            distances['correlation'] = 1.0

        if self.use_enhanced_features:
            features1 = self._extract_enhanced_features(emb1)
            features2 = self._extract_enhanced_features(emb2)

            feature_diff = 0.0
            for key in features1:
                if key in features2:
                    feature_diff += (features1[key] - features2[key]) ** 2
            distances['enhanced'] = float(np.sqrt(feature_diff))

        return distances

    def _combine_distances(self, distances: Dict[str, float]) -> float:
        """結合多種距離成單一分數"""
        combined = 0.0
        total_weight = 0.0

        for dist_type, weight in self.distance_weights.items():
            if dist_type in distances:
                if dist_type == 'magnitude':
                    normalized_dist = min(distances[dist_type] / 50.0, 1.0)
                elif dist_type == 'euclidean':
                    normalized_dist = min(distances[dist_type] / 100.0, 1.0)
                else:
                    normalized_dist = distances[dist_type]

                combined += weight * normalized_dist
                total_weight += weight

        return combined / total_weight if total_weight > 0 else 1.0

    def _confidence_from_scores(self, cosine_similarity: float, combined_score: float,
                                threshold: float) -> float:
        """將多距離分數與餘弦相似度映射為置信度"""
        if threshold <= 0:
            base = 0.0
        else:
            normalized = max(0.0, 1.0 - min(1.0, combined_score / threshold))
            base = normalized
        conf = (base + max(0.0, min(1.0, cosine_similarity))) / 2.0
        return min(conf, self.confidence_max_cap)

    def _get_qualified_speakers(self) -> Dict[str, Dict]:
        """返回達到最小樣本數要求的說話人資料"""
        qualified: Dict[str, Dict] = {}
        for speaker_id, data in self.speaker_database.items():
            sample_count = data.get('metadata', {}).get('sample_count', len(data.get('embeddings', [])))
            if sample_count >= self.min_samples_for_recognition:
                qualified[speaker_id] = data
        return qualified

    def _dbscan_match(self, embedding: np.ndarray) -> Optional[Tuple[str, float, Dict[str, float]]]:
        """使用 DBSCAN 進行快速聚類匹配"""
        if DBSCAN is None or cosine is None or euclidean is None:
            return None

        try:
            qualified_speakers = self._get_qualified_speakers()
            if not qualified_speakers:
                return None

            embeddings: List[np.ndarray] = []
            labels: List[str] = []
            for spk_id, data in qualified_speakers.items():
                for emb in data.get('embeddings', []):
                    embeddings.append(emb)
                    labels.append(spk_id)

            if len(embeddings) == 0:
                return None

            X = np.vstack(embeddings)
            db = DBSCAN(eps=self.dbscan_eps, min_samples=self.dbscan_min_samples, metric=self.dbscan_metric)  # type: ignore
            clusters = db.fit_predict(X)

            dists = np.array([
                cosine(embedding, e) if self.dbscan_metric == 'cosine' else euclidean(embedding, e)
                for e in embeddings
            ])
            min_idx = int(np.argmin(dists))
            min_cluster = clusters[min_idx]
            if min_cluster == -1:
                return None

            cluster_indices = np.where(clusters == min_cluster)[0]
            best_local_idx = cluster_indices[np.argmin(dists[cluster_indices])]
            matched_speaker = labels[best_local_idx]

            matched_embedding = embeddings[best_local_idx]
            distances = self._calculate_multi_distance(embedding, matched_embedding) if self.use_multi_distance else {'cosine': float(cosine(embedding, matched_embedding))}
            similarity = 1 - distances.get('cosine', 0)

            return matched_speaker, similarity, distances  # type: ignore
        except Exception as e:
            logger.debug("[Speaker] DBSCAN 匹配失敗: %s", e)
            return None

    def _find_best_speaker_match(self, embeddings: List[np.ndarray],
                                 qualified_speakers: Optional[Dict] = None) -> Optional[Tuple[str, float]]:
        """為工作上下文中的樣本找到最佳的說話人匹配"""
        if qualified_speakers is None:
            qualified_speakers = self._get_qualified_speakers()

        if not qualified_speakers or not embeddings:
            return None

        avg_embedding = np.mean(embeddings, axis=0)
        _, best_similarity, best_distance_score, _, matched = \
            self._match_against_database(avg_embedding)

        # 需要重新找出 best_match_id
        if matched:
            # 重跑一次以取得 ID（_match_against_database 已回傳）
            best_match_id, best_similarity, _, _, _ = \
                self._match_against_database(avg_embedding)
            if best_match_id:
                return best_match_id, best_similarity

        return None

    # ──────────────────────────────────────────────
    #  資料庫管理
    # ──────────────────────────────────────────────

    def _load_speaker_database(self):
        try:
            if os.path.exists(self.database_path):
                with open(self.database_path, 'rb') as f:
                    data = pickle.load(f)
                    self.speaker_database = data.get('database', {})
                    self.speaker_counter = data.get('counter', 0)
                logger.info("[Speaker] 載入說話人資料庫: %d 位說話人", len(self.speaker_database))
            else:
                logger.info("[Speaker] 創建新的說話人資料庫")
        except Exception as e:
            logger.error("[Speaker] 載入資料庫失敗: %s", e)
            self.speaker_database = {}
            self.speaker_counter = 0

    def _save_speaker_database(self):
        try:
            os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
            data = {'database': self.speaker_database, 'counter': self.speaker_counter}
            with open(self.database_path, 'wb') as f:
                pickle.dump(data, f)
            logger.debug("[Speaker] 儲存說話人資料庫: %d 位說話人", len(self.speaker_database))
        except Exception as e:
            logger.error("[Speaker] 儲存資料庫失敗: %s", e)

    def get_speaker_info(self, speaker_id: str) -> Optional[Dict]:
        return self.speaker_database.get(speaker_id)

    def list_speakers(self) -> Dict[str, Dict]:
        result: Dict[str, Dict] = {}
        for speaker_id, data in self.speaker_database.items():
            metadata = data.get('metadata', {})
            result[speaker_id] = {
                'embeddings': len(data.get('embeddings', [])),
                'created_at': metadata.get('created_at'),
                'last_seen': metadata.get('last_seen'),
                'sample_count': metadata.get('sample_count', 0),
            }
        return result

    def rename_speaker(self, old_id: str, new_id: str) -> bool:
        try:
            if old_id not in self.speaker_database:
                logger.error("[Speaker] 說話人 '%s' 不存在", old_id)
                return False
            if new_id in self.speaker_database:
                logger.error("[Speaker] 說話人 '%s' 已存在", new_id)
                return False
            self.speaker_database[new_id] = self.speaker_database.pop(old_id)
            self._save_speaker_database()
            logger.info("[Speaker] 說話人 '%s' 已重新命名為 '%s'", old_id, new_id)
            return True
        except Exception as e:
            logger.error("[Speaker] 重新命名失敗: %s", e)
            return False

    def delete_speaker(self, speaker_id: str) -> bool:
        try:
            if speaker_id not in self.speaker_database:
                logger.error("[Speaker] 說話人 '%s' 不存在", speaker_id)
                return False
            del self.speaker_database[speaker_id]
            self._save_speaker_database()
            logger.info("[Speaker] 說話人 '%s' 已刪除", speaker_id)
            return True
        except Exception as e:
            logger.error("[Speaker] 刪除失敗: %s", e)
            return False

    def clear_all_speakers(self) -> bool:
        try:
            self.speaker_database.clear()
            self.speaker_counter = 0
            self._save_speaker_database()
            logger.info("[Speaker] 所有說話人數據已清空")
            return True
        except Exception as e:
            logger.error("[Speaker] 清空失敗: %s", e)
            return False

    def backup_speakers(self, backup_path: str) -> bool:
        try:
            import shutil
            if os.path.exists(self.database_path):
                shutil.copy2(self.database_path, backup_path)
                logger.info("[Speaker] 說話人數據已備份至: %s", backup_path)
                return True
            else:
                logger.error("[Speaker] 原始數據庫不存在，無法備份")
                return False
        except Exception as e:
            logger.error("[Speaker] 備份失敗: %s", e)
            return False

    def restore_speakers(self, backup_path: str) -> bool:
        try:
            import shutil
            if not os.path.exists(backup_path):
                logger.error("[Speaker] 備份檔案不存在: %s", backup_path)
                return False
            if os.path.exists(self.database_path):
                current_backup = f"{self.database_path}.pre_restore"
                shutil.copy2(self.database_path, current_backup)
                logger.info("[Speaker] 當前數據已備份至: %s", current_backup)
            shutil.copy2(backup_path, self.database_path)
            self._load_speaker_database()
            logger.info("[Speaker] 說話人數據已從備份恢復: %s", backup_path)
            return True
        except Exception as e:
            logger.error("[Speaker] 恢復失敗: %s", e)
            return False

    def get_database_info(self) -> Dict:
        try:
            total_speakers = len(self.speaker_database)
            total_samples = sum(len(data.get('embeddings', [])) for data in self.speaker_database.values())
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
                'multi_distance': {'enabled': self.use_multi_distance, 'weights': self.distance_weights},
            }
        except Exception as e:
            logger.error("[Speaker] 獲取資料庫信息失敗: %s", e)
            return {}

    def get_database_status(self) -> Dict:
        qualified_speakers = self._get_qualified_speakers()
        total_samples = sum(len(data.get('embeddings', [])) for data in self.speaker_database.values())
        qualified_samples = sum(len(data.get('embeddings', [])) for data in qualified_speakers.values())
        return {
            'total_speakers': len(self.speaker_database),
            'qualified_speakers': len(qualified_speakers),
            'min_samples_threshold': self.min_samples_for_recognition,
            'total_samples': total_samples,
            'qualified_samples': qualified_samples,
            'unqualified_speakers': len(self.speaker_database) - len(qualified_speakers),
        }

    def update_similarity_threshold(self, threshold: float):
        clamped = max(0.0, min(1.0, threshold))
        if clamped != threshold:
            logger.info("[Speaker] 輸入的相似度閾值 %s 超出範圍，已調整為 %s", threshold, clamped)
        self.similarity_threshold = clamped
        logger.info("[Speaker] 更新相似度閾值: %s", self.similarity_threshold)

    def update_distance_weights(self, weights: Dict[str, float]):
        for key, value in weights.items():
            if key in self.distance_weights:
                self.distance_weights[key] = value
        logger.info("[Speaker] 更新距離權重: %s", self.distance_weights)

    def get_working_context_status(self) -> Dict[str, Any]:
        if self.context_manager is None:
            return {'active_speaker_contexts': 0, 'contexts': [], 'context_threshold': self.context_sample_threshold}
        context_info = self.context_manager.get_all_contexts_info()
        speaker_contexts = [
            ctx for ctx in context_info
            if ctx.get('type') == ContextType.SPEAKER_ACCUMULATION.value
        ]
        return {
            'active_speaker_contexts': len(speaker_contexts),
            'contexts': speaker_contexts,
            'context_threshold': self.context_sample_threshold,
        }

    def reset_speaker_database(self, backup: bool = True):
        if backup and self.speaker_database:
            backup_path = f"{self.database_path}.backup_{int(time.time())}"
            try:
                with open(backup_path, 'wb') as f:
                    pickle.dump({'database': self.speaker_database, 'counter': self.speaker_counter}, f)
                logger.info("[Speaker] 資料庫已備份至: %s", backup_path)
            except Exception as e:
                logger.error("[Speaker] 備份失敗: %s", e)
        self.speaker_database = {}
        self.speaker_counter = 0
        self._save_speaker_database()
        logger.info("[Speaker] 說話人資料庫已重建")

    def shutdown(self):
        self._save_speaker_database()
        logger.info("[Speaker] 說話人識別模組已關閉")
