# 模組重載策略文檔

## 概述

本文檔定義 U.E.P 系統中所有模組的統一重載機制，確保使用者設定變更能夠安全、一致地應用到各個模組。

## 重載機制分類

### 1. **即時生效（Runtime Update）**
- **定義**：修改運行時參數，無需重啟模組實例
- **適用場景**：純數值調整、開關切換、不涉及資源重新載入
- **實現方式**：直接更新實例變數
- **示例**：
  - TTS 音量、語速調整
  - MOV 摩擦係數、投擲速度
  - LLM 對話溫度

### 2. **條件重載（Conditional Reload）**
- **定義**：根據當前狀態決定是否需要重新初始化
- **適用場景**：涉及外部資源（音頻流、設備索引）但可以延遲應用
- **實現方式**：標記需要重載，在下次使用時重新初始化
- **示例**：
  - STT 麥克風索引（停止當前流，下次錄音時重新初始化）
  - STT 啟動模式切換（標記變更，建議重啟監聽）

### 3. **禁用重載（Reload Disabled）**
- **定義**：需要完整重啟應用程式才能生效
- **適用場景**：涉及底層架構、GUI 框架、模型載入
- **實現方式**：從 `RELOAD_REQUIRED` 中移除，UI 顯示重啟提示
- **示例**：
  - UI 縮放比例（需要重建 QWidget 樹）
  - UI 硬體加速（需要重新初始化 Qt 渲染引擎）
  - STT/TTS 模型路徑（需要重新載入模型權重）

## 各模組重載策略

### STT 模組（stt_module）

#### 即時生效
- `vad_sensitivity`：更新 VAD 實例的靈敏度和能量閾值
- `min_speech_duration`：更新 VAD 的語音持續閾值
- `wake_word_confidence`：更新實例變數（供 NLP 使用）
- `enable_continuous_mode`：切換啟動模式（標記需要重啟監聽）

#### 條件重載
- `microphone_device_index`：
  - 停止當前音頻流
  - 設置 `audio_stream = None`
  - 下次錄音時自動使用新索引重新初始化

#### 禁用重載
- `enabled`：STT 模組開關（由外部控制啟停）
- `whisper_model_id`：需要重啟應用（模型載入耗時）

---

### TTS 模組（tts_module）

#### 即時生效
- `volume`：更新 `user_volume` 實例變數
- `speed`：更新 `user_speed` 實例變數
- `default_emotion`：更新 `user_emotion` 實例變數
- `emotion_intensity`：更新 `user_emotion_intensity` 和 `emotion_mapper.max_strength`

#### 禁用重載
- `enabled`：TTS 模組開關（由外部控制）
- `model_dir`：需要重啟應用（IndexTTS 模型路徑）
- `default_character`：需要重啟應用（角色權重載入）

---

### MOV 模組（mov_module）

#### 即時生效
- `boundary_mode`：更新 `boundary_mode` 實例變數
- `enable_throw_behavior`：調整 `_throw_handler.throw_threshold_speed`
  - 禁用：設為 999999.0（實際不觸發）
  - 啟用：恢復預設閾值
- `max_throw_speed`：更新 `_throw_handler.max_throw_speed`
- `enable_cursor_tracking`：設置 `_cursor_tracking_enabled`，並停止當前追蹤
- `movement_smoothing`：更新 `_smoothing_enabled`，重置速度緩衝
- `ground_friction`：更新 `physics.ground_friction`

---

### LLM 模組（llm_module）

#### 即時生效
- `user_additional_prompt`：下次生成時自動套用
- `temperature`：更新 `model.temperature`
- `enable_learning`：更新 `learning_engine.learning_enabled`
- `allow_internet_access`：更新 `allow_internet_access`
- `allow_api_calls`：更新 `allow_api_calls`
- `network_timeout`：更新 `network_timeout`

---

### MEM 模組（mem_module）

#### 即時生效
- `enabled`：模組開關（日誌記錄，實際控制由外部處理）

---

### ANI 模組（ani_module）

#### 即時生效
- `enable_hardware_acceleration`：更新 `hardware_acceleration`
- `reduce_animations_on_battery`：更新 `reduce_on_battery`

#### 禁用重載
- `animation_quality`：需要重載 ANI 模組（涉及動畫資源載入）

---

### SYS 模組（sys_module）

#### 即時生效
- `behavior.permissions.*`：所有權限設定即時生效

---

### UI 模組（ui_module）

