# Legacy TTS Source Snapshot

This folder contains copied source code from `U.E.P-s-Core` for isolated migration work.

Excluded intentionally:

- `modules/tts_module/checkpoints/`
- runtime temp files
- Python caches
- voice/model weights under `models/tts/`

Keep large model artifacts outside Git and configure their paths through local settings when running heavy inference tests.


## VOX Backend（VoxCPM2 串流）

`echo_tts.runtime.vox_backend.VoxBackend`，供 Echo Stream 的 `VoxSpeakStage` 使用。

```python
b = VoxBackend({"model_dir": "...", "lora_dir": "..."})
b.prepare()                                  # 同步：載入模型與 LoRA、建 prompt cache、暖機
for chunk in b.stream("你好", lang=None, speed=None, emotion=None, intensity=None):   # 1-D float32，48 kHz
    ...
b.unload()
```

config：

| 欄位 | 說明 |
|---|---|
| `model_dir` | VoxCPM2 模型目錄（必填，不進 git） |
| `lora_dir` | 台灣華語 LoRA checkpoint 目錄，例如 `.../step_0000600`（必填，不進 git） |
| `assets_dir` | 參考音目錄，預設為套件內建的 `echo_tts/assets/vox/` |
| `device` | 預設 `cuda` |
| `optimize` | 預設 `True`（torch.compile） |

各語言設定（生成參數 cfg 2.0、steps 10、不載入 denoiser、streaming 不重試 badcase）：

| 語言 | 模式 | LoRA | 前處理 | 預設語速 |
|---|---|---|---|---|
| zh | ULT：加速參考音 + 逐字稿 | 開 | 台灣讀音（`echo_tts/text/taiwan_pronunciation.py`），`normalize=False` | 1.05 |
| en | REF：EV25b | 關 | 無 | 1.0 |
| ja | REF：JV25b | 關 | 假名化（`echo_tts/text/japanese_kana.py`，pyopenjtalk-plus） | 1.0 |

- `lang=None` 時自動判斷：含假名為 ja，含漢字為 zh，其餘 en。
- `speed` 取最接近的檔位 1.0 / 1.05 / 1.1；zh 三個檔位各有一份參考音（預設 1.05），en / ja 目前只有 1.0 檔，其他速度會落到 1.0。
- 執行緒契約：`optimize=True` 時 torch.compile 的 CUDA graph 綁定執行緒，`prepare()`、`stream()`、`unload()` 必須在同一條執行緒呼叫（Echo Stream 端請用單執行緒 executor）。不同執行緒會立即丟 `RuntimeError`，不會等到 inductor assert；`optimize=False` 不檢查；`unload()` 後重新 `prepare()` 會重設記錄的執行緒。
- 同時只服務一個請求（內部 lock）；消費端在同一執行緒對 generator `close()` 即可釋放。

情緒（目前只有 zh 套用，en / ja 收到 emotion 會忽略並以 debug 記錄一次）：

```python
for chunk in b.stream("太好了！", emotion="happy", intensity=0.5):   # 標籤
    ...
b.stream("太好了！", emotion=[0.6, 0, 0, 0, 0, 0, 0.1, 0])         # 8 維向量：happy, angry, sad, afraid, disgusted, melancholic, surprised, calm
```

- `emotion`：8 維 list / tuple（取最大值）、標籤字串或 `None`；全 0、`None`、`"neutral"`、未知標籤都走中性（行為與未傳時完全一致）。
- `intensity`：0~1 user 刻度（`None` = 0.5），經 `emotion_presets.user_to_strength` 換算成 0.57~0.65，取最接近的片段檔位（0.57 / 0.61 / 0.65）。
- 組合模式：`reference_wav_path` 用 zh 中性參考音（依 speed 檔位）鎖音色，`prompt_wav_path` + `prompt_text` 用情緒片段帶入情緒；有情緒時 LoRA scale 調為 0.85（直接改 `LoRALinear.scaling` buffer，不觸發重編譯），結束後（含例外 / close）還原為 1.0。
- 片段在 `echo_tts/assets/vox/emotion/`，由 `manifest.json` 的 `emotion.clips` 描述（情緒、強度、wav、逐字稿、來源）。目前有 8 種情緒的 0.57 與 0.65 兩檔；0.61 檔待補，缺檔位時自動退回最接近的檔位（強度平手取較強者），整個情緒都沒有片段則走中性，不會拋例外。補片段只需放 wav 並在 manifest 加一筆。
- `prepare()` 會用 zh 情緒路徑暖機一次。

assets 結構（`echo_tts/assets/vox/`）：`manifest.json` 記錄每語言、每檔位的 wav、逐字稿與製作方式。
zh 的 1.05 版本由 1.0 參考音（22.05 kHz）直接以 WSOLA（audiotsm，保音高）加速 1.05 倍，保留原取樣率，不降採樣；1.1 版本由 1.0 參考音（c_t2 seed 9232）重取樣為 16 kHz 單聲道後，以 WSOLA（audiotsm，保音高）加速，逐字稿不變。

模型與 LoRA 不進 git，正式部署位置由使用者決定，透過 config 指定。測試：

- 單元測試：`pytest tests/test_vox_backend.py`
- GPU 整合測試（預設跳過）：設 `VOX_GPU_TEST=1`、`VOX_MODEL_DIR`、`VOX_LORA_DIR` 後執行 `pytest tests/test_vox_backend_gpu.py -s`
