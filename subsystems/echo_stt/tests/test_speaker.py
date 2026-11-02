"""
T-12: Speaker Identification 測試

包含：
- Bug #1 修復驗證：context_info UnboundLocalError
- Bug #3 修復驗證：_compute_fallback_embedding 確定性
"""

import numpy as np
import pytest
from echo_stt.speaker.identification import SpeakerIdentification


@pytest.fixture
def speaker():
    """不帶 context_manager 的 SpeakerIdentification 實例"""
    return SpeakerIdentification(context_manager=None)


class TestFallbackEmbeddingDeterministic:
    """Bug #3 修復驗證：同一音訊的 fallback embedding 應該是確定性的"""

    def test_deterministic_embedding(self, speaker):
        """同一 audio_data 連呼兩次，結果應完全一致"""
        audio = np.random.RandomState(42).randint(-10000, 10000, 16000, dtype=np.int16)

        emb1 = speaker._compute_fallback_embedding(audio)
        emb2 = speaker._compute_fallback_embedding(audio)

        assert np.array_equal(emb1, emb2), (
            "Bug #3: _compute_fallback_embedding 不是確定性的！"
            f"\n差異: {np.max(np.abs(emb1 - emb2))}"
        )

    def test_embedding_shape(self, speaker):
        """Fallback embedding 應有固定維度"""
        audio = np.random.RandomState(42).randint(-10000, 10000, 16000, dtype=np.int16)
        emb = speaker._compute_fallback_embedding(audio)
        assert emb.ndim == 1
        assert len(emb) > 0

    def test_zero_audio_embedding(self, speaker):
        """全零音訊不應 crash"""
        audio = np.zeros(16000, dtype=np.int16)
        emb = speaker._compute_fallback_embedding(audio)
        assert emb.ndim == 1


class TestContextManagerNone:
    """Bug #1 修復驗證：context_manager=None 時不應出現 UnboundLocalError"""

    def test_fallback_no_context(self, speaker):
        """fallback 路徑在無 context_manager 時不 crash"""
        audio = np.random.RandomState(42).randint(-10000, 10000, 16000, dtype=np.int16)
        result = speaker._fallback_speaker_identification(audio)
        # 應回傳 SpeakerInfo（可能是 context_pending 或 unknown）
        assert hasattr(result, 'speaker_id')
        assert hasattr(result, 'confidence')

    def test_direct_embedding_no_context(self, speaker):
        """direct embedding 路徑在無 context_manager 時不 crash"""
        audio = np.random.RandomState(42).randint(-10000, 10000, 16000, dtype=np.int16)
        result = speaker._direct_embedding_identification(audio)
        assert hasattr(result, 'speaker_id')


class TestSpeakerDatabase:
    """基本資料庫操作"""

    def test_empty_database(self, speaker):
        """初始資料庫應為空"""
        assert len(speaker.speaker_database) == 0

    def test_list_speakers_empty(self, speaker):
        result = speaker.list_speakers()
        assert isinstance(result, dict)
        assert len(result) == 0

    def test_get_database_info(self, speaker):
        info = speaker.get_database_info()
        assert info['total_speakers'] == 0
        assert info['total_samples'] == 0

    def test_get_database_status(self, speaker):
        status = speaker.get_database_status()
        assert status['total_speakers'] == 0
        assert status['qualified_speakers'] == 0

    def test_get_working_context_status_no_manager(self, speaker):
        """無 context_manager 時應回傳空狀態"""
        status = speaker.get_working_context_status()
        assert status['active_speaker_contexts'] == 0

    def test_qualified_speakers_empty(self, speaker):
        """空資料庫應無合格說話人"""
        qualified = speaker._get_qualified_speakers()
        assert len(qualified) == 0
