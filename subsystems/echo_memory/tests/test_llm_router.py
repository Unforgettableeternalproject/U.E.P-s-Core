"""
tests/test_llm_router.py — LLMRouter 單元測試

不呼叫真實 API；透過 mock 驗證路由、配置、降級等邏輯。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from llm_integration.router import LLMRole, LLMRouter, RoleConfig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """建立一個最小的 config.yaml（無 router 區塊）"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: gemini-2.5-flash-lite\n", encoding="utf-8")
    return cfg


@pytest.fixture
def full_config(tmp_path: Path) -> Path:
    """建立一個包含 router 區塊的 config.yaml"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        model: gemini-2.5-flash-lite

        router:
          roles:
            chat:
              model: gemini-3.1-flash-lite
              temperature: 0.9
              max_tokens: 8192
              description: 測試用 CHAT
            work:
              model: gemini-3.1-flash-lite
              temperature: 0.2
              max_tokens: 2048
            eval:
              model: gemini-2.5-flash-lite
              temperature: 0.0
              max_tokens: 512
    """), encoding="utf-8")
    return cfg


@pytest.fixture
def router_default() -> LLMRouter:
    """使用預設配置的 Router（跳過 config.yaml 載入）"""
    # 給一個不存在的路徑，讓它回退到預設值
    return LLMRouter(config_path="/nonexistent/config.yaml")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置載入
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConfigLoading:
    """配置載入測試"""

    def test_default_configs_all_roles(self, router_default: LLMRouter):
        """預設配置應包含所有三個角色"""
        assert set(router_default.available_roles) == {
            LLMRole.CHAT, LLMRole.WORK, LLMRole.EVAL
        }

    def test_default_chat_config(self, router_default: LLMRouter):
        """CHAT 預設配置（gemini-3.1-flash-lite）"""
        cfg = router_default.get_role_config(LLMRole.CHAT)
        assert cfg.model == "gemini-3.1-flash-lite"
        assert cfg.temperature == 0.8
        assert cfg.max_tokens == 4096

    def test_default_work_config(self, router_default: LLMRouter):
        """WORK 預設配置（gemini-3.1-flash-lite, 低溫度）"""
        cfg = router_default.get_role_config(LLMRole.WORK)
        assert cfg.model == "gemini-3.1-flash-lite"
        assert cfg.temperature == 0.3
        assert cfg.max_tokens == 1024

    def test_default_eval_config(self, router_default: LLMRouter):
        """EVAL 預設配置（gemini-2.5-flash-lite, 零溫度）"""
        cfg = router_default.get_role_config(LLMRole.EVAL)
        assert cfg.model == "gemini-2.5-flash-lite"
        assert cfg.temperature == 0.0
        assert cfg.max_tokens == 768

    def test_config_override_from_yaml(self, full_config: Path):
        """config.yaml 的 router 區塊應覆蓋預設值"""
        router = LLMRouter(config_path=full_config)

        chat = router.get_role_config(LLMRole.CHAT)
        assert chat.model == "gemini-3.1-flash-lite"
        assert chat.temperature == 0.9
        assert chat.max_tokens == 8192
        assert chat.description == "測試用 CHAT"

        work = router.get_role_config(LLMRole.WORK)
        assert work.model == "gemini-3.1-flash-lite"
        assert work.temperature == 0.2
        assert work.max_tokens == 2048

        eval_ = router.get_role_config(LLMRole.EVAL)
        assert eval_.model == "gemini-2.5-flash-lite"
        assert eval_.temperature == 0.0
        assert eval_.max_tokens == 512

    def test_minimal_config_uses_defaults(self, minimal_config: Path):
        """config.yaml 沒有 router 區塊時，使用預設值"""
        router = LLMRouter(config_path=minimal_config)
        cfg = router.get_role_config(LLMRole.WORK)
        assert cfg.model == "gemini-3.1-flash-lite"
        assert cfg.temperature == 0.3

    def test_nonexistent_config_uses_defaults(self):
        """config.yaml 不存在時，使用預設值"""
        router = LLMRouter(config_path="/this/does/not/exist.yaml")
        assert len(router.available_roles) == 3

    def test_unknown_role_in_config_ignored(self, tmp_path: Path):
        """config.yaml 中的未知角色名稱應被跳過"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            router:
              roles:
                unknown_role:
                  model: some-model
                work:
                  temperature: 0.1
        """), encoding="utf-8")
        router = LLMRouter(config_path=cfg)
        # work 被正確更新
        assert router.get_role_config(LLMRole.WORK).temperature == 0.1
        # 未知角色沒有被加入
        assert len(router.available_roles) == 3

    def test_partial_override(self, tmp_path: Path):
        """只覆蓋部分欄位，其餘保留預設值"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent("""\
            router:
              roles:
                eval:
                  max_tokens: 1024
        """), encoding="utf-8")
        router = LLMRouter(config_path=cfg)
        eval_cfg = router.get_role_config(LLMRole.EVAL)
        assert eval_cfg.max_tokens == 1024          # 被覆蓋
        assert eval_cfg.temperature == 0.0           # 保留預設
        assert eval_cfg.model == "gemini-2.5-flash-lite"  # 保留預設


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# get_role_config 防禦
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRoleConfigAccess:
    """角色配置存取"""

    def test_get_role_config_returns_copy(self, router_default: LLMRouter):
        """get_role_config 回傳副本，修改不影響原始"""
        cfg1 = router_default.get_role_config(LLMRole.WORK)
        cfg1.temperature = 999.0
        cfg2 = router_default.get_role_config(LLMRole.WORK)
        assert cfg2.temperature == 0.3  # 原始值未被修改


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# generate / get_llm_fn（mock API）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGenerate:
    """生成呼叫測試（mock genai）"""

    def _make_mock_result(self, text: str):
        """建立模擬 API 回應"""
        result = MagicMock()
        result.text = text
        return result

    def test_generate_routes_to_correct_model(self, router_default: LLMRouter):
        """generate 應使用角色對應的模型"""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._make_mock_result("ok")

        router_default._client = mock_client
        router_default._initialized = True

        router_default.generate("hello", role=LLMRole.EVAL)

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-2.5-flash-lite"

    def test_generate_uses_role_temperature(self, router_default: LLMRouter):
        """generate 應使用角色預設溫度"""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._make_mock_result("ok")

        router_default._client = mock_client
        router_default._initialized = True

        # 使用 EVAL（temperature=0.0）
        router_default.generate("test", role=LLMRole.EVAL)

        # 檢查傳入的 config 參數
        call_kwargs = mock_client.models.generate_content.call_args
        config_obj = call_kwargs.kwargs["config"]
        assert config_obj.temperature == 0.0

    def test_generate_temperature_override(self, router_default: LLMRouter):
        """明確指定溫度時應覆蓋角色預設"""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._make_mock_result("ok")

        router_default._client = mock_client
        router_default._initialized = True

        router_default.generate("test", role=LLMRole.EVAL, temperature=0.5)

        call_kwargs = mock_client.models.generate_content.call_args
        config_obj = call_kwargs.kwargs["config"]
        assert config_obj.temperature == 0.5

    def test_generate_returns_text(self, router_default: LLMRouter):
        """generate 應回傳 API 回應的文字"""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._make_mock_result(
            "這是回應"
        )

        router_default._client = mock_client
        router_default._initialized = True

        result = router_default.generate("你好", role=LLMRole.WORK)
        assert result == "這是回應"

    def test_generate_with_system_instruction(self, router_default: LLMRouter):
        """system_instruction 應傳入 API 配置"""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._make_mock_result("ok")

        router_default._client = mock_client
        router_default._initialized = True

        router_default.generate(
            "test",
            role=LLMRole.WORK,
            system_instruction="以 JSON 格式回應",
        )

        call_kwargs = mock_client.models.generate_content.call_args
        config_obj = call_kwargs.kwargs["config"]
        assert config_obj.system_instruction == "以 JSON 格式回應"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# get_llm_fn
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGetLlmFn:
    """get_llm_fn 回傳的 callable 測試"""

    def test_returns_callable(self, router_default: LLMRouter):
        """get_llm_fn 應回傳 callable"""
        fn = router_default.get_llm_fn(LLMRole.WORK)
        assert callable(fn)

    def test_callable_calls_generate(self, router_default: LLMRouter):
        """回傳的 callable 應透過 generate 呼叫"""
        mock_client = MagicMock()
        result_mock = MagicMock()
        result_mock.text = "mocked"
        mock_client.models.generate_content.return_value = result_mock

        router_default._client = mock_client
        router_default._initialized = True

        fn = router_default.get_llm_fn(LLMRole.EVAL)
        result = fn("test prompt")

        assert result == "mocked"
        mock_client.models.generate_content.assert_called_once()

    def test_callable_uses_correct_role(self, router_default: LLMRouter):
        """回傳的 callable 應使用指定角色的模型"""
        mock_client = MagicMock()
        result_mock = MagicMock()
        result_mock.text = "ok"
        mock_client.models.generate_content.return_value = result_mock

        router_default._client = mock_client
        router_default._initialized = True

        fn = router_default.get_llm_fn(LLMRole.EVAL)
        fn("test")

        call_kwargs = mock_client.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "gemini-2.5-flash-lite"

    def test_callable_with_system_instruction(self, router_default: LLMRouter):
        """get_llm_fn 的 system_instruction 應嵌入 closure"""
        mock_client = MagicMock()
        result_mock = MagicMock()
        result_mock.text = "ok"
        mock_client.models.generate_content.return_value = result_mock

        router_default._client = mock_client
        router_default._initialized = True

        fn = router_default.get_llm_fn(
            LLMRole.WORK,
            system_instruction="你是概念提取器",
        )
        fn("提取概念...")

        call_kwargs = mock_client.models.generate_content.call_args
        config_obj = call_kwargs.kwargs["config"]
        assert config_obj.system_instruction == "你是概念提取器"

    def test_callable_conforms_to_llm_fn_contract(self, router_default: LLMRouter):
        """回傳的 callable 符合 (str) -> str 契約"""
        mock_client = MagicMock()
        result_mock = MagicMock()
        result_mock.text = "response text"
        mock_client.models.generate_content.return_value = result_mock

        router_default._client = mock_client
        router_default._initialized = True

        fn = router_default.get_llm_fn(LLMRole.WORK)

        # 應接受 str 並回傳 str
        result = fn("input prompt")
        assert isinstance(result, str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 統計
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStats:
    """路由統計測試"""

    def test_initial_stats(self, router_default: LLMRouter):
        """初始統計應為零"""
        s = router_default.stats
        assert s.total_calls == 0
        assert s.failures == 0
        assert s.calls_by_role == {}

    def test_stats_increment_on_generate(self, router_default: LLMRouter):
        """generate 成功時統計應遞增"""
        mock_client = MagicMock()
        result_mock = MagicMock()
        result_mock.text = "ok"
        mock_client.models.generate_content.return_value = result_mock

        router_default._client = mock_client
        router_default._initialized = True

        router_default.generate("a", role=LLMRole.WORK)
        router_default.generate("b", role=LLMRole.EVAL)
        router_default.generate("c", role=LLMRole.WORK)

        s = router_default.stats
        assert s.total_calls == 3
        assert s.calls_by_role["work"] == 2
        assert s.calls_by_role["eval"] == 1
        assert s.failures == 0

    def test_stats_failures_on_error(self, router_default: LLMRouter):
        """generate 主模型 + fallback 均失敗時 failures 應遞增"""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("API down")

        router_default._client = mock_client
        router_default._initialized = True
        # 停用 Ollama fallback，確保測試行為確定性
        router_default._OLLAMA_FALLBACK_ENABLED = False

        with pytest.raises(RuntimeError):
            router_default.generate("test", role=LLMRole.WORK)

        s = router_default.stats
        assert s.total_calls == 1
        assert s.failures == 1

    def test_stats_fallback_used_on_primary_error(self, router_default: LLMRouter):
        """主模型失敗但 Ollama fallback 成功時，fallback_used 應遞增且不 raise"""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("API down")

        router_default._client = mock_client
        router_default._initialized = True
        router_default._OLLAMA_FALLBACK_ENABLED = True

        # Mock Ollama fallback 回傳成功
        with patch.object(
            router_default, "_try_ollama_fallback", return_value="fallback response"
        ):
            result = router_default.generate("test", role=LLMRole.WORK)

        assert result == "fallback response"
        s = router_default.stats
        assert s.total_calls == 1
        assert s.failures == 0  # fallback 成功，不計為最終失敗
        assert s.fallback_used == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _extract_text
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExtractText:
    """_extract_text 靜態方法測試"""

    def test_extract_from_text_attr(self):
        """有 .text 屬性時直接取"""
        result = MagicMock()
        result.text = "hello"
        assert LLMRouter._extract_text(result) == "hello"

    def test_extract_from_candidates(self):
        """.text 為空時從 candidates 取"""
        part = MagicMock()
        part.text = "from parts"

        content = MagicMock()
        content.parts = [part]

        candidate = MagicMock()
        candidate.content = content

        result = MagicMock()
        result.text = None
        result.candidates = [candidate]

        assert LLMRouter._extract_text(result) == "from parts"

    def test_extract_fallback(self):
        """都取不到時回傳預設文字"""
        result = MagicMock()
        result.text = None
        result.candidates = []

        text = LLMRouter._extract_text(result)
        assert "No response" in text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSummary:
    """summary 輸出測試"""

    def test_summary_contains_all_roles(self, router_default: LLMRouter):
        """summary 應列出所有角色"""
        s = router_default.summary()
        assert "chat" in s
        assert "work" in s
        assert "eval" in s

    def test_summary_contains_model_names(self, router_default: LLMRouter):
        """summary 應包含模型名稱"""
        s = router_default.summary()
        assert "gemini-3.1-flash-lite" in s  # CHAT/WORK
        assert "gemini-2.5-flash-lite" in s  # EVAL

    def test_summary_with_stats(self, router_default: LLMRouter):
        """有統計時 summary 應包含統計資訊"""
        router_default._stats.total_calls = 10
        router_default._stats.failures = 2
        router_default._stats.calls_by_role = {"work": 7, "eval": 3}

        s = router_default.summary()
        assert "10 calls" in s
        assert "2 failures" in s


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLMRole enum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLLMRole:
    """LLMRole enum 測試"""

    def test_role_values(self):
        """角色值應為小寫字串"""
        assert LLMRole.CHAT.value == "chat"
        assert LLMRole.WORK.value == "work"
        assert LLMRole.EVAL.value == "eval"

    def test_role_from_string(self):
        """應能從字串建立角色"""
        assert LLMRole("chat") == LLMRole.CHAT
        assert LLMRole("work") == LLMRole.WORK
        assert LLMRole("eval") == LLMRole.EVAL

    def test_invalid_role_raises(self):
        """無效角色名應 raise ValueError"""
        with pytest.raises(ValueError):
            LLMRole("invalid")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RoleConfig dataclass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRoleConfig:
    """RoleConfig dataclass 測試"""

    def test_creation(self):
        """基本建立"""
        cfg = RoleConfig(model="test", temperature=0.5, max_tokens=100)
        assert cfg.model == "test"
        assert cfg.temperature == 0.5
        assert cfg.max_tokens == 100
        assert cfg.description == ""  # 預設空字串

    def test_with_description(self):
        """含 description 建立"""
        cfg = RoleConfig(
            model="test", temperature=0.5, max_tokens=100,
            description="測試用",
        )
        assert cfg.description == "測試用"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Ollama fallback：num_gpu / keep_alive
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestOllamaFallbackOptions:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_FALLBACK_NUM_GPU", raising=False)
        monkeypatch.delenv("OLLAMA_FALLBACK_KEEP_ALIVE", raising=False)

    @staticmethod
    def _post(router: LLMRouter, structured: bool = False) -> dict:
        router._OLLAMA_FALLBACK_ENABLED = True
        resp = MagicMock()
        resp.json.return_value = {"response": '{"a": 1}'}
        cfg = router.get_role_config(LLMRole.EVAL)
        with patch("httpx.post", return_value=resp) as post:
            if structured:
                router._try_ollama_fallback_structured("p", LLMRole.EVAL, cfg)
            else:
                router._try_ollama_fallback("p", LLMRole.EVAL, cfg)
        return post.call_args.kwargs["json"]

    @pytest.mark.parametrize("structured", [False, True])
    def test_unset_not_sent(self, router_default, structured):
        body = self._post(router_default, structured)
        assert "num_gpu" not in body["options"] and "keep_alive" not in body

    @pytest.mark.parametrize("structured", [False, True])
    def test_set_values_sent(self, router_default, monkeypatch, structured):
        monkeypatch.setenv("OLLAMA_FALLBACK_NUM_GPU", "0")
        monkeypatch.setenv("OLLAMA_FALLBACK_KEEP_ALIVE", "10m")
        body = self._post(router_default, structured)
        assert body["options"]["num_gpu"] == 0
        assert isinstance(body["options"]["num_gpu"], int)
        assert body["keep_alive"] == "10m"

    def test_integer_keep_alive(self, router_default, monkeypatch):
        monkeypatch.setenv("OLLAMA_FALLBACK_KEEP_ALIVE", "-1")
        assert self._post(router_default)["keep_alive"] == -1

    def test_invalid_values_ignored_with_warning(self, router_default, monkeypatch, caplog):
        monkeypatch.setenv("OLLAMA_FALLBACK_NUM_GPU", "abc")
        monkeypatch.setenv("OLLAMA_FALLBACK_KEEP_ALIVE", "forever")
        with caplog.at_level("WARNING"):
            body = self._post(router_default)
        assert "num_gpu" not in body["options"] and "keep_alive" not in body
        assert "OLLAMA_FALLBACK_NUM_GPU" in caplog.text and "OLLAMA_FALLBACK_KEEP_ALIVE" in caplog.text

    def test_llm_fn_forwards_max_tokens(self, router_default):
        with patch.object(router_default, "generate", return_value="ok") as gen:
            fn = router_default.get_llm_fn(LLMRole.EVAL)
            fn("p", max_tokens=2048)
            fn("p")
        assert gen.call_args_list[0].kwargs["max_tokens"] == 2048
        assert gen.call_args_list[1].kwargs["max_tokens"] is None
