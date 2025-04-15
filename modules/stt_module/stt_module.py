# modules/stt_module/stt_module.py

import threading
import queue
import speech_recognition as sr
from core.module_base import BaseModule
from utils.debug_helper import debug_log, info_log, error_log
from .schemas import STTInput, STTOutput

class STTModule(BaseModule):
    def __init__(self, config=None):
        self.config = config or {}
        self.recognizer = sr.Recognizer()
        self.mic = None
        self.device_index = self.config.get("device_index", 1)
        self.phrase_time_limit = self.config.get("phrase_time_limit", -1)  # 語音片段的最大長度
        self.auto_adjust_noise = self.config.get("auto_adjust_noise", True)
        self._running = False
        self._thread = None
        self._queue = queue.Queue()
        self._callback = None

    def debug(self):
        # Debug level = 1
        debug_log(1, "[STT] Debug 模式啟用")

        # Debug level = 2
        debug_log(2, f"[STT] 模組設定: {self.config}")


    def initialize(self):
        debug_log(1, "[STT] 初始化中...")
        self.debug()

        try:
            self.mic = sr.Microphone(device_index=self.device_index)
            info_log("[STT] 麥克風初始化成功")
        except OSError as e:
            error_log(f"[STT] 麥克風初始化失敗：{e}")
            print("[STT] 可用裝置如下：")
            mic_list = sr.Microphone.list_microphone_names()
            for index, name in enumerate(mic_list):
                print(f"Index: {index}, Microphone: {name}")
            raise e

        if self.auto_adjust_noise:
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1)

        info_log("[STT] 已完成環境噪音校正")

    def handle(self, data: dict = {}) -> dict:
        validated = STTInput(**data)
        debug_log(1, f"[STT] 接收到的資料: {validated}")

        """單次語音轉文字"""
        try:
            with self.mic as source:
                print("[STT] Listening...")
                audio = self.recognizer.listen(source)
            print("[STT] Transcribing...")
            text = self.recognizer.recognize_google(audio)
            debug_log(1, f"[STT] 辨識結果: {text}")
            return STTOutput(text=text, error=None).dict()
        except sr.UnknownValueError:
            error_log("[STT] 無法辨識語音")
            return STTOutput(text="", error="無法辨識語音").dict()
        except sr.RequestError as e:
            error_log(f"[STT] API 錯誤: {e}")
            return STTOutput(text="", error=f"API 錯誤: {e}").dict()

    def _realtime_loop(self):
        info_log("[STT] Real-time 模式啟動")
        while self._running:
            try:
                with self.mic as source:
                    print("[STT] 🎙 Listening...")
                    if self.phrase_time_limit > 0:
                        audio = self.recognizer.listen(source, phrase_time_limit=self.phrase_time_limit)
                    else:
                        audio = self.recognizer.listen(source)
                text = self.recognizer.recognize_google(audio)
                debug_log(1, f"[STT] 辨識結果: {text}")
                if self._callback:
                    self._callback(text)
            except sr.UnknownValueError:
                error_log("[STT] 無法辨識語音")
            except sr.RequestError as e:
                error_log(f"[STT] API 錯誤: {e}")
            except Exception as e:
                error_log(f"[STT] 錯誤: {e}")
                break

    def start_realtime(self, on_result=None):
        if self._running:
            print("[STT] Real-time 已在執行中")
            return
        self._callback = on_result
        self._running = True
        self._thread = threading.Thread(target=self._realtime_loop, daemon=True)
        self._thread.start()
        info_log("[STT] Real-time 模式已啟動")

    def stop_realtime(self):
        self._running = False
        if self._thread:
            self._thread.join()
        info_log("[STT] Real-time 模式已停止")

    def shutdown(self):
        self.stop_realtime()
        info_log("[STT] 模組已關閉")
