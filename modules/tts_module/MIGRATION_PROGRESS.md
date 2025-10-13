# TTS模組重構 - 遷移進度

## 已完成

✅ **文件結構重組** (2025/10/11)
- 舊實現移至 `deprecated/` 資料夾
  - infer_core.py
  - vc_infer_pipeline.py
  - rmvpe.py
  - config.py
  - lib/
  - temp/
- 保留核心文件維持API
  - tts_module.py
  - schemas.py
  - config.yaml
  - __init__.py

✅ **IndexTTS核心文件遷移** (2025/10/11)
- 從 Index-TTS 專案複製核心文件
  - ✅ lite_engine.py (主推論引擎)
  - ✅ gpt/ (GPT模型目錄)
  - ✅ s2mel/modules/ (S2Mel轉換模組)
  - ✅ utils/ (工具函數)
- 移除訓練專用模組
  - ❌ campplus/ (僅訓練時需要)

✅ **導入路徑修正** (2025/10/11)
- lite_engine.py: `indextts.xxx` → `.xxx` (相對導入)
- 創建必要的 `__init__.py`
  - gpt/__init__.py
  - s2mel/__init__.py
  - utils/__init__.py

✅ **套件依賴更新** (2025/10/11)
- 添加至 requirements.txt:
  - huggingface-hub==0.26.5
  - safetensors==0.4.5
  - omegaconf==2.3.0
- 已有相容套件:
  - transformers==4.48.1
  - numpy==2.0.0
  - librosa==0.11.0

---

## 待完成

### 1. 導入路徑全面檢查
- [ ] 檢查所有子模組的相對導入
- [ ] 確認 gpt/ 下所有文件的導入路徑
- [ ] 確認 s2mel/modules/ 下所有文件的導入路徑
- [ ] 確認 utils/ 下所有文件的導入路徑

### 2. 模型權重遷移
- [ ] 複製 checkpoints/ 目錄
  - config.yaml
  - gpt.pth
  - s2mel.pth
  - hf_cache/ (semantic_codec 和 bigvgan)
- [ ] 複製 characters/ 目錄
  - uep-1.pt
  - uep-1_metadata.json

### 3. TTS模組主文件重構
- [ ] 閱讀現有 tts_module.py 的API設計
- [ ] 設計新的 TTSModule 類別
  - 繼承 ModuleBase
  - 整合 IndexTTSLite 引擎
  - 實現情緒映射 (Status Manager → 8維向量)
  - 實現文本分段處理
  - 實現異步語音生成
- [ ] 更新 schemas.py 數據結構
  - TTSInput
  - TTSOutput
  - EmotionVector (8維)

### 4. 情緒向量映射系統
- [ ] 設計 Status Manager 數值 → 8維情緒向量轉換
  - mood (心情)
  - pride (自豪感)
  - helpfulness (樂於助人)
  - boredom (無聊度)
  - → [happy, sad, angry, fear, disgust, surprise, neutral, excitement]
- [ ] 實現動態情緒強度控制
- [ ] 測試情緒向量對語音的影響

### 5. 文本分段與串流
- [ ] 實現長文本智能分段
  - 按句子分段
  - 按語義分段
  - 限制每段最大長度
- [ ] 實現異步串流生成
  - 不堵塞系統循環
  - 支援分段播放
  - 支援中斷控制

### 6. 性能優化
- [ ] 模型預熱機制
  - 啟動時進行一次語音合成
  - 加載並緩存所有模型
- [ ] 響應時間優化 (目標 < 2秒)
  - 減少模型加載時間
  - 優化推論流程
  - 使用 GPU 加速

### 7. 測試與驗證
- [ ] 重建 unit_tests/test_tts_module.py
- [ ] 重建 devtools/module_tests/tts_test.py
- [ ] Debug GUI 整合
  - TTS test tab
  - 情緒向量調整介面
  - 實時語音測試
- [ ] Main system loop 整合測試

### 8. 文檔與配置
- [ ] 更新 config.yaml
  - IndexTTS 相關配置
  - 模型路徑配置
  - 情緒映射配置
- [ ] 編寫使用文檔
- [ ] 編寫開發文檔

---

## 已知問題

### 套件版本衝突風險
- IndexTTS 需要 torch>=2.0.0
- 現有專案使用 torch==2.7.0+cu128
- 需要測試相容性

### 路徑結構差異
- IndexTTS 原本: `indextts/xxx`
- 現在: `modules/tts_module/xxx`
- 所有相對導入都需要檢查和修正

### 模型權重大小
- 總計約 2-3 GB
- gpt.pth: ~500 MB
- s2mel.pth: ~300 MB
- semantic_codec: ~500 MB
- bigvgan: ~1.5 GB
- 需要考慮 Git LFS 或外部存儲

---

## 驗收條件 (來自 TTS 待辦.md)

1. ✅ 能夠成功地從IndexTTS遷移架構 (檔案已遷移,待測試)
2. ⏳ TTS以新系統架構設計 (進行中)
3. ⏳ 根據Status Manager調整語音風格 (待實現)
4. ⏳ 處理長文本分段輸出與串流播放 (待實現)
5. ⏳ 響應時間控制在2秒內 (待優化)
6. ⏳ 重建並通過 unit_tests (待實現)
7. ⏳ 重建並通過 module_tests (待實現)
8. ⏳ Debug GUI 運作流暢 (待實現)
9. ⏳ Main system loop 成功運作 (待實現)

**重要提醒**: 在所有標準完成前,不要在 config.yaml 中標記 TTS 為已重構!

---

## 下一步行動

1. **檢查所有導入路徑** - 確保所有子模組能正確導入
2. **安裝新套件** - 測試套件相容性
3. **複製模型權重** - 準備推論所需的模型文件
4. **測試 lite_engine.py** - 驗證核心引擎能否正常運作
