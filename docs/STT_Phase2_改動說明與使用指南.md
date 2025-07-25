# STT Module Phase 2 改動說明與使用指南

## 📋 概覽

STT Module Phase 2 是一次重大重構，引入了三個核心功能：
1. **感應式啟動 (Always-on)** - 背景持續監聽，自動觸發識別
2. **聲音識別 (Speaker Identification)** - 識別不同說話人的聲音特徵
3. **智能啟動 (Smart Activation)** - 基於語義分析的智能觸發機制

## 🔄 主要改動

### 1. 新增功能組件

#### A. 聲音活動檢測 (VAD) 
- **檔案**: `modules/stt_module/voice_activity_detector.py`
- **功能**: 即時檢測語音活動，區分語音和靜默
- **特點**: 動態閾值調整、能量計算、狀態追蹤

#### B. 聲音識別
- **檔案**: `modules/stt_module/speaker_identification.py`
- **功能**: 使用 MFCC 特徵識別不同說話人
- **特點**: 持久化模型、相似度計算、新用戶註冊

#### C. 智能啟動
- **檔案**: `modules/stt_module/smart_activation.py`
- **功能**: 多模式語義分析，智能判斷是否啟動
- **特點**: 關鍵字匹配、上下文分析、信心度評估

### 2. 核心模組重構

#### 原版 `stt_module.py` vs 新版對比

**原版特性**:
```python
# 簡單的單次錄音
result = stt.handle()

# 基本即時監聽
stt.start_realtime(callback)
stt.stop_realtime()
```

**新版特性**:
```python
# 多模式支援
result = stt.handle({
    "mode": "manual|smart|always_on",
    "language": "zh-TW",
    "enable_speaker_id": True,
    "duration": 5,
    "context": "conversation_context"
})

# Always-on 背景監聽
stt.start_always_on(callback)
stt.stop_always_on()

# 統計資訊
speaker_stats = stt.get_speaker_stats()
activation_stats = stt.get_activation_stats()
```

### 3. 數據結構擴展

#### 新增 Schemas (`modules/stt_module/schemas.py`)

```python
class ActivationMode(Enum):
    MANUAL = "manual"           # 手動錄音模式
    ALWAYS_ON = "always_on"     # 背景監聽模式
    SMART = "smart"             # 智能啟動模式

class SpeakerInfo(BaseModel):
    speaker_id: str             # 說話人 ID
    similarity: float           # 相似度分數
    is_new_speaker: bool        # 是否為新用戶

class STTInput(BaseModel):
    mode: ActivationMode = ActivationMode.MANUAL
    language: str = "zh-TW"
    enable_speaker_id: bool = False
    duration: Optional[int] = None
    context: Optional[str] = None

class STTOutput(BaseModel):
    text: str
    confidence: float
    speaker_info: Optional[SpeakerInfo] = None
    activation_reason: Optional[str] = None
    error: Optional[str] = None
```

### 4. 配置系統升級

#### 新配置結構 (`modules/stt_module/config.yaml`)

```yaml
# 感應式啟動配置
always_on:
  enabled: true
  vad_sensitivity: 0.6
  min_speech_duration: 0.5
  max_silence_duration: 2.0
  energy_threshold: 4000
  dynamic_threshold: true

# 智能啟動配置
smart_activation:
  enabled: true
  context_keywords: ["UEP", "你好", "幫我", "請"]
  conversation_mode: true
  activation_confidence: 0.7

# 聲音識別配置
speaker_identification:
  enabled: true
  feature_extraction:
    mfcc_features: 13
    sample_rate: 16000
    frame_length: 2048
    hop_length: 512
  similarity_threshold: 0.8
  new_speaker_threshold: 0.6
  min_samples_for_model: 5
```

## 🎯 透過 Controller 存取

### 1. 更新的 Controller 函數

#### A. 基本單次測試
```python
# 手動模式 (與舊版相容)
result = stt_test_single(mode="manual")

# 智能啟動模式
result = stt_test_single(mode="smart", enable_speaker_id=True)
```

#### B. 智能啟動專用測試
```python
# 智能啟動測試
result = stt_test_smart_activation()
```

#### C. Always-on 背景監聽
```python
# 30秒背景監聽
stt_test_always_on(duration=30)
```

#### D. 統計資訊查詢
```python
# 獲取使用統計
stats = stt_get_stats()
```

### 2. 新版 Controller API

#### 原版調用方式 (仍相容)
```python
from core.controller import modules

stt = modules["stt"]
result = stt.handle()  # 使用預設參數
```

#### 新版調用方式
```python
from core.controller import modules

stt = modules["stt"]

# 手動模式
result = stt.handle({
    "mode": "manual",
    "language": "zh-TW",
    "enable_speaker_id": True,
    "duration": 5
})

# 智能啟動模式
result = stt.handle({
    "mode": "smart",
    "language": "zh-TW",
    "enable_speaker_id": True,
    "context": "conversation"
})

# Always-on 背景監聽
def on_speech_detected(result):
    print(f"檢測到語音: {result['text']}")
    print(f"啟動原因: {result['activation_reason']}")

stt.start_always_on(callback=on_speech_detected)
# ... 讓它在背景運行 ...
stt.stop_always_on()
```

