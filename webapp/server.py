"""v25_final 盲測比對網站（stdlib only，不需額外依賴）。

用法：
    python webapp/server.py [--port 8848]

然後另開終端：
    ngrok http 8848

音檔來源 outputs/v25_final/，評測結果寫入 outputs/v25_final_reviews/。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_final")
REVIEW_DIR = os.path.join(PROJECT_ROOT, "outputs", "v25_final_reviews")

SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+\.wav$")
SAFE_SLUG = re.compile(r"[^A-Za-z0-9_一-鿿-]+")


class Handler(BaseHTTPRequestHandler):
    server_version = "UEPBlindTest/1.0"

    # ---- 共用 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # ngrok 免費版會插入警告頁，這個 header 可跳過
        self.send_header("ngrok-skip-browser-warning", "1")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:  # 精簡日誌
        if "/audio/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"),
                             "text/html; charset=utf-8")
            return

        if path.startswith("/audio/"):
            name = path[len("/audio/"):]
            if not SAFE_NAME.match(name):
                self._json(400, {"error": "bad name"})
                return
            self._serve_file(os.path.join(AUDIO_DIR, name), "audio/wav")
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if path == "/api/manifest":
            self._json(200, {"files": sorted(
                f for f in os.listdir(AUDIO_DIR) if f.endswith(".wav")
            )})
            return

        self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/submit":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            self._json(400, {"error": "bad length"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "bad json"})
            return

        os.makedirs(REVIEW_DIR, exist_ok=True)
        nickname = str(payload.get("nickname") or "anon")[:32]
        slug = SAFE_SLUG.sub("_", nickname) or "anon"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        payload["_received_at"] = stamp
        payload["_remote"] = self.client_address[0]

        fname = f"{stamp}_{slug}.json"
        with open(os.path.join(REVIEW_DIR, fname), "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)

        print(f"[收到評測] {nickname} -> outputs/v25_final_reviews/{fname}", flush=True)
        self._json(200, {"ok": True, "saved": fname})

    # ---- 靜態檔 ----
    def _serve_file(self, full: str, ctype: str) -> None:
        if not os.path.isfile(full):
            self._json(404, {"error": "not found"})
            return
        with open(full, "rb") as fp:
            data = fp.read()
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    if not os.path.isdir(AUDIO_DIR):
        raise SystemExit(f"找不到音檔目錄：{AUDIO_DIR}")

    count = len([f for f in os.listdir(AUDIO_DIR) if f.endswith(".wav")])
    print(f"音檔目錄：{AUDIO_DIR}（{count} 檔）")
    print(f"評測輸出：{REVIEW_DIR}")
    print(f"本機開啟：http://127.0.0.1:{args.port}")
    print(f"對外轉發：ngrok http {args.port}")

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
