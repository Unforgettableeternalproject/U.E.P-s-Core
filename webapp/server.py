"""A/B 二選一盲測網站（stdlib only，不需額外依賴）。

用法：
    python webapp/server.py [--host 127.0.0.1] [--port 8848] [--sets-dir DIR]

資料來源 outputs/blind_sets/<set>/manifest.json（音檔在同目錄 audio/）。
item 剛好 2 個候選即一題；超過 2 個則展開成兩兩配對，或依 item 的
可選欄位 `pairs: [[cand_id, cand_id], ...]` 指定。
作答（按下即存）寫入 outputs/blind_sets/<set>/reviews/<時間>_<暱稱>.json。

前端只拿得到 pair、cand_id、音檔 URL、text、lang、emotion；
system / params / metrics 永遠不會離開伺服器。
"""
from __future__ import annotations

import argparse
import json
import os
import random
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
MAX_ELAPSED_MS = 3_600_000     # 單題停留時間上限（1 小時）
MAX_NICK = 32
MAX_REVIEWS_PER_SET = 500       # 單一 set 的評分檔數上限，避免被灌爆磁碟
RATE_LIMIT = (60, 60.0)         # 每個來源 IP 60 秒內最多 60 次寫入
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

    def pairs(self, name: str) -> list[dict] | None:
        """展開成 A/B 題目（正規順序，不含隨機）。

        item 剛好 2 個有效候選即一題；超過 2 個則依 item 的可選欄位
        `pairs: [[cand_id, cand_id], ...]` 指定配對，沒有就兩兩展開。
        """
        data = self.manifest(name)
        if data is None:
            return None
        out: list[dict] = []
        for it in data["items"]:
            iid = str(it.get("item_id", ""))
            if not SAFE_ID.match(iid):
                continue
            valid: list[str] = []
            for c in it.get("candidates") or []:
                cid = str(c.get("cand_id", ""))
                if SAFE_ID.match(cid) and cid not in valid and self.audio_path(name, iid, cid):
                    valid.append(cid)
            if len(valid) < 2:
                continue
            declared = it.get("pairs")
            combos: list[tuple[str, str]] = []
            if len(valid) > 2 and isinstance(declared, list):
                for p in declared:
                    if (isinstance(p, (list, tuple)) and len(p) == 2
                            and all(str(x) in valid for x in p) and str(p[0]) != str(p[1])):
                        combos.append((str(p[0]), str(p[1])))
            else:
                combos = [(valid[i], valid[j]) for i in range(len(valid))
                          for j in range(i + 1, len(valid))]
            seen: set[frozenset] = set()
            for a, b in combos:
                key = frozenset((a, b))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"pair": f"{iid}:{a}:{b}", "item_id": iid,
                            "text": str(it.get("text", "")),
                            "lang": str(it.get("lang", "")),
                            "emotion": str(it.get("emotion", "")),
                            "cands": [a, b]})
        return out

    def quiz(self, name: str, sid: str) -> dict | None:
        """該受測者專屬題目：題序與 A/B 位置由 (set, sid) 決定性亂數產生，續答時不變。

        回應只含 pair / 文本 / 兩個音檔 URL；system、params、metrics 不外流。
        """
        pairs = self.pairs(name)
        if pairs is None:
            return None
        data = self.manifest(name) or {}
        rng = random.Random(f"{name}|{sid}")
        order = list(pairs)
        rng.shuffle(order)
        questions = []
        for p in order:
            a, b = p["cands"]
            if rng.random() < 0.5:
                a, b = b, a
            questions.append({
                "pair": p["pair"], "text": p["text"], "lang": p["lang"],
                "emotion": p["emotion"],
                "a": {"cand_id": a, "url": f"/audio/{name}/{p['item_id']}/{a}"},
                "b": {"cand_id": b, "url": f"/audio/{name}/{p['item_id']}/{b}"}})
        done = self.load_progress(name, sid).get("choices", {})
        return {"set": name, "description": str(data.get("description", "")),
                "total": len(questions), "done": sorted(done), "questions": questions}

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
                return {"choices": {}}
            try:
                with open(path, encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, ValueError):
                return {"choices": {}}
        choices = data.get("choices")
        return {"nickname": data.get("nickname", ""),
                "choices": choices if isinstance(choices, dict) else {}}

    def save_choice(self, payload: dict, user_agent: str) -> tuple[int, dict]:
        """按下即存：每題一筆 {pair, chosen, shown_order, elapsed_ms}。"""
        name = str(payload.get("set") or "")
        if self.manifest(name) is None:
            return 404, {"error": "unknown set"}
        sid = str(payload.get("sid") or "")
        if not SID_RE.match(sid):
            return 400, {"error": "bad sid"}
        pair_id = str(payload.get("pair") or "")
        meta = next((p for p in self.pairs(name) or [] if p["pair"] == pair_id), None)
        if meta is None:
            return 400, {"error": "unknown pair"}
        chosen = payload.get("chosen")
        if chosen not in meta["cands"]:
            return 400, {"error": "bad chosen"}
        shown = payload.get("shown_order")
        if not (isinstance(shown, list) and sorted(map(str, shown)) == sorted(meta["cands"])):
            return 400, {"error": "bad shown_order"}
        elapsed = payload.get("elapsed_ms", 0)
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
            return 400, {"error": "bad elapsed_ms"}
        elapsed = int(min(elapsed, MAX_ELAPSED_MS))
        nickname = str(payload.get("nickname") or "anon").strip()[:MAX_NICK] or "anon"

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        entry = {"pair": pair_id, "chosen": chosen,
                 "shown_order": [str(x) for x in shown], "elapsed_ms": elapsed, "at": now}

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
                doc = {"set": name, "sid": sid, "started": now}
                self._sid_files[(name, sid)] = path
            if not isinstance(doc.get("choices"), dict):
                doc["choices"] = {}
            doc["nickname"] = nickname
            doc["updated"] = now
            doc["user_agent"] = user_agent[:200]
            doc["choices"][pair_id] = entry
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(doc, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return 200, {"ok": True, "answered": len(doc["choices"]),
                     "file": os.path.basename(path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "UEPBlindTest/3.0"
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
                pairs = self.store.pairs(name)
                if pairs:
                    desc = (self.store.manifest(name) or {}).get("description", "")
                    out.append({"set": name, "description": str(desc), "pairs": len(pairs)})
            self._json(200, {"sets": out})
        elif path == "/api/quiz":
            name = (query.get("set") or [""])[0]
            sid = (query.get("sid") or [""])[0]
            if not SID_RE.match(sid):
                self._json(400, {"error": "bad request"})
                return
            quiz = self.store.quiz(name, sid) if SET_NAME.match(name) else None
            if quiz is None:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, quiz)
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
        code, body = self.store.save_choice(payload, self.headers.get("User-Agent") or "")
        if code == 200:
            print(f"[收到作答] {str(payload.get('nickname', ''))[:32]} -> "
                  f"{payload.get('set')}/{payload.get('pair')}", flush=True)
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
    parser = argparse.ArgumentParser(description="A/B 二選一盲測網站")
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
