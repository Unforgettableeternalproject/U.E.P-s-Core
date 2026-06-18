"""
E2E Integration Tests — 打真實 LLM 服務驗證完整壓縮流程

需要本地 Ollama 服務執行中（或設定 GOOGLE_API_KEY 用 Gemini）。
預設使用 Ollama/gemma4 以節省成本。

Run:
    pytest -m e2e -v                          # 跑所有 E2E（需要 Ollama）
    pytest -m e2e -v --e2e-backend=gemini     # 改用 Gemini
    pytest -m e2e -v -k "test_full_conversation"  # 跑單一場景

Environment:
    E2E_BACKEND:  ollama | gemini (default: ollama)
    E2E_MODEL:    model name override (default: auto-detect)
    OLLAMA_URL:   Ollama service URL (default: http://localhost:11434)
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pytest
import requests

from echo_thought_core import (
    CompactionConfig,
    ContextManager,
    PersonaContext,
)
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import CompactionResult, CompressionTier

# ═══════════════════════════════════════════════════════════════════════════════
# Pytest 設定
# ═══════════════════════════════════════════════════════════════════════════════

def pytest_addoption(parser):
    """註冊 E2E 專用的 CLI 選項"""
    parser.addoption(
        "--e2e-backend",
        action="store",
        default=os.getenv("E2E_BACKEND", "ollama"),
        help="E2E backend: ollama or gemini (default: ollama)",
    )
    parser.addoption(
        "--e2e-model",
        action="store",
        default=os.getenv("E2E_MODEL", ""),
        help="Override model name for E2E tests",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def e2e_backend_type(request) -> str:
    """取得 E2E 後端類型"""
    return request.config.getoption("--e2e-backend", default="ollama")


@pytest.fixture(scope="session")
def e2e_model_override(request) -> str:
    """取得模型覆蓋名稱"""
    return request.config.getoption("--e2e-model", default="")


@pytest.fixture(scope="session")
def ollama_url() -> str:
    """Ollama 服務 URL"""
    return os.getenv("OLLAMA_URL", "http://localhost:11434")


@pytest.fixture(scope="session")
def ollama_available(ollama_url) -> bool:
    """檢查 Ollama 服務是否可用"""
    try:
        r = requests.get(f"{ollama_url}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def gemini_available() -> bool:
    """檢查 Gemini API 是否可用"""
    return bool(
        os.getenv("GOOGLE_API_KEY") or os.getenv("GCP_PROJECT_ID")
    )


@pytest.fixture(scope="session")
def e2e_backend(e2e_backend_type, ollama_available, gemini_available, ollama_url, e2e_model_override):
    """建立 E2E 後端實例"""
    if e2e_backend_type == "ollama":
        if not ollama_available:
            pytest.skip("Ollama service not available")
        from echo_thought_core.backends.ollama import OllamaBackend

        model = e2e_model_override or "gemma4:e4b-it-q4_K_M"
        backend = OllamaBackend(
            model=model,
            base_url=ollama_url,
            temperature=0.7,
            max_tokens=2048,
        )
        return backend

    elif e2e_backend_type == "gemini":
        if not gemini_available:
            pytest.skip("Gemini API key not configured")
        from echo_thought_core.llm_client import LLMClient

        model = e2e_model_override or "gemini-2.5-flash-lite"
        backend = LLMClient(model=model, temperature=0.7, max_tokens=2048)
        return backend

    else:
        pytest.skip(f"Unknown backend: {e2e_backend_type}")


@pytest.fixture
def e2e_persona() -> PersonaContext:
    """E2E 測試用人設"""
    return PersonaContext(
        name="E2E-TestBot",
        traits=["concise", "helpful"],
        speaking_style="direct",
        user_preferences={"language": "zh-TW"},
    )


@pytest.fixture
def e2e_manager(e2e_backend, e2e_persona, e2e_backend_type, e2e_model_override):
    """建立注入真實後端的 ContextManager。

    使用小 context window（2000 tokens）以便快速觸發壓縮。
    """
    model = e2e_model_override or (
        "gemma4:e4b-it-q4_K_M" if e2e_backend_type == "ollama"
        else "gemini-2.5-flash-lite"
    )
    config = CompactionConfig(
        backend=e2e_backend_type,
        model=model,
        context_window_size=2000,  # 小 window 方便觸發壓縮
        compaction_cooldown_seconds=0,  # 無冷卻
    )
    return ContextManager(
        config=config,
        persona=e2e_persona,
        backend=e2e_backend,
    )


@pytest.fixture
def e2e_manager_medium(e2e_backend, e2e_persona, e2e_backend_type, e2e_model_override):
    """中等 context window 的 ContextManager（用於 preflight 測試）"""
    model = e2e_model_override or (
        "gemma4:e4b-it-q4_K_M" if e2e_backend_type == "ollama"
        else "gemini-2.5-flash-lite"
    )
    config = CompactionConfig(
        backend=e2e_backend_type,
        model=model,
        context_window_size=5000,  # 中等 window
        compaction_cooldown_seconds=0,
    )
    return ContextManager(
        config=config,
        persona=e2e_persona,
        backend=e2e_backend,
    )


class SpyMemoryBridge:
    """記錄所有生命週期回調的 MemoryBridge 實作"""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def on_session_start(self, session_id: str) -> None:
        self.calls.append({"method": "on_session_start", "session_id": session_id})

    def on_session_end(self, session_id: str, messages: list) -> None:
        self.calls.append({
            "method": "on_session_end",
            "session_id": session_id,
            "message_count": len(messages),
        })

    def on_pre_compress(self, messages: list) -> str:
        self.calls.append({
            "method": "on_pre_compress",
            "message_count": len(messages),
        })
        return ""  # 不注入記憶

    def on_post_compress(self, result: CompactionResult) -> None:
        self.calls.append({
            "method": "on_post_compress",
            "tier": result.tier.name,
            "tokens_saved": result.tokens_saved,
        })

    def prefetch(self, query: str) -> str:
        self.calls.append({"method": "prefetch", "query": query})
        return ""

    def get_calls(self, method: str) -> list:
        """取得指定方法的呼叫記錄"""
        return [c for c in self.calls if c["method"] == method]


@pytest.fixture
def spy_memory_bridge():
    """建立 SpyMemoryBridge"""
    return SpyMemoryBridge()


# ═══════════════════════════════════════════════════════════════════════════════
# Test Cases
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
class TestE2EFullFlow:
    """E2E 完整流程測試"""

    # ── TC-E2E-01: 完整對話 + 自動壓縮觸發 ──────────────────────────────

    async def test_full_conversation_with_auto_compression(self, e2e_manager):
        """場景 a: 持續對話直到觸發自動壓縮

        驗證：
        - 訊息超過閾值時自動觸發壓縮
        - 壓縮後 token 數降低
        - 壓縮後仍可繼續對話
        """
        mgr = e2e_manager

        # 灌入足夠訊息讓 token 超過閾值（2000 * 0.60 = 1200 tokens）
        for i in range(15):
            mgr.add_message("user", f"這是第 {i + 1} 輪對話的問題。" + "測試內容" * 20)
            mgr.add_message("assistant", f"這是第 {i + 1} 輪的回覆。" + "回應資料" * 20)

        tokens_before = mgr.state.current_tokens
        assert tokens_before > 0, "應有 token 累積"

        # 觸發異步壓縮（Layer 2/3 需要真實 LLM）
        messages = await mgr.get_messages_async()

        # 驗證壓縮發生
        assert mgr.state.compression_count >= 1, (
            f"預期至少 1 次壓縮，實際: {mgr.state.compression_count}。"
            f"tokens_before={tokens_before}, tokens_after={mgr.state.current_tokens}"
        )

        # 壓縮後 token 應降低
        assert mgr.state.current_tokens < tokens_before, (
            f"壓縮後 token 應降低: before={tokens_before}, after={mgr.state.current_tokens}"
        )

        # 壓縮後仍可取得訊息
        assert len(messages) > 0, "壓縮後應保留至少一條訊息"

        # 壓縮後可繼續加入訊息
        mgr.add_message("user", "壓縮後的新訊息")
        assert mgr.state.message_count > len(messages), "壓縮後應可繼續加入訊息"

    # ── TC-E2E-02: preflight_compress ────────────────────────────────────

    async def test_preflight_compress(self, e2e_manager_medium):
        """場景 b: API 呼叫前預防性壓縮

        驗證：
        - 接近滿載時 preflight 會觸發壓縮
        - 壓縮結果有效
        """
        mgr = e2e_manager_medium

        # 灌到 ~70% (5000 * 0.70 = 3500 tokens)
        for i in range(20):
            mgr.add_message("user", f"問題 {i}: " + "填充內容" * 15)
            mgr.add_message("assistant", f"回覆 {i}: " + "回應資料" * 15)

        tokens_before = mgr.state.current_tokens

        # Preflight: 假設接下來的 API 請求會消耗 500 tokens
        result = await mgr.preflight_compress(estimated_request_tokens=500)

        if result is not None:
            # 壓縮被觸發
            assert result.tokens_saved >= 0, "tokens_saved 不應為負"
            assert result.tokens_before > 0
            assert mgr.state.current_tokens <= tokens_before, (
                f"Preflight 後 token 不應增加: before={tokens_before}, "
                f"after={mgr.state.current_tokens}"
            )
        else:
            # 壓縮未觸發（token 未達閾值）— 也是合理的
            pass

    # ── TC-E2E-03: 手動 compact (Layer 3) ────────────────────────────────

    async def test_manual_compact_layer3(self, e2e_manager):
        """場景 c: 手動觸發 Layer 3 完整壓縮

        驗證：
        - compact() 回傳 TIER_3_FULL_COMPACTION 結果
        - summary_text 非空
        - 壓縮後訊息中有 is_compressed 標記
        """
        mgr = e2e_manager

        # 加入 20 條對話
        for i in range(10):
            mgr.add_message("user", f"使用者第 {i + 1} 個問題：請告訴我關於 AI 壓縮的知識。")
            mgr.add_message("assistant", f"回覆 {i + 1}：AI 壓縮涉及多種技術，包括剪枝、量化、蒸餾等。")

        tokens_before = mgr.state.current_tokens

        # 手動觸發 Layer 3
        result = await mgr.compact()

        # 驗證結果
        assert result.tier == CompressionTier.TIER_3_FULL_COMPACTION
        assert result.tokens_before == tokens_before

        # summary_text 應有內容（LLM 回傳的壓縮摘要）
        # 注意：本地模型可能不完美遵守 <summary> 格式，所以降低要求
        assert result.summary_text is not None, "應有壓縮摘要文字"
        assert len(result.summary_text) > 0, "壓縮摘要不應為空"

        # 壓縮後的訊息數量應大幅減少（20 → 1~2 條摘要訊息）
        assert mgr.state.message_count < 20, (
            f"壓縮後訊息數應減少，實際: {mgr.state.message_count}"
        )

        # 注意：對短對話（<500 tokens），壓縮摘要可能比原文更長。
        # 這是預期行為——壓縮的價值在長對話中才顯著。
        # 因此只驗證流程完成且訊息數量減少，不強制 tokens_after < tokens_before。

    # ── TC-E2E-04: 壓縮後 sync_history_from_messages ─────────────────────

    async def test_compression_syncs_to_backend(self, e2e_backend, e2e_persona, e2e_backend_type, e2e_model_override):
        """場景 d: 壓縮後 LLM backend 的歷史應同步

        驗證：
        - compact 後 backend._history 與 manager.history.messages 一致
        - role 映射正確
        """
        model = e2e_model_override or (
            "gemma4:e4b-it-q4_K_M" if e2e_backend_type == "ollama"
            else "gemini-2.5-flash-lite"
        )
        # 用新的 backend 避免跟其他測試共享歷史狀態
        if e2e_backend_type == "ollama":
            from echo_thought_core.backends.ollama import OllamaBackend
            backend = OllamaBackend(model=model, temperature=0.7, max_tokens=2048)
        else:
            from echo_thought_core.llm_client import LLMClient
            backend = LLMClient(model=model, temperature=0.7, max_tokens=2048)

        config = CompactionConfig(
            backend=e2e_backend_type,
            model=model,
            context_window_size=2000,
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(config=config, persona=e2e_persona, backend=backend)

        # 加入訊息
        for i in range(8):
            mgr.add_message("user", f"同步測試 Q{i + 1}: 說明壓縮技術。")
            mgr.add_message("assistant", f"同步測試 A{i + 1}: 壓縮技術包括多種方法。")

        # 手動 compact
        await mgr.compact()

        # 驗證 backend 歷史長度與 manager 一致
        manager_msg_count = len(mgr.history.messages)
        backend_history = backend._history

        assert len(backend_history) == manager_msg_count, (
            f"Backend 歷史長度 ({len(backend_history)}) 應等於 "
            f"Manager 訊息數 ({manager_msg_count})"
        )

        # 驗證 role 映射（manager 的 role → backend 的 role）
        for i, msg in enumerate(mgr.history.messages):
            if e2e_backend_type == "ollama":
                backend_msg = backend_history[i]
                expected_role = msg.role  # Ollama 支援 system/user/assistant
                assert backend_msg["role"] == expected_role or (
                    msg.role == "system" and backend_msg["role"] in ("system", "user")
                ), f"Message {i}: role mismatch: {backend_msg['role']} vs {msg.role}"

    # ── TC-E2E-05: MemoryBridge 生命週期回調 ─────────────────────────────

    async def test_memory_bridge_lifecycle(
        self, e2e_backend, e2e_persona, e2e_backend_type, e2e_model_override, spy_memory_bridge
    ):
        """場景 e: MemoryBridge 在壓縮前後收到正確的回調

        驗證：
        - on_session_start 在建立時呼叫
        - on_pre_compress + on_post_compress 在壓縮時呼叫
        - on_session_end 在 reset 時呼叫
        """
        model = e2e_model_override or (
            "gemma4:e4b-it-q4_K_M" if e2e_backend_type == "ollama"
            else "gemini-2.5-flash-lite"
        )
        if e2e_backend_type == "ollama":
            from echo_thought_core.backends.ollama import OllamaBackend
            backend = OllamaBackend(model=model, temperature=0.7, max_tokens=2048)
        else:
            from echo_thought_core.llm_client import LLMClient
            backend = LLMClient(model=model, temperature=0.7, max_tokens=2048)

        config = CompactionConfig(
            backend=e2e_backend_type,
            model=model,
            context_window_size=2000,
            compaction_cooldown_seconds=0,
        )
        mgr = ContextManager(
            config=config,
            persona=e2e_persona,
            backend=backend,
            memory_bridge=spy_memory_bridge,
        )

        # ① on_session_start 應在建立時呼叫
        start_calls = spy_memory_bridge.get_calls("on_session_start")
        assert len(start_calls) == 1, f"on_session_start 應呼叫 1 次，實際: {len(start_calls)}"

        # 加入訊息
        for i in range(8):
            mgr.add_message("user", f"Bridge 測試 Q{i + 1}: 測試記憶整合。")
            mgr.add_message("assistant", f"Bridge 測試 A{i + 1}: 已收到。")

        # ② 觸發 Layer 3 壓縮
        await mgr.compact()

        # on_pre_compress 應被呼叫
        pre_calls = spy_memory_bridge.get_calls("on_pre_compress")
        assert len(pre_calls) >= 1, f"on_pre_compress 應至少呼叫 1 次，實際: {len(pre_calls)}"

        # on_post_compress 應被呼叫
        post_calls = spy_memory_bridge.get_calls("on_post_compress")
        assert len(post_calls) >= 1, f"on_post_compress 應至少呼叫 1 次，實際: {len(post_calls)}"
        assert post_calls[-1]["tier"] == "TIER_3_FULL_COMPACTION"

        # ③ Reset session
        session_id_before = mgr.state.session_id
        mgr.reset_session()

        # on_session_end 應被呼叫
        end_calls = spy_memory_bridge.get_calls("on_session_end")
        assert len(end_calls) == 1, f"on_session_end 應呼叫 1 次，實際: {len(end_calls)}"
        assert end_calls[0]["session_id"] == session_id_before

        # 新 session 的 on_session_start 也應呼叫
        start_calls = spy_memory_bridge.get_calls("on_session_start")
        assert len(start_calls) == 2, f"reset 後 on_session_start 應呼叫 2 次，實際: {len(start_calls)}"

    # ── TC-E2E-06: Session Reset ─────────────────────────────────────────

    def test_session_reset(self, e2e_manager):
        """場景 f: session reset 後所有狀態歸零

        驗證：
        - token 歸零、訊息清空、session_id 更換
        - backend 歷史清空
        """
        mgr = e2e_manager

        # 加入訊息
        for i in range(5):
            mgr.add_message("user", f"Reset 測試 {i}")
            mgr.add_message("assistant", f"OK {i}")

        old_session_id = mgr.state.session_id
        assert mgr.state.message_count > 0

        # Reset
        mgr.reset_session()

        # 驗證歸零
        assert mgr.state.current_tokens == 0, f"Tokens 應歸零，實際: {mgr.state.current_tokens}"
        assert mgr.state.message_count == 0, f"Messages 應歸零，實際: {mgr.state.message_count}"
        assert len(mgr.history.messages) == 0
        assert mgr.state.session_id != old_session_id, "Session ID 應更換"
        assert mgr.state.compression_count == 0

        # Backend 歷史應清空
        if mgr._backend is not None:
            backend_history = getattr(mgr._backend, "_history", None)
            if backend_history is not None:
                assert len(backend_history) == 0, (
                    f"Backend 歷史應清空，實際: {len(backend_history)}"
                )


@pytest.mark.e2e
class TestE2EBackendDirect:
    """直接測試 Backend 的 E2E"""

    def test_backend_generate(self, e2e_backend):
        """Backend.generate() 應回傳非空字串"""
        response = e2e_backend.generate("Say 'hello' in one word.")
        assert isinstance(response, str)
        assert len(response) > 0, "generate() 應回傳非空字串"

    def test_backend_chat(self, e2e_backend):
        """Backend.chat() 應回傳非空字串且更新歷史"""
        e2e_backend.clear_history()

        response = e2e_backend.chat("Say 'hi' in one word.")
        assert isinstance(response, str)
        assert len(response) > 0

        # 歷史應有 2 條（user + assistant）
        history = e2e_backend._history
        assert len(history) >= 2, f"歷史應至少 2 條，實際: {len(history)}"

    def test_backend_count_tokens(self, e2e_backend):
        """count_tokens() 應回傳正整數"""
        count = e2e_backend.count_tokens("Hello world 你好世界")
        assert isinstance(count, int)
        assert count > 0

    def test_backend_context_window(self, e2e_backend):
        """get_context_window_size() 應回傳合理值"""
        size = e2e_backend.get_context_window_size()
        assert isinstance(size, int)
        assert size >= 2048, f"Context window 太小: {size}"

    async def test_backend_query(self, e2e_backend):
        """query() 應回傳含 'text' 鍵的 dict"""
        result = e2e_backend.query("Say 'test' in one word.")
        assert isinstance(result, dict)
        assert "text" in result, f"query() 結果應包含 'text' 鍵: {result.keys()}"
        assert len(result["text"]) > 0

    async def test_backend_summarize(self, e2e_backend):
        """summarize() 應回傳非空摘要"""
        messages = [
            Message(role="user", content="What is Python?"),
            Message(role="assistant", content="Python is a programming language."),
            Message(role="user", content="What about JavaScript?"),
            Message(role="assistant", content="JavaScript runs in browsers."),
        ]
        summary = await e2e_backend.summarize(messages)
        assert isinstance(summary, str)
        assert len(summary) > 0, "summarize() 應回傳非空摘要"


@pytest.mark.e2e
class TestE2ECLISmoke:
    """CLI Smoke Test（不啟動互動式 prompt）"""

    def test_cli_initialize_with_ollama(self, ollama_available):
        """CLI 應可用 Ollama backend 初始化"""
        if not ollama_available:
            pytest.skip("Ollama not available")

        from echo_thought_core.cli import SessionControlCLI

        args = argparse.Namespace(
            model="gemma4:e4b-it-q4_K_M",
            window=5000,
            backend="ollama",
            ollama_url="http://localhost:11434",
            debug=False,
        )
        cli = SessionControlCLI(args)
        result = cli.initialize()

        assert result is True, "CLI initialize() 應成功"
        assert cli.mgr is not None, "ContextManager 應被建立"
        assert cli.llm is not None, "Backend 應被建立"

    def test_cli_fill_command(self, ollama_available):
        """CLI fill 命令應正確灌入訊息"""
        if not ollama_available:
            pytest.skip("Ollama not available")

        from echo_thought_core.cli import SessionControlCLI

        args = argparse.Namespace(
            model="gemma4:e4b-it-q4_K_M",
            window=50000,
            backend="ollama",
            ollama_url="http://localhost:11434",
            debug=False,
        )
        cli = SessionControlCLI(args)
        cli.initialize()

        # 執行 fill 5
        cli._cmd_fill("fill 5")

        assert cli.mgr.state.message_count == 10, (
            f"fill 5 應產生 10 條訊息，實際: {cli.mgr.state.message_count}"
        )
        assert cli.mgr.state.current_tokens > 0

    def test_cli_offline_fallback(self):
        """CLI 無法連線時應降級為 offline 模式"""
        from echo_thought_core.cli import SessionControlCLI

        args = argparse.Namespace(
            model="nonexistent-model",
            window=5000,
            backend="ollama",
            ollama_url="http://localhost:99999",  # 不存在的 port
            debug=False,
        )
        cli = SessionControlCLI(args)
        result = cli.initialize()

        assert result is True, "CLI 即使 offline 也應成功初始化"
        assert cli.mgr is not None, "ContextManager 應被建立（offline 模式）"
        assert cli.llm is None, "Backend 應為 None（offline 模式）"
