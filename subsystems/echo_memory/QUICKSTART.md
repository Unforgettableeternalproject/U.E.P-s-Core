# Echo Memory System - 快速開始

Echo Memory 刻意不維護自己的 `venv` 或 `.venv`。所有 Python 依賴由同層主專案
`~/.uep/venv-inference-312` 統一提供（由 U.E.P-s-Core 的 uv.lock 管理）；正式 embedding 後端為 Ollama `bge-m3`。

## 1. 確認共用環境

```powershell
cd C:\Users\Bernie\source\repos\Unforgettableeternalproject\TestSeperateMemorySystem
$UEP_PYTHON = "$env:USERPROFILE\.uep\venv-inference-312\Scripts\python.exe"

& $UEP_PYTHON --version
& $UEP_PYTHON -m pip check
& $UEP_PYTHON -c "from echo_memory import MemoryEngine; import mcp; print('runtime OK')"
```

不要在本 repo 建立獨立環境或直接執行未指定路徑的 `pip install`。若缺少依賴，應在
U.E.P Core 主環境統一處理。

## 2. 確認 Ollama

```powershell
ollama list
```

輸出必須包含 `bge-m3`。正式設定如下：

```dotenv
EMBEDDING_BACKEND=ollama
EMBEDDING_MODEL=bge-m3
EMBEDDING_QUERY_PREFIX=
OLLAMA_BASE_URL=http://localhost:11434
RETRIEVAL_MIN_SIMILARITY=0.35
```

`bge-m3` 是 1024 維多語言 embedding，不需要 query prefix。切換模型或向量維度時，
系統會根據 `embedding_index.json` 重建索引；不要混用舊模型產生的向量。

## 3. 建立設定

若 `.env` 尚不存在：

```powershell
Copy-Item .env.example .env
notepad .env
```

至少配置一個 LLM provider；embedding 則維持上方的 Ollama 設定。

```powershell
& $UEP_PYTHON verify_config.py
```

## 4. 驗證

```powershell
& $UEP_PYTHON -m pytest -q
& $UEP_PYTHON -m ruff check . --select E,F --ignore E501,E402
& $UEP_PYTHON -m compileall -q echo_memory llm_integration mcp_server Entry.py
```

## 5. 啟動 CLI

```powershell
.\run.bat
```

`run.bat` 會固定使用 `%USERPROFILE%\.uep\venv-inference-312\Scripts\python.exe`。

## 6. 啟動 MCP Server

```powershell
$env:PYTHONIOENCODING = "utf-8"
& $UEP_PYTHON -m mcp_server
```

MCP 使用 stdio 通訊，直接啟動時沒有互動式畫面是正常行為。客戶端設定請參考
`mcp_server/README.md`。

## 常見問題

- `U.E.P Core Python not found`：確認共用環境存在於
  `%USERPROFILE%\.uep\venv-inference-312\Scripts\python.exe`。
- `Ollama embedding failed`：確認 Ollama 服務已啟動、`OLLAMA_BASE_URL` 正確，並且
  `ollama list` 包含 `bge-m3`。
- 模型切換後召回異常：確認 query prefix 與模型匹配，並讓 manifest 機制完成索引重建。
- MCP import 失敗：依賴必須補到 U.E.P Core 共用環境，不要在本 repo 建立替代環境。