### 3. 結果處理

#### 新版結果結構
```python
result = {
    "text": "識別的文字內容",
    "confidence": 0.95,
    "speaker_info": {
        "speaker_id": "user_001",
        "similarity": 0.87,
        "is_new_speaker": False
    },
    "activation_reason": "smart_keyword_match",
    "error": None,
    "processing_time": 1.23
}
```

#### 處理範例
```python
def handle_stt_result(result):
    if result.get("error"):
        print(f"❌ 錯誤: {result['error']}")
        return
    
    text = result.get("text", "")
    confidence = result.get("confidence", 0)
    
    print(f"📝 識別結果: {text}")
    print(f"🎯 信心度: {confidence:.2f}")
    
    # 聲音識別資訊
    speaker_info = result.get("speaker_info")
    if speaker_info:
        speaker_id = speaker_info.get("speaker_id", "Unknown")
        similarity = speaker_info.get("similarity", 0)
        is_new = speaker_info.get("is_new_speaker", False)
        
        print(f"🗣️ 說話人: {speaker_id}")
        print(f"📊 相似度: {similarity:.2f}")
        if is_new:
            print("🆕 這是新用戶！")
    
    # 啟動資訊
    activation_reason = result.get("activation_reason")
    if activation_reason:
        print(f"🚀 啟動原因: {activation_reason}")
```

## 🛠️ 實際使用場景

### 1. 一般對話系統
```python
# 啟動背景監聽
def conversation_handler(result):
    text = result.get("text", "")
    speaker_id = result.get("speaker_info", {}).get("speaker_id", "Unknown")
    
    # 傳遞給 NLP 模組處理
    nlp_result = modules["nlp"].handle({"text": text})
    
    # 個人化回應 (基於說話人)
    personalized_response = customize_response(nlp_result, speaker_id)
    
    # 使用 TTS 回應
    modules["tts"].handle({"text": personalized_response})

# 啟動 Always-on 模式
modules["stt"].start_always_on(callback=conversation_handler)
```

### 2. 智能助手啟動
```python
def smart_assistant():
    # 智能啟動監聽
    result = modules["stt"].handle({
        "mode": "smart",
        "language": "zh-TW",
        "enable_speaker_id": True,
        "context": "assistant_mode"
    })
    
    if result.get("activation_reason") == "smart_keyword_match":
        print("🤖 助手已啟動！")
        # 進入對話流程
        continue_conversation()
```

### 3. 多用戶支援
```python
def multi_user_system():
    # 檢查說話人統計
    stats = modules["stt"].get_speaker_stats()
    
    for speaker_id, interaction_count in stats.items():
        print(f"用戶 {speaker_id}: {interaction_count} 次互動")
    
    # 為不同用戶提供個人化服務
    def personalized_handler(result):
        speaker_info = result.get("speaker_info")
        if speaker_info:
            speaker_id = speaker_info.get("speaker_id")
            # 載入用戶特定設定
            user_preferences = load_user_preferences(speaker_id)
            # 個人化處理
            handle_user_request(result["text"], user_preferences)
```

## 🔧 開發與測試

### 1. 運行完整測試
```bash
cd examples
python stt_phase2_controller_usage.py
```

### 2. 單元測試
```bash
python -m pytest module_tests/test_stt_phase2.py -v
```

### 3. 功能演示
```bash
python examples/stt_phase2_demo.py
```

## 📦 依賴套件

新增的主要依賴：
- `librosa>=0.11.0` - 音頻處理與特徵提取
- `scikit-learn>=1.6.1` - 機器學習算法
- `soundfile>=0.13.1` - 音頻檔案處理

## ⚡ 性能考量

### 1. Always-on 模式
- 背景持續運行，CPU 使用率約 5-10%
- 記憶體使用增加約 50-100MB
- 可透過配置調整 VAD 敏感度來平衡性能

### 2. 聲音識別
- 初次建立聲音模型需要 5+ 樣本
- 識別延遲約 100-200ms
- 模型持久化到檔案系統

### 3. 智能啟動
- 語義分析延遲 < 50ms
- 支援多種觸發模式
- 可動態調整啟動閾值

## 🔄 向後相容性

- ✅ 舊版 `stt.handle()` 調用仍然支援
- ✅ 原有的回調機制保持不變
- ✅ 配置檔案向下相容
- ⚠️ `start_realtime()` 建議改用 `start_always_on()`

## 🚀 未來規劃

1. **Phase 3**: 與 NLP 模組深度整合
2. **Phase 4**: 支援多語言混合識別
3. **Phase 5**: 情緒識別與語調分析
4. **Phase 6**: 離線模式支援

---

這份文件涵蓋了所有 STT Phase 2 的改動與使用方式。如有疑問，請參考範例程式或測試檔案。
