# UI 設定介面對應狀態

## 概覽
`user_settings.py` 中的 UI 控制項與 `user_settings.yaml` 的對應狀態。

## ✅ 已實作（6 項）

### 個人資訊
- [x] `uep_name_edit` → `general.identity.uep_name`
- [x] `user_name_edit` → `general.identity.user_name`

### 表現設定 - TTS
- [x] `enable_tts_checkbox` → `interaction.speech_output.enabled`
- [x] `tts_volume_slider` → `interaction.speech_output.volume`

### 行為設定
- [x] `enable_movement_checkbox` → `behavior.movement.enabled`

### 互動設定
- [x] `mouse_hover_checkbox` → `interaction.mouse_hover_enabled`

---

## ⚠️ 待實作（50+ 項）

### 個人頁籤
#### 個人偏好組
- [ ] `language_combo` → `general.system.language`
- [ ] `theme_combo` → `interface.appearance.theme`

#### 帳戶設定組
- [ ] `login_button` / `logout_button` - 功能待定

---

### 表現頁籤
#### 語音合成組
- [ ] `tts_speed_slider` → `interaction.speech_output.speed`
- [ ] `voice_combo` → `interaction.speech_output.voice`（YAML 中無此項）

#### 字幕顯示組
- [ ] `enable_subtitle_checkbox` → 無對應
- [ ] `subtitle_position_combo` → 無對應
- [ ] `subtitle_size_spinbox` → 無對應
- [ ] `subtitle_opacity_slider` → 無對應

#### 動畫設定組
- [ ] `enable_animation_checkbox` → `interface.appearance.enable_effects`
- [ ] `animation_quality_combo` → `interface.appearance.animation_quality`
- [ ] `animation_speed_slider` → `interface.access_widget.animation_speed`

#### 視覺效果組
- [ ] `shadow_checkbox` → 無對應
- [ ] `transparency_checkbox` → `interface.main_window.transparency`
- [ ] `particle_checkbox` → 無對應

---

### 行為模式頁籤
#### 系統狀態控制組
- [ ] `state_tree` (模組啟用/停用) → `advanced.modules.*_enabled`
- [ ] `enable_all_button` / `disable_all_button` / `reset_states_button` - 批次操作

#### 移動行為限制組
- [ ] `movement_boundary_combo` → `behavior.movement.boundary_mode`
- [ ] `movement_speed_slider` → 無對應（但有 `max_throw_speed`）
- [ ] `gravity_checkbox` → 無對應

#### 自動行為組
- [ ] `auto_roam_checkbox` → 無對應
- [ ] `smart_follow_checkbox` → `behavior.movement.enable_cursor_tracking`
- [ ] `auto_response_checkbox` → 無對應（但有 `require_user_input`）
- [ ] `sleep_mode_checkbox` → `behavior.auto_sleep.enabled`
- [ ] `sleep_time_spinbox` → `behavior.auto_sleep.max_idle_time`（秒 vs 分鐘）

---

### 互動頁籤
#### 滑鼠互動組
- [ ] `click_interaction_checkbox` → 無對應
- [ ] `drag_behavior_combo` → 無對應
- [ ] `double_click_combo` → 無對應

#### 鍵盤快捷鍵組
- [ ] 快捷鍵編輯 → `shortcuts.*`（目前為 ReadOnly）

#### 檔案拖放組
- [ ] `file_drop_checkbox` → 無對應
- [ ] `supported_files_edit` → 無對應
- [ ] `drop_action_combo` → 無對應

#### 通知設定組
- [ ] `notifications_checkbox` → 無對應
- [ ] `notification_position_combo` → 無對應
- [ ] `notification_duration_spinbox` → 無對應

---

### 其他頁籤
#### 進階設定組
- [ ] `developer_mode_checkbox` → `general.system.enable_debug_mode`
- [ ] `debug_logging_checkbox` → `advanced.logging.enabled`
- [ ] `performance_monitor_checkbox` → 無對應
- [ ] `auto_update_checkbox` → 無對應

#### 資料與隱私組
- [ ] `save_conversations_checkbox` → `interaction.memory.auto_save_conversations`
- [ ] `data_retention_spinbox` → `privacy.data_retention.conversation_retention_days`
- [ ] `clear_data_button` / `export_data_button` - 功能按鈕

#### 系統維護組
- [ ] `restart_button` / `reset_settings_button` / `check_updates_button` / `repair_system_button` - 功能按鈕

#### 關於組
- [ ] `website_button` / `license_button` / `help_button` - 連結按鈕

---

## 🔍 發現的問題

### 1. YAML 中有但 UI 中無
- `interaction.speech_input.*` (STT 設定) - 完全沒有 UI
- `interaction.memory.*` (MEM 設定) - 只有部分
- `interaction.conversation.*` (LLM 設定) - 沒有 UI
- `interaction.proactivity.*` (主動性設定) - 沒有 UI
- `behavior.mischief.*` (搗蛋模式) - 沒有 UI
- `behavior.permissions.*` (權限設定) - 沒有 UI
- `monitoring.*` (監控與背景工作) - 沒有 UI
- `advanced.performance.*` (效能設定) - 沒有 UI
- `advanced.experimental.*` (實驗性功能) - 沒有 UI

### 2. UI 中有但 YAML 中無
- 字幕顯示相關設定（整組）
- 粒子效果、陰影效果
- 語音選擇（voice_combo）
- 移動速度滑桿、重力效果
- 通知系統（整組）
- 檔案拖放（整組）
- 效能監控、自動更新

### 3. 命名/單位不一致
- `sleep_time_spinbox` 使用「分鐘」，但 YAML 中 `max_idle_time` 是「秒」
- `movement_speed_slider` vs `max_throw_speed`（概念不同）
- `mouse_hover_enabled` 路徑不一致（頂層 vs 巢狀）

---

## 📋 建議方案

### 短期（立即）
1. **為已存在的 UI 控制項完成 YAML 對應**
   - 優先處理常用功能（語速、主題、動畫品質等）
   - 暫時停用/隱藏無對應的控制項

2. **添加視覺指示器**
   - 在尚未實作的控制項旁顯示 "⚠️ 開發中"
   - 或者直接 `setEnabled(False)` 並加上 tooltip

### 中期
3. **補全關鍵功能 UI**
   - STT 設定（麥克風、VAD 靈敏度等）
   - MEM 設定（記憶保留、語意搜尋等）
   - LLM 對話設定（溫度、上下文等）

4. **移除或實作多餘 UI**
   - 決定是否真需要「字幕顯示」功能
   - 決定「通知系統」的實作方式
   - 整合或移除重複概念的控制項

### 長期
5. **UI 重構**
   - 根據實際功能重新組織頁籤
   - 考慮動態生成 UI（從 YAML schema）
   - 添加進階/簡易模式切換

---

## 🎯 下一步行動

### 立即任務
1. ✅ 添加 access_widget 關閉按鈕
2. ⏳ 完成核心設定項的 load/save 實作
3. ⏳ 為未實作控制項添加禁用狀態

### 本週任務
- 補全「個人偏好」組的對應（語言、主題）
- 補全「動畫設定」組的對應
- 添加「STT 設定」UI 組

### 未來任務
- 重新設計 UI 結構
- 考慮使用動態表單生成
- 添加設定搜尋功能
