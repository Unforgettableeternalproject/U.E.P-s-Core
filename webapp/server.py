"""通用多候選盲測網站（stdlib only，不需額外依賴）。

用法：
    python webapp/server.py [--host 127.0.0.1] [--port 8848] [--sets-dir DIR]

資料來源 outputs/blind_sets/<set>/manifest.json（音檔在同目錄 audio/），
評分結果寫入 outputs/blind_sets/<set>/reviews/<時間>_<暱稱>.json。

前端只拿得到 cand_id、音檔 URL、text、lang、emotion；
system / params / metrics 永遠不會離開伺服器。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SETS_DIR = os.path.join(PROJECT_ROOT, "outputs", "blind_sets")

MAX_BODY = 64 * 1024            # 單次寫入上限
MAX_COMMENT = 1000
MAX_NICK = 32
MAX_REVIEWS_PER_SET = 500       # 單一 set 的評分檔數上限，避免被灌爆磁碟
RATE_LIMIT = (60, 60.0)         # 每個來源 IP 60 秒內最多 60 次寫入
DIMENSIONS = ("timbre", "naturalness", "emotion", "accent")
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
               ".flac": "audio/flac", ".m4a": "audio/mp4"}

SET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SID_RE = re.compile(r"^[a-f0-9]{16,32}$")
# 暱稱只保留字母數字、底線、連字號與中日文，其餘（含路徑字元）換成底線
SAFE_SLUG = re.compile(r"[^A-Za-z0-9_\-぀-ヿ一-鿿]+")


def slugify(nickname: str) -> str:
    slug = SAFE_SLUG.sub("_", nickname.strip()).strip("._")
    return slug[:MAX_NICK] or "anon"


class BlindStore:
    """manifest 載入、候選對應與評分檔讀寫。"""

    def __init__(self, sets_dir: str) -> None:
        self.sets_dir = os.path.abspath(sets_dir)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._sid_files: dict[tuple[str, str], str] = {}

    # ---- manifest ----
    def list_sets(self) -> list[str]:
        if not os.path.isdir(self.sets_dir):
            return []
        return sorted(
            n for n in os.listdir(self.sets_dir)
            if SET_NAME.match(n)
            and os.path.isfile(os.path.join(self.sets_dir, n, "manifest.json"))
        )

    def manifest(self, name: str) -> dict | None:
        if not SET_NAME.match(name or ""):
            return None
        path = os.path.join(self.sets_dir, name, "manifest.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None
        self._cache[name] = (mtime, data)
        return data

    def public_set(self, name: str) -> dict | None:
        """去識別化的 set 內容；只含 cand_id / 音檔 URL / text / lang / emotion。"""
        data = self.manifest(name)
        if data is None:
            return None
        items = []
        for it in data["items"]:
            iid = str(it.get("item_id", ""))
            if not SAFE_ID.match(iid):
                continue
            cands = []
            for c in it.get("candidates") or []:
                cid = str(c.get("cand_id", ""))
                if SAFE_ID.match(cid) and self.audio_path(name, iid, cid):
                    cands.append({"cand_id": cid,
                                  "url": f"/audio/{name}/{iid}/{cid}"})
            if len(cands) < 2:
                continue
            items.append({"item_id": iid, "text": str(it.get("text", "")),
                          "lang": str(it.get("lang", "")),
                          "emotion": str(it.get("emotion", "")),
                          "candidates": cands})
        return {"set": name, "description": str(data.get("description", "")),
                "items": items}

    def audio_path(self, name: str, item_id: str, cand_id: str) -> str | None:
        """由 manifest 反查音檔；路徑必須落在 <set>/audio/ 內，否則視為不存在。"""
        data = self.manifest(name)
        if data is None:
            return None
        base = os.path.join(self.sets_dir, name)
        audio_root = os.path.realpath(os.path.join(base, "audio"))
        for it in data["items"]:
            if str(it.get("item_id")) != item_id:
                continue
            for c in it.get("candidates") or []:
                if str(c.get("cand_id")) != cand_id:
                    continue
                rel = str(c.get("file") or "")
                if not rel:
                    return None
                # manifest 的 file 可能寫成 "audio/x.wav" 或 "x.wav"
                for cand in (os.path.join(base, rel), os.path.join(audio_root, rel)):
                    real = os.path.realpath(cand)
                    try:
                        inside = os.path.commonpath([real, audio_root]) == audio_root
                    except ValueError:  # 不同磁碟機
                        inside = False
                    if (inside and os.path.isfile(real)
                            and os.path.splitext(real)[1].lower() in AUDIO_TYPES):
                        return real
                return None
        return None

    # ---- 評分檔 ----
    def _review_dir(self, name: str) -> str:
        return os.path.join(self.sets_dir, name, "reviews")

    def _find_file(self, name: str, sid: str) -> str | None:
        key = (name, sid)
        cached = self._sid_files.get(key)
        if cached and os.path.isfile(cached):
            return cached
        rdir = self._review_dir(name)
        if not os.path.isdir(rdir):
            return None
        for f in os.listdir(rdir):
            if not f.endswith(".json"):
                continue
            path = os.path.join(rdir, f)
            try:
                with open(path, encoding="utf-8") as fp:
                    if json.load(fp).get("sid") == sid:
                        self._sid_files[key] = path
                        return path
            except (OSError, ValueError):
                continue
        return None

    def load_progress(self, name: str, sid: str) -> dict:
        with self._lock:
            path = self._find_file(name, sid)
            if not path:
                return {"responses": {}}
            with open(path, encoding="utf-8") as fp:
                data = json.load(fp)
        return {"nickname": data.get("nickname", ""),
                "responses": data.get("responses", {})}

    def save_response(self, payload: dict, user_agent: str) -> tuple[int, dict]:
        name = str(payload.get("set") or "")
        data = self.manifest(name)
        if data is None:
            return 404, {"error": "unknown set"}
        sid = str(payload.get("sid") or "")
        if not SID_RE.match(sid):
            return 400, {"error": "bad sid"}
        item_id = str(payload.get("item_id") or "")
        item = next((i for i in data["items"] if str(i.get("item_id")) == item_id), None)
        if item is None:
            return 400, {"error": "unknown item"}
        cand_ids = [str(c.get("cand_id")) for c in item.get("candidates") or []
                    if self.audio_path(name, item_id, str(c.get("cand_id")))]

        ratings_in = payload.get("ratings")
        if not isinstance(ratings_in, dict) or set(ratings_in) != set(cand_ids):
            return 400, {"error": "ratings must cover every candidate"}
        ratings = {}
        for cid in cand_ids:
            r = ratings_in[cid]
            if not isinstance(r, dict):
                return 400, {"error": "bad rating"}
            row = {}
            for dim in DIMENSIONS:
                v = r.get(dim)
                if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
                    return 400, {"error": f"bad score {dim}"}
                row[dim] = v
            ratings[cid] = row

        best = payload.get("best")
        if best not in cand_ids:
            return 400, {"error": "bad best"}
        order = payload.get("order")
        if not (isinstance(order, list) and sorted(map(str, order)) == sorted(cand_ids)):
            order = cand_ids
        comment = payload.get("comment") or ""
        if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
            return 400, {"error": "bad comment"}
        nickname = str(payload.get("nickname") or "anon").strip()[:MAX_NICK] or "anon"

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        entry = {"order": [str(o) for o in order], "ratings": ratings,
                 "best": best, "comment": comment, "at": now}

        with self._lock:
            path = self._find_file(name, sid)
            if path:
                with open(path, encoding="utf-8") as fp:
                    doc = json.load(fp)
            else:
                rdir = self._review_dir(name)
                os.makedirs(rdir, exist_ok=True)
                if len([f for f in os.listdir(rdir) if f.endswith(".json")]) >= MAX_REVIEWS_PER_SET:
                    return 429, {"error": "too many reviews"}
                stem = f"{time.strftime('%Y%m%d_%H%M%S')}_{slugify(nickname)}"
                path, n = os.path.join(rdir, stem + ".json"), 1
                while os.path.exists(path):
                    n += 1
                    path = os.path.join(rdir, f"{stem}_{n}.json")
                doc = {"set": name, "sid": sid, "started": now, "responses": {}}
                self._sid_files[(name, sid)] = path
            doc["nickname"] = nickname
            doc["updated"] = now
            doc["user_agent"] = user_agent[:200]
            doc["responses"][item_id] = entry
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(doc, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return 200, {"ok": True, "answered": len(doc["responses"]),
                     "file": os.path.basename(path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "UEPBlindTest/2.0"
    store: BlindStore
    _hits: dict
    _hits_lock: threading.Lock

    # ---- 共用 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # ngrok 免費版會插入警告頁，這個 header 可跳過
        self.send_header("ngrok-skip-browser-warning", "1")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def log_message(self, fmt: str, *args) -> None:  # 音檔請求太多，不記
        if "/audio/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    # ---- GET ----
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path, query = unquote(parts.path), parse_qs(parts.query)

        if path in ("/", "/index.html"):
            self._serve_static(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        elif path == "/api/sets":
            out = []
            for name in self.store.list_sets():
                pub = self.store.public_set(name)
                if pub and pub["items"]:
                    out.append({"set": name, "description": pub["description"],
                                "items": len(pub["items"])})
            self._json(200, {"sets": out})
        elif path.startswith("/api/set/"):
            pub = self.store.public_set(path[len("/api/set/"):])
            if pub is None:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, pub)
        elif path == "/api/progress":
            name = (query.get("set") or [""])[0]
            sid = (query.get("sid") or [""])[0]
            if self.store.manifest(name) is None or not SID_RE.match(sid):
                self._json(400, {"error": "bad request"})
            else:
                self._json(200, self.store.load_progress(name, sid))
        elif path.startswith("/audio/"):
            seg = path[len("/audio/"):].split("/")
            if len(seg) != 3 or not all(SAFE_ID.match(s) for s in seg):
                self._json(400, {"error": "bad name"})
                return
            real = self.store.audio_path(*seg)
            if real is None:
                self._json(404, {"error": "not found"})
            else:
                self._serve_audio(real)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    # ---- POST ----
    def _rate_ok(self) -> bool:
        limit, window = RATE_LIMIT
        now = time.monotonic()
        with self._hits_lock:
            dq = self._hits[self.client_address[0]]
            while dq and now - dq[0] > window:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
        return True

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/review":
            self._json(404, {"error": "not found"})
            return
        if not self._rate_ok():
            self._json(429, {"error": "slow down"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413 if length > MAX_BODY else 400, {"error": "bad length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad json"})
            return
        code, body = self.store.save_response(payload, self.headers.get("User-Agent") or "")
        if code == 200:
            print(f"[收到評分] {str(payload.get('nickname', ''))[:32]} -> "
                  f"{payload.get('set')}/{payload.get('item_id')}", flush=True)
        self._json(code, body)

    # ---- 檔案 ----
    def _serve_static(self, full: str, ctype: str) -> None:
        try:
            with open(full, "rb") as fp:
                data = fp.read()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    def _serve_audio(self, full: str) -> None:
        """支援 Range（iOS Safari 播音檔必須）。"""
        ctype = AUDIO_TYPES[os.path.splitext(full)[1].lower()]
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(size - int(m.group(2)), 0)
                if start > end or start >= size:
                    self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("ngrok-skip-browser-warning", "1")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionError):
                    return
                remaining -= len(chunk)


def make_server(sets_dir: str, host: str = "127.0.0.1", port: int = 8848) -> ThreadingHTTPServer:
    """建立 server（不啟動）；測試與 main 共用。"""
    handler = type("BoundHandler", (Handler,), {
        "store": BlindStore(sets_dir), "_hits": defaultdict(deque),
        "_hits_lock": threading.Lock()})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="通用多候選盲測網站")
    parser.add_argument("--host", default="127.0.0.1",
                        help="監聽位址（預設只限本機；區網/對外請明確指定 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--sets-dir", default=DEFAULT_SETS_DIR,
                        help="blind_sets 目錄（預設 outputs/blind_sets）")
    args = parser.parse_args()

    if not os.path.isdir(args.sets_dir):
        raise SystemExit(f"找不到 set 目錄：{args.sets_dir}")
    srv = make_server(args.sets_dir, args.host, args.port)
    sets = srv.RequestHandlerClass.store.list_sets()
    print(f"set 目錄：{args.sets_dir}（{len(sets)} 個 set：{', '.join(sets) or '無'}）")
    print(f"監聽：http://{args.host}:{args.port}  指定 set：/?set=<名稱>")
    srv.serve_forever()


if __name__ == "__main__":
    main()
