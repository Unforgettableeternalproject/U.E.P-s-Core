# 本機 SearXNG（U.E.P web.search 用）

只綁定 `127.0.0.1:8888`，供 Core 的 `web.search` 優先使用。

## 啟動

1. 建立 `deploy/searxng/.env`（已被 gitignore）：
   ```
   SEARXNG_SECRET=<隨機長字串，例如 openssl rand -hex 32>
   ```
2. `docker compose up -d`（在本目錄執行）
3. 驗證：`curl "http://127.0.0.1:8888/search?q=test&format=json"`

## 停止

`docker compose down`

## Core 設定（根目錄 `.env`）

```
UEP_WEB_SEARCH_PROVIDERS=searxng,wikipedia,duckduckgo
UEP_WEB_SEARXNG_URL=http://127.0.0.1:8888
UEP_WEB_SEARXNG_PRIVATE_HOSTS=127.0.0.1
```
