# Echo Memory MCP Server

將 Echo Memory 四層記憶系統包裝為標準 MCP (Model Context Protocol) Server，
讓 Claude Desktop 和其他 MCP 客戶端能透過工具和資源存取記憶功能。

---

## 執行環境

本 repo 不建立獨立虛擬環境。MCP SDK 與其他依賴都安裝在
`U.E.P-s-Core\env`；若主環境尚未安裝 MCP，再由主專案維護者安裝。

---

## 啟動方式

### 方式一：模組執行（開發用）

```powershell
# 在專案根目錄執行
$env:PYTHONIOENCODING = "utf-8"
..\U.E.P-s-Core\env\Scripts\python.exe -m mcp_server
```

> **注意**：MCP Server 使用 stdio 通訊，直接執行時不會有可見的輸出。
> 請透過 Claude Desktop 或 MCP 客戶端連接使用。

---

## Claude Desktop 配置範例

在 Claude Desktop 的配置檔案中加入以下設定：

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "echo-memory": {
      "command": "<UEP_CORE_ENV>/Scripts/python.exe",
      "args": [
        "-m",
        "mcp_server"
      ],
      "cwd": "<REPO_ROOT>/TestSeperateMemorySystem",
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": "<REPO_ROOT>/TestSeperateMemorySystem"
      }
    }
  }
}
```

> 將 `<UEP_CORE_ENV>` 替換為 `U.E.P-s-Core/env` 的絕對路徑，`<REPO_ROOT>` 替換為你的 repo 根目錄。

---

## 可用工具（Tools）

| 工具名稱 | 說明 | 必填參數 |
|---------|------|---------|
| `store_episode` | 存儲對話情節 | `user_input`, `agent_response` |
| `retrieve_memories` | 檢索相關記憶，返回格式化文字 | `query` |
| `search_concepts` | 搜尋語義概念 | `query` |
| `get_memory_stats` | 取得系統統計資訊 | （無） |
| `trigger_dream` | 觸發 Dream Engine 記憶鞏固 | （無） |
| `list_episodes` | 列出情節記憶 | （無） |
| `store_observation` | 存儲重要觀察（高顯著性） | `content` |

所有工具都接受可選的 `namespace_id`。未提供時使用 `MEMORY_NAMESPACE_ID`（預設 `local-default`）。

### 工具參數詳情

#### store_episode
```json
{
  "user_input": "用戶輸入的文字",
  "agent_response": "Agent 的回應",
  "session_id": "會話ID（可選，預設 mcp-default）",
  "salience": 0.5,
  "namespace_id": "team-alpha"
}
```

#### retrieve_memories
```json
{
  "query": "查詢文字",
  "top_k": 5,
  "min_similarity": 0.35,
  "dopamine": 0.5,
  "serotonin": 0.5
}
```

#### search_concepts
```json
{
  "query": "搜尋關鍵字",
  "top_k": 5
}
```

#### trigger_dream
```json
{
  "triggered_by": "mcp"
}
```
> 注意：有 60 秒冷卻時間，防止頻繁觸發。執行超時設定為 30 秒。

#### list_episodes
```json
{
  "session_id": "過濾特定 session（可選）",
  "limit": 10,
  "sort_by": "timestamp"
}
```

#### store_observation
```json
{
  "content": "重要觀察或事實的內容",
  "importance": 0.85
}
```

---

## 可用資源（Resources）

| URI | 說明 | MIME Type |
|-----|------|-----------|
| `memory://stats` | 記憶系統統計 JSON | `application/json` |
| `memory://episodes/recent` | 最近 10 個情節 | `application/json` |
| `memory://concepts` | 所有概念列表 | `application/json` |
| `memory://graph/summary` | 記憶圖結構摘要 | `application/json` |

Resources 固定讀取 Server 設定的預設 namespace；需要指定 namespace 時請使用 Tools。

### 資源回應範例

#### memory://stats
```json
{
  "memory_layers": {
    "episodes": 42,
    "concepts": 15,
    "working_cache": 2
  },
  "graph": {
    "nodes": 45,
    "edges": 67,
    "avg_degree": 2.9778,
    "avg_edge_weight": 0.4231
  },
  "generated_at": "2026-06-12T10:30:00+00:00"
}
```

---

## 環境變數

MCP Server 從 `.env` 檔案（專案根目錄 `TestSeperateMemorySystem/.env`）載入配置：

| 變數 | 說明 | 預設值 |
|-----|------|--------|
| `MEMORY_STORAGE_PATH` | 記憶資料存儲路徑 | `./data` |
| `MEMORY_NAMESPACE_ID` | MCP Tools 未指定 namespace 時使用的預設隔離空間 | `local-default` |
| `EMBEDDING_MODEL` | Ollama 嵌入模型名稱 | `bge-m3` |
| `EMBEDDING_BACKEND` | 嵌入後端 | `ollama` |
| `EMBEDDING_QUERY_PREFIX` | bge-m3 查詢前綴 | 空字串 |
| `OLLAMA_BASE_URL` | Ollama API 位址 | `http://localhost:11434` |
| `EMBEDDING_DEVICE` | 運行設備 | `cpu` |
| `GOOGLE_API_KEY` | Google API 金鑰（Dream Engine 知識蒸餾需要） | — |

同一 namespace 的存儲與 Dream 寫入會以跨程序鎖序列化。這個後端適合單機多 client 的正確性需求，不是高吞吐分散式儲存。

---

## 架構說明

```
MCP Client (Claude Desktop / U.E.P agent)
    │
    │ stdio (MCP Protocol)
    ▼
mcp_server/
├── server.py      # 主入口，初始化 Server + MemoryEngine
├── handlers.py    # 7 個 MCP Tool handlers
├── resources.py   # 4 個 MCP Resource handlers
└── __main__.py    # python -m mcp_server 入口
    │
    │ Python API (sync → asyncio.to_thread)
    ▼
echo_memory/
├── engine.py      # MemoryEngine 主類
├── storage/       # 記憶存儲（Episodic/Semantic/Working）
├── retrieval/     # EcphoryRAG 檢索
└── dream/         # Dream Engine
```

---

## 注意事項

- MCP Server 使用 **stdio** 通訊，所有日誌輸出到 `stderr`，不影響 MCP 協議
- MemoryEngine 的 API 是同步的，透過 `asyncio.to_thread()` 包裝為 async
- `trigger_dream` 有 **60 秒冷卻時間**，且執行超時設定為 30 秒
- 啟動前請確認 Ollama 正在執行，且 `ollama list` 可看到 `bge-m3`
- Dream Engine 的知識蒸餾功能需要設定 LLM API 金鑰（`GOOGLE_API_KEY`）
- `search_concepts` 使用 SemanticMemory 的向量搜索；不得以關鍵字掃描私有儲存欄位作為替代路徑