#### 禁用重載
- `max_fps`：需要重啟應用（涉及 QTimer 和渲染循環重建）
- `enable_hardware_acceleration`：需要重啟應用（Qt 渲染引擎初始化）
- `ui_scale`：需要重啟應用（QWidget 樹重建）
- `theme`：需要重啟應用（樣式表重新載入）

**理由**：UI 模組的重載涉及：
1. 所有 QWidget 實例的重建
2. Desktop Pet（主視窗）的銷毀和重新創建
3. Access Widget 的重新定位和動畫重置
4. 可能導致視覺閃爍和狀態丟失

**未來改進方向**：
- 實現 Loading Overlay，在重載期間顯示
- 保存當前 UEP 位置和動畫狀態
- 銷毀所有 UI 實例 → 重新初始化 → 恢復狀態

---

## 實現標準

### 1. 方法簽名

所有模組必須實現：

```python
def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
    """
    從 user_settings.yaml 重載設定
    
    Args:
        key_path: 設定路徑（如 "interaction.speech_output.volume"）
        value: 新值
        
    Returns:
        是否成功重載
    """
```

### 2. 日誌規範

```python
# 開始重載
info_log(f"[{module_name}] 🔄 重載使用者設定: {key_path} = {value}")

# 參數更新
info_log(f"[{module_name}] {參數名稱}已更新: {old_value} → {new_value}")

# 未處理的路徑
debug_log(2, f"[{module_name}] 未處理的設定路徑: {key_path}")

# 錯誤處理
error_log(f"[{module_name}] 重載使用者設定失敗: {e}")
traceback.format_exc()
```

### 3. 註冊回調

所有後端模組在初始化時必須註冊重載回調：

```python
# 在 __init__ 末尾
from configs.user_settings_manager import user_settings_manager
user_settings_manager.register_reload_callback(
    "module_name",
    self._reload_from_user_settings
)
```

### 4. 錯誤處理

```python
try:
    # 重載邏輯
    return True
except Exception as e:
    error_log(f"[{module_name}] 重載失敗: {e}")
    import traceback
    error_log(traceback.format_exc())
    return False
```

---

## UserSettingsManager 配置

在 `configs/user_settings_manager.py` 的 `RELOAD_REQUIRED` 字典中定義需要重載的設定：

```python
RELOAD_REQUIRED = {
    # 格式: "設定路徑": ["模組1", "模組2", ...]
    
    # 即時生效的設定
    "interaction.speech_output.volume": ["tts_module"],
    "behavior.movement.ground_friction": ["mov_module"],
    
    # 禁用重載的設定（不要加入此字典）
    # "advanced.performance.max_fps": [],  # ❌ 不應該在這裡
}
```

**注意**：
- 只有需要**即時生效**或**條件重載**的設定才加入 `RELOAD_REQUIRED`
- 需要**重啟應用**的設定不應加入，改為在 UI 顯示「⚠️ 需要重啟」標記

---

## 未來改進

### 階段 1：完善當前機制（✅ 已完成）
- [x] 統一所有模組的 `_reload_from_user_settings` 簽名
- [x] 標準化日誌格式
- [x] 移除 UI 模組的重載配置

### 階段 2：實現完整模組重載（⏳ 計劃中）
- [ ] 設計 Loading Overlay 系統
- [ ] 實現模組狀態保存/恢復機制
- [ ] 支援 UI 模組的優雅重載
- [ ] 支援 STT/TTS 模型的熱切換

### 階段 3：使用者體驗優化（⏳ 計劃中）
- [ ] 設定變更時顯示預期效果（即時/重啟）
- [ ] 提供「一鍵重啟」按鈕
- [ ] 批次應用設定變更（減少重載次數）

---

## 測試清單

### 功能測試
- [ ] STT 麥克風索引變更後錄音正常
- [ ] TTS 音量/語速即時調整生效
- [ ] MOV 摩擦係數變更後物理行為改變
- [ ] LLM 溫度調整後回應風格變化

### 邊界測試
- [ ] GS 活躍時設定變更（應標記為待處理）
- [ ] 連續快速變更同一設定（應正確套用最新值）
- [ ] 重載失敗時的錯誤處理（日誌記錄、返回 False）

### 穩定性測試
- [ ] 長時間運行後重載功能正常
- [ ] 重載過程中不會造成記憶體洩漏
- [ ] 重載不會影響其他模組運行

---

## 參考資料

- `configs/user_settings_manager.py`：重載管理器實現
- `configs/user_settings.yaml`：使用者設定檔案
- 各模組的 `_reload_from_user_settings` 方法實現

---

**最後更新**：2025-11-30  
**版本**：1.0  
**維護者**：U.E.P 開發團隊
