# modules/stt_module/stt_module.py（繼續保留原有 handle() 方法）

import threading
import queue
import speech_recognition as sr
from core.module_base import BaseModule

class STTModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or {}
        self.recognizer = sr.Recognizer()
        self.mic = None
        self.device_index = self.config.get("device_index", 1)
        self.phrase_time_limit = self.config.get("phrase_time_limit", -1)  # 語音片段的最大長度
        self.auto_adjust_noise = self.config.get("auto_adjust_noise", True)
        self.debug = self.config.get("debug_mode", False)
        self._running = False
        self._thread = None
        self._queue = queue.Queue()
        self._callback = None

    def initialize(self):
        print("[STT] 初始化中...")
        self.mic = sr.Microphone(device_index=self.device_index)
        if self.auto_adjust_noise:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1)
        print("[STT] 已完成環境噪音校正")

    def handle(self, data: dict = {}) -> dict:
        """單次語音轉文字"""
        try:
            with self.mic as source:
                print("[STT] Listening...")
                audio = self.recognizer.listen(source)
            print("[STT] Transcribing...")
            text = self.recognizer.recognize_google(audio)
            return {"text": text}
        except sr.UnknownValueError:
            return {"text": "", "error": "unrecognized_audio"}
        except sr.RequestError as e:
            return {"text": "", "error": str(e)}

    def _realtime_loop(self):
        print("[STT] Real-time 模式啟動")
        while self._running:
            try:
                with self.mic as source:
                    print("[STT] 🎙 Listening...")
                    if self.phrase_time_limit > 0:
                        audio = self.recognizer.listen(source, phrase_time_limit=self.phrase_time_limit)
                    else:
                        audio = self.recognizer.listen(source)
                text = self.recognizer.recognize_google(audio)
                print("[STT] ✅ Real-time result:", text)
                if self._callback:
                    self._callback(text)
            except sr.UnknownValueError:
                print("[STT] 無法辨識語音")
            except sr.RequestError as e:
                print(f"[STT] API 錯誤: {e}")
            except Exception as e:
                print(f"[STT] 錯誤: {e}")
                break

    def start_realtime(self, on_result=None):
        if self._running:
            print("[STT] Real-time 已在執行中")
            return
        self._callback = on_result
        self._running = True
        self._thread = threading.Thread(target=self._realtime_loop, daemon=True)
        self._thread.start()

    def stop_realtime(self):
        self._running = False
        if self._thread:
            self._thread.join()
        print("[STT] Real-time 已停止")

    def shutdown(self):
        self.stop_realtime()
        print("[STT] 模組已關閉")
