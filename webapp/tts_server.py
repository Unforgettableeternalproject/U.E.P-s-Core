"""網頁版 TTS shell — 在瀏覽器裡打指令、聽即時合成的語音。

與 server.py（v25_final 盲測站）並存，兩者互不影響。

用法：
    python webapp/tts_server.py [--port 8850] [--eager]

然後另開終端：
    ngrok http 8850

引擎預設 lazy load（第一次 say 才載入，約 20 秒），加 --eager 可在啟動時
就載入。GPU 一次只能跑一個合成，因此所有請求以 lock 序列化。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# 引擎的 log 含 ⚠ 🎭 等字元，Windows 終端預設 cp950 會 UnicodeEncodeError
# 並讓載入整個失敗（run.bat 是靠 PYTHONIOENCODING=utf-8 迴避）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "tts" / "web"

sys.path.insert(0, str(PROJECT_ROOT))

from echo_tts.cli.interactive import SUPPORTED_LANGUAGES, _detect_language  # noqa: E402
from echo_tts.config.emotion_presets import (  # noqa: E402
    EMOTION_LABELS,
    USER_STRENGTH_DEFAULT,
    get_preset_overrides,
    get_preset_vector,
    load_presets,
    scale_to_user_strength,
    strength_to_user,
    user_to_strength,
)

EMOTION_PRESETS = load_presets()
SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]+\.wav$")

SAMPLE_RATE = 22050  # 對齊 config 的 s2mel.preprocess_params.sr

# 串流 frame 格式：1 byte type + 4 byte big-endian length + payload
FRAME_AUDIO = 0x01   # payload = 完整的 WAV bytes（每段獨立，前端可直接 decode）
FRAME_META = 0x02    # payload = JSON，合成結束時的統計
FRAME_ERROR = 0x03   # payload = JSON，{"error": "..."}


def tensor_to_wav_bytes(tensor, sample_rate: int = SAMPLE_RATE) -> bytes:
    """把引擎吐出的音訊 tensor 轉成獨立可播的 WAV bytes。

    每段都是完整 WAV（含 header），前端就能直接餵給 decodeAudioData，
    不需要自己拼 PCM 或處理格式。
    """
    import io
    import wave

    import numpy as np

    arr = tensor.detach().cpu().float().numpy().squeeze()
    if arr.ndim > 1:
        arr = arr[0]
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_duration(path: Path) -> float:
    import wave
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


class TTSService:
    """包住 TTSInteractiveSession，提供執行緒安全的合成入口。"""

    def __init__(self, device: str = "cuda", use_fp16: bool = True):
        self.device = device
        self.use_fp16 = use_fp16
        self._session = None
        self._engine_loaded = False
        self._lock = threading.Lock()      # 序列化 GPU 合成
        self._load_lock = threading.RLock()
        self.load_error: str | None = None
        self.load_seconds: float | None = None

    # ── 生命週期 ──

    @property
    def loaded(self) -> bool:
        return self._engine_loaded

    def ensure_session(self):
        """建立 session 物件但不載入模型。

        emotion / language 這類純設定不需要 GPU，先分出來避免使用者只是
        調個語言就等 20 秒。
        """
        if self._session is not None:
            return self._session
        with self._load_lock:
            if self._session is None:
                from echo_tts.runtime.audio import NullAudioPlayer
                from echo_tts.runtime.indextts_backend import IndexTTS2Backend
                from echo_tts.runtime.indextts_generate import default_generation_paths
                from echo_tts.runtime.session import TTSInteractiveSession

                backend = IndexTTS2Backend(
                    paths=default_generation_paths(PROJECT_ROOT),
                    device=self.device,
                    use_fp16=self.use_fp16,
                )
                self._session = TTSInteractiveSession(
                    backend=backend,
                    output_dir=OUTPUT_DIR,
                    player=NullAudioPlayer(),   # 語音回瀏覽器，不走本機喇叭
                )
        return self._session

    def ensure_loaded(self) -> None:
        """真正把模型載進 GPU（約 20 秒）。"""
        session = self.ensure_session()
        if self._engine_loaded:
            return
        with self._load_lock:
            if self._engine_loaded:
                return
            t0 = time.perf_counter()
            session.load()
            self.load_seconds = time.perf_counter() - t0
            self._engine_loaded = True

    # ── 狀態 ──

    def state(self) -> dict:
        if self._session is None:
            return {
                "loaded": False,
                "language": "auto",
                "emotion": [0.0] * 8,
                "strength": USER_STRENGTH_DEFAULT,
                "speed": 0.0,
                "presets": list(EMOTION_PRESETS.keys()),
                "languages": SUPPORTED_LANGUAGES,
                "load_error": self.load_error,
            }
        s = self._session
        return {
            "loaded": self._engine_loaded,
            "language": getattr(s, "_language", "auto"),
            "emotion": s.get_emotion(),
            "strength": strength_to_user(s.get_emotion_strength()),
            "actual_strength": s.get_emotion_strength(),
            "speed": s.get_speed(),
            "presets": list(EMOTION_PRESETS.keys()),
            "languages": SUPPORTED_LANGUAGES,
            "load_seconds": self.load_seconds,
            "load_error": self.load_error,
        }

    # ── 指令 ──

    def set_emotion(self, name: str | None, values: list[float] | None,
                    user_strength: float) -> dict:
        self.ensure_session()   # 純設定，不需要載模型
        if name is not None:
            entry = EMOTION_PRESETS[name]
            vec = scale_to_user_strength(list(get_preset_vector(entry)), user_strength)
            self._session.set_emotion_overrides(get_preset_overrides(entry))
        else:
            vec = scale_to_user_strength(list(values or []), user_strength)
            self._session.set_emotion_overrides(None)
        self._session.set_emotion(vec)
        self._session.set_emotion_strength(user_to_strength(user_strength))
        return {
            "emotion": vec,
            "strength": max(0.0, min(1.0, user_strength)),
            "actual_strength": user_to_strength(user_strength),
        }

    def set_language(self, lang: str) -> None:
        self.ensure_session().set_language(lang)   # 純設定，不需要載模型

    def set_speed(self, value: float) -> float:
        # session.set_speed 內部會 self.load()，語速要動到引擎狀態
        self.ensure_loaded()
        return self._session.set_speed(value)

    def say_stream(self, text: str, language: str | None, emit) -> dict:
        """串流合成：每段音訊一產出就立刻透過 emit 送走。

        走 backend.generate 的 on_segment_audio callback（與本機 shell 的
        StreamingAudioPlayer 同一條路徑），因此首段出聲時間 ≈ 首段合成時間，
        不必等整句跑完。完整檔案仍會寫出，供前端重播。

        Args:
            emit: callable(frame_type: int, payload: bytes) — 由 handler 提供，
                  負責寫進 HTTP response。
        """
        self.ensure_loaded()
        lang = self._resolve_language(text, language)

        with self._lock:
            session = self._session
            prev = getattr(session, "_language", None)
            session.set_language(lang)
            output_path = session._resolve_auto_output_path(prefix="say")
            output_path.parent.mkdir(parents=True, exist_ok=True)

            t0 = time.perf_counter()
            first_audio_ms: float | None = None
            seg_count = 0

            def _on_segment(audio_tensor, seg_idx: int, total: int):
                nonlocal first_audio_ms, seg_count
                if first_audio_ms is None:
                    first_audio_ms = (time.perf_counter() - t0) * 1000
                seg_count += 1
                emit(FRAME_AUDIO, tensor_to_wav_bytes(audio_tensor))

            try:
                session.backend.generate(
                    text, output_path,
                    on_segment_audio=_on_segment,
                    language=lang,
                    emotion_overrides=session.get_emotion_overrides(),
                )
            finally:
                if prev is not None:
                    session.set_language(prev)

            elapsed = time.perf_counter() - t0

        duration = wav_duration(output_path)
        return {
            "file": output_path.name,
            "language": lang,
            "segments": seg_count,
            "first_audio_ms": round(first_audio_ms) if first_audio_ms else None,
            "elapsed_ms": round(elapsed * 1000),
            "audio_seconds": round(duration, 2),
            "rtf": round(elapsed / duration, 3) if duration > 0 else None,
        }

    def _resolve_language(self, text: str, language: str | None) -> str:
        lang = language or getattr(self._session, "_language", "auto")
        if lang == "auto":
            lang = _detect_language(text)
            if lang not in SUPPORTED_LANGUAGES:
                lang = "en"
        return lang

    def say(self, text: str, language: str | None = None) -> dict:
        """合成一句話，回傳音檔名與耗時。GPU 一次只服務一個請求。"""
        self.ensure_loaded()
        lang = language or getattr(self._session, "_language", "auto")
        if lang == "auto":
            lang = _detect_language(text)
            if lang not in SUPPORTED_LANGUAGES:
                lang = "en"

        with self._lock:
            prev = getattr(self._session, "_language", None)
            self._session.set_language(lang)
            t0 = time.perf_counter()
            try:
                result = self._session.save_auto(text)
            finally:
                if prev is not None:
                    self._session.set_language(prev)
            elapsed = time.perf_counter() - t0

        path = Path(result.output_path)
        try:
            import wave
            with wave.open(str(path)) as w:
                duration = w.getnframes() / w.getframerate()
        except Exception:
            duration = 0.0

        return {
            "file": path.name,
            "language": lang,
            "elapsed_ms": round(elapsed * 1000),
            "audio_seconds": round(duration, 2),
            "rtf": round(elapsed / duration, 3) if duration > 0 else None,
        }


SERVICE: TTSService | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "UEPTTSWeb/1.0"

    # ── 共用 ──

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ngrok-skip-browser-warning", "1")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt: str, *args) -> None:
        if args and "/audio/" in str(args[0]):
            return
        super().log_message(fmt, *args)

    # ── 路由 ──

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path in ("/", "/index.html"):
            self._serve_file(WEB_DIR / "tts.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/state":
            self._json(200, SERVICE.state())
            return
        if path.startswith("/audio/"):
            name = path[len("/audio/"):]
            if not SAFE_NAME.match(name):
                self._json(400, {"error": "bad name"})
                return
            self._serve_file(OUTPUT_DIR / name, "audio/wav")
            return
        self._json(404, {"error": "not found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._body()

        try:
            if path == "/api/load":
                SERVICE.ensure_loaded()
                self._json(200, SERVICE.state())
                return

            if path == "/api/say/stream":
                self._handle_say_stream(body)
                return

            if path == "/api/say":
                text = (body.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "text 不可為空"})
                    return
                if len(text) > 2000:
                    self._json(400, {"error": "text 過長（上限 2000 字）"})
                    return
                self._json(200, SERVICE.say(text, body.get("language")))
                return

            if path == "/api/emotion":
                name = body.get("preset")
                values = body.get("values")
                strength = float(body.get("strength", USER_STRENGTH_DEFAULT))
                if name is not None:
                    name = str(name).lower()
                    if name not in EMOTION_PRESETS:
                        self._json(400, {"error": f"未知的 preset '{name}'"})
                        return
                elif not (isinstance(values, list) and len(values) == 8):
                    self._json(400, {"error": "需要 preset 名稱或 8 維向量"})
                    return
                self._json(200, SERVICE.set_emotion(name, values, strength))
                return

            if path == "/api/language":
                lang = str(body.get("language") or "auto")
                if lang != "auto" and lang not in SUPPORTED_LANGUAGES:
                    self._json(400, {"error": f"不支援的語言 '{lang}'"})
                    return
                SERVICE.set_language(lang)
                self._json(200, {"language": lang})
                return

            if path == "/api/speed":
                value = max(-1.0, min(1.0, float(body.get("speed", 0.0))))
                self._json(200, {"speed": SERVICE.set_speed(value)})
                return

            self._json(404, {"error": "not found"})

        except Exception as exc:  # 合成失敗要讓前端看得到原因
            traceback.print_exc()
            SERVICE.load_error = str(exc)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_say_stream(self, body: dict) -> None:
        """串流合成：邊生成邊把每段 WAV 推給前端。

        不設 Content-Length、以關閉連線標示結束（HTTP/1.0 合法的 streaming
        做法），前端 fetch 的 ReadableStream 會逐段收到。
        """
        text = (body.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "text 不可為空"})
            return
        if len(text) > 2000:
            self._json(400, {"error": "text 過長（上限 2000 字）"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")   # 避免中間層緩衝
        self.send_header("ngrok-skip-browser-warning", "1")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(frame_type: int, payload: bytes) -> None:
            self.wfile.write(bytes([frame_type]) + len(payload).to_bytes(4, "big"))
            self.wfile.write(payload)
            self.wfile.flush()   # 關鍵：不 flush 就不是串流

        try:
            meta = SERVICE.say_stream(text, body.get("language"), emit)
            emit(FRAME_META, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass   # 使用者中途關掉分頁
        except Exception as exc:
            traceback.print_exc()
            try:
                emit(FRAME_ERROR, json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
                ).encode("utf-8"))
            except Exception:
                pass

    def _serve_file(self, full: Path, ctype: str) -> None:
        if not full.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, full.read_bytes(), ctype)


def main() -> None:
    global SERVICE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8850)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument("--eager", action="store_true",
                        help="啟動時就載入引擎（預設是第一次 say 才載）")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE = TTSService(device=args.device, use_fp16=not args.no_fp16)

    print(f"輸出目錄：{OUTPUT_DIR}")
    print(f"本機開啟：http://127.0.0.1:{args.port}")
    print(f"對外轉發：ngrok http {args.port}")

    if args.eager:
        print(">> 載入引擎中……", flush=True)
        SERVICE.ensure_loaded()
        print(f">> 引擎就緒（{SERVICE.load_seconds:.1f}s）", flush=True)
    else:
        print(">> 引擎 lazy load：第一次 say 會等約 20 秒", flush=True)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
