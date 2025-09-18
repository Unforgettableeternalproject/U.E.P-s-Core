# STT → NLP → MEM 工作流程指南

## 概述

本文檔詳細說明 U.E.P's Core 中 STT（語音轉文字）→ NLP（自然語言處理）→ MEM（記憶管理）的完整工作流程，包括資料傳遞、身份管理和記憶操作。

## 工作流程架構

```
語音輸入 → STT模組 → Working Context → NLP模組 → Working Context → MEM模組
            ↓           ↓               ↓           ↓              ↓
        文字+語者資訊  語者樣本累積   語意分析+身份創建  身份令牌儲存   記憶操作
```

## 詳細流程說明

### 1. STT 模組（語音識別）

**輸入**: 音頻資料
**輸出**: 識別文字 + 語者資訊

#### 核心功能:
- **語音識別**: 使用 Whisper 模型進行實時語音轉文字
- **語者識別**: 使用 pyannote.audio 進行語者辨識，99.9995% 相似度閾值
- **Working Context 整合**: 語者樣本累積和決策觸發

#### 資料流出:
```python
STTOutput = {
    "text": "識別出的文字內容",
    "confidence": 0.95,
    "speaker_info": {
        "speaker_id": "speaker_001", 
        "confidence": 0.999,
        "embedding": [語者特徵向量]
    }
}
```

#### Working Context 操作:
- 將語者樣本添加到 `SPEAKER_ACCUMULATION` 上下文
- 達到閾值時觸發身份決策
- 傳遞語者資訊給後續模組

### 2. NLP 模組（語意分析）

**輸入**: STT 輸出 + Working Context 中的語者資訊
**輸出**: 語意分析結果 + 身份令牌

#### 核心功能:
- **語意理解**: 意圖識別、實體抽取、情感分析
- **身份管理**: 使用語者資訊作為鑰匙創建/獲取使用者身份
- **Working Context 整合**: 將身份令牌存入全局上下文

#### 身份處理流程:
```python
def _process_speaker_identity(self, input_data):
    speaker_id = input_data.speaker_id
    
    # 使用身份管理器處理語者識別
    identity, action = self.identity_manager.process_speaker_identification(
        speaker_id, input_data.text
    )
    
    # 將身份存入 Working Context
    working_context_manager.set_current_identity(identity.dict())
    working_context_manager.set_memory_token(identity.memory_token)
    
    return {"identity": identity, "action": action}
```

#### 資料流出:
```python
NLPOutput = {
    "primary_intent": "chat",
    "entities": [...],
    "sentiment": "positive", 
    "identity": {
        "identity_id": "user_12345",
        "memory_token": "mem_token_abc",
        "preferences": {...}
    }
}
```

### 3. MEM 模組（記憶管理）

**輸入**: NLP 輸出 + Working Context 中的身份令牌
**輸出**: 記憶檢索結果 + 更新後的記憶庫

#### 核心功能:
- **身份隔離**: 使用 Memory Token 決定存取權限
- **短期記憶**: 對話快照管理和比對
- **長期記憶**: 使用者特徵記憶和 RAG 總結
- **LLM 整合**: 記憶上下文整理和動態更新

#### 記憶操作流程:
```python
def handle(self, data):
    # 從 Working Context 獲取身份資訊
    identity = working_context_manager.get_current_identity()
    memory_token = working_context_manager.get_memory_token()
    
    if memory_token:
        # 使用記憶令牌進行身份隔離存取
        memories = self.memory_manager.retrieve_memories(
            query=data.text,
            memory_token=memory_token,
            memory_type=data.memory_type
        )
        
        # 對話快照比對和創建
        snapshot_action = self._compare_and_create_snapshot(
            data.text, identity, memories
        )
        
        # RAG 總結和 LLM 指令生成
        llm_context = self._prepare_llm_context(memories, identity)
        
        return self._create_response(memories, llm_context, snapshot_action)
```

## Working Context 跨模組資料共享

### 身份資訊流動:
```python
# STT → Working Context
working_context_manager.add_data_to_context(
    ContextType.SPEAKER_ACCUMULATION, 
    speaker_sample
)

# NLP → Working Context  
working_context_manager.set_current_identity(identity_data)
working_context_manager.set_memory_token(memory_token)

# Working Context → MEM
identity = working_context_manager.get_current_identity()
memory_token = working_context_manager.get_memory_token()
```

### 上下文類型:
- `SPEAKER_ACCUMULATION`: 語者樣本累積
- `IDENTITY_MANAGEMENT`: 身份管理決策
- `CONVERSATION`: 對話上下文管理
- `CROSS_MODULE_DATA`: 跨模組資料共享

## 動態路由決策

### 路由策略:
```python
# 能力基礎路由
if requires_speech_recognition:
    route_to = "stt_module"
elif requires_language_understanding:  
    route_to = "nlp_module"
elif requires_memory_operations:
    route_to = "mem_module"

# 上下文感知路由
if working_context.has_pending_identity_decision():
    route_to = "nlp_module"  # 優先處理身份決策
```

### 決策引擎:
- **能力匹配**: 根據模組能力自動路由
- **上下文感知**: 考慮當前 Working Context 狀態
- **優先級管理**: 身份決策 > 記憶操作 > 一般對話

## 系統特色

### 1. 身份隔離系統
- 每個使用者擁有獨立的 Memory Token
- 記憶存取完全隔離，保護隱私
- 支援多使用者同時操作

### 2. 智能快照管理
- 自動偵測對話主題變化
- 創建新快照或延續現有對話
- 短期/長期記憶分層管理

### 3. RAG 整合
- 記憶檢索結合語義相似度
- LLM 上下文自動整理
- 動態記憶庫更新

### 4. 決策驅動架構
- Working Context 決策處理器
- 自動化與人工決策結合
- 可插拔的決策策略

## 配置要求

### 模組啟用狀態:
```yaml
modules_enabled:
  stt_module: true
  nlp_module: true  
  mem_module: true

modules_refactored:
  stt_module: true
  nlp_module: true
  mem_module: true
```

### 依賴套件:
- **STT**: torch, transformers, pyaudio, pyannote.audio
- **NLP**: pydantic, 自然語言處理庫
- **MEM**: sentence_transformers, faiss, numpy

## 總結

U.E.P's Core 的 STT→NLP→MEM 工作流程是一個高度整合的系統，通過 Working Context 實現了模組間的無縫資料傳遞和決策協調。系統支援：

- ✅ **完整的語者身份管理** - 從語音到身份令牌的自動映射
- ✅ **智能記憶隔離** - 基於身份的安全記憶存取
- ✅ **動態決策路由** - 上下文感知的模組通信
- ✅ **可擴展架構** - 支援新模組和決策策略的插入

這個架構設計優秀，為 UEP 提供了強大的多模組協作基礎。