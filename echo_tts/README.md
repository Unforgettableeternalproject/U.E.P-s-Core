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
for chunk in b.stream("你好", lang=None, speed=None):   # 1-D float32，48 kHz
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
| zh | ULT：加速參考音 + 逐字稿 | 開 | 台灣讀音（`echo_tts/text/taiwan_pronunciation.py`），`normalize=False` | 1.2 |
| en | REF：EV25b | 關 | 無 | 1.0 |
| ja | REF：JV25b | 關 | 假名化（`echo_tts/text/japanese_kana.py`，pyopenjtalk-plus） | 1.0 |

- `lang=None` 時自動判斷：含假名為 ja，含漢字為 zh，其餘 en。
- `speed` 取最接近的檔位 1.0 / 1.1 / 1.2；zh 三個檔位各有一份加速參考音，en / ja 目前只有 1.0 檔，其他速度會落到 1.0。
- 同時只服務一個請求（內部 lock）；消費端在同一執行緒對 generator `close()` 即可釋放。

assets 結構（`echo_tts/assets/vox/`）：`manifest.json` 記錄每語言、每檔位的 wav、逐字稿與製作方式。
zh 的 1.1 / 1.2 版本由 `neutral.wav` 重取樣為 16 kHz 單聲道後，以 WSOLA（audiotsm，保音高）加速，逐字稿不變。

模型與 LoRA 不進 git，正式部署位置由使用者決定，透過 config 指定。測試：

- 單元測試：`pytest tests/test_vox_backend.py`
- GPU 整合測試（預設跳過）：設 `VOX_GPU_TEST=1`、`VOX_MODEL_DIR`、`VOX_LORA_DIR` 後執行 `pytest tests/test_vox_backend_gpu.py -s`
