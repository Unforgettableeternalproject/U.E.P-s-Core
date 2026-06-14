# Session Control — LLM 會話控制子系統

> Prompt / Token / Context 三大指標優化引擎

從 [U.E.P's Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) 遷出的獨立子系統實驗，與 [Echo Memory](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem) 互補：
- **Echo Memory** = 跨會話的記憶持久化
- **Session Control** = 會話內的上下文壓縮與管理

## 核心架構

四層漸進式壓縮，參考 Anthropic Claude Code 的設計哲學：**漸進式降級，不是斷崖式截斷**。

```
Layer 0: Token Accounting    — 實時追蹤 token 使用量
Layer 1: Micro-Pruning @60%  — 規則式清理（零 LLM 呼叫）
Layer 2: Rolling Summary @80% — 增量摘要（LLM 摘要 → Echo Memory L1）
Layer 3: Full Compaction @92% — 九+一段式結構化壓縮（→ Echo Memory L2）
Layer 4: Emergency           — API 413 錯誤觸發，緊急截斷 + 斷路器
```

### 神聖段落（Sacred Content）

任何壓縮層級都不會移除的內容：
- **人設上下文**（角色名稱、人格特徵、說話風格）
- **使用者明確指令原文**
- **當前任務目標**

## 快速開始

```bash
pip install -e ".[dev]"

# 執行測試
pytest -v

# 使用範例
python -c "
from echo_thought_core import ContextManager, CompactionConfig, PersonaContext

config = CompactionConfig(backend='anthropic', context_window_size=200000)
persona = PersonaContext(name='U.E.P', traits=['friendly', 'professional'])
manager = ContextManager(config=config, persona=persona)

manager.add_message(role='user', content='Hello!')
manager.add_message(role='assistant', content='Hi there!')
print(manager.get_usage())
"
```

## 支援的 LLM 後端

| 後端 | 狀態 | 說明 |
|------|------|------|
| Anthropic Claude | 🔲 開發中 | 含 server-side compaction (Beta) |
| OpenAI | 🔲 計畫中 | GPT-4o / GPT-4o-mini |
| Google Gemini | 🔲 計畫中 | Vertex AI + context caching |
| 本地模型 | 🔲 計畫中 | Qwen2.5 via llama.cpp / vLLM |

## 技術棧

- Python 3.10+
- Pydantic 2.5+（Schema 驗證）
- tiktoken（Token 計數）
- rich（CLI 顯示）

## 開發路線

### MVP（Phase 1）
- [x] 專案骨架與 CI
- [ ] Token Accounting（精確計數 + 預算管理）
- [ ] Layer 1 Micro-Pruning（規則式清理）
- [ ] Layer 3 Full Compaction（LLM 摘要）
- [ ] Layer 4 Emergency（截斷 + 斷路器）

### Phase 2
- [ ] Layer 2 Rolling Summary + Echo Memory L1 整合
- [ ] LLMLingua-2 整合（Layer 1 增強）
- [ ] 人設保護機制
- [ ] 多後端 Compaction Provider 抽象

### Phase 3
- [ ] 自適應閾值
- [ ] StreamingLLM 整合（本地模型 KV cache）
- [ ] 壓縮品質評估迴路

## 與 U.E.P Core 的整合

完成 MVP 後，將作為 `echo_thought_core` 模組併入 U.E.P Core：
- 替換現有 `llm_module` 中的 `cache_manager.py` 和部分 `prompt_manager.py` 功能
- 透過 Event Bus 注入壓縮事件
- 與 Echo Memory 的 MCP Server 協作做記憶持久化

## 授權

MIT License
