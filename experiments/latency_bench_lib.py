"""延遲基準共用：句子表、統計、資源監控、播放模擬（純標準庫 + numpy，core env 與 .venv-voxcpm 皆可 import）。

資源監控不依賴 psutil（.venv-voxcpm 沒裝）：
- GPU：常駐的 `nvidia-smi -lms`（使用率、溫度、時脈、功耗、降頻原因、全卡記憶體）
- WDDM 共享記憶體：`typeperf "\\GPU Process Memory(*)\\Shared Usage"`（本行程的 pid 欄位）
- CPU / RSS：ctypes 呼叫 kernel32 / psapi
"""
import ctypes
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs", "latency_bench")
RAW = os.path.join(OUT, "raw")
WAV = os.path.join(OUT, "wav")
DEST = "E:/Materials/Voices/UEP/2026-11-01_latency_bench"
for _d in (OUT, RAW, WAV):
    os.makedirs(_d, exist_ok=True)

# ----------------------------------------------------------------------------- 測試句
# 每桶 4 句輪替：短 ≈1–2 秒、中 ≈4–7 秒、長 ≥12 秒（單一合成單元，不經 ES 切句器）
TEXTS = {
    "zh": {
        "short": ["好的，我知道了。", "今天天氣真不錯。", "等我一下喔。", "沒問題，交給我吧。"],
        "mid": ["我剛剛把桌上的東西整理好了，接下來想先喝杯熱茶休息一下。",
                "明天早上九點有個會議，記得提前十分鐘到會議室準備資料。",
                "週末如果天氣好的話，我們一起去河濱公園散步聊聊天吧。",
                "這個問題我再確認一下，晚點把結果整理好傳給你看看。"],
        "long": ["昨天晚上我一個人在家整理舊照片，翻到好幾張以前和朋友一起去旅行的合影，忍不住笑了起來，也想起了很多快要忘記的有趣小事。",
                 "最近公司在討論新的專案計畫，大家提出了很多不同的想法，我覺得最重要的是先把時程和分工確認清楚，再一步一步慢慢往前推進。",
                 "如果你下個星期有空的話，我們可以約在車站附近那間安靜的咖啡店，一邊喝咖啡一邊把這陣子累積下來的事情好好聊一聊。",
                 "前幾天我去市場買菜，碰到一位很久沒見的老朋友，我們站在路邊聊了快半個小時，才發現原來彼此都已經搬到同一個社區好幾年了。"],
    },
    "en": {
        "short": ["Sounds good.", "I'll be right there.", "Let me check that.", "That works for me."],
        "mid": ["I finished organizing everything on my desk, so what should we do next?",
                "The meeting starts at nine tomorrow, so please be there ten minutes early.",
                "If the weather is nice this weekend, let's take a walk along the river.",
                "I'll double check that issue and send you the results later tonight."],
        "long": ["Last night I stayed home and sorted through some old photographs, and I found several pictures from a trip with my friends, which made me laugh and remember many small things I had almost forgotten.",
                 "The team has been discussing the new project plan for a few days, and everyone has shared different ideas, but I think the most important thing is to confirm the schedule and responsibilities first.",
                 "If you are free next week, we could meet at the quiet coffee shop near the station, and spend an hour or so talking about everything that has happened over the past few months.",
                 "A few days ago I went to the market to buy vegetables and ran into an old friend I had not seen for years, and we talked on the street for almost half an hour before realizing we live in the same neighborhood."],
    },
    "ja": {
        "short": ["わかりました。", "ちょっと待ってね。", "ありがとう。", "いいですよ。"],
        "mid": ["机の上の仕事を全部片付けたので、次は何をしましょうか？",
                "明日の朝九時に会議があるので、十分前に来てくださいね。",
                "週末に天気がよかったら、一緒に川沿いを散歩しましょう。",
                "その問題はもう一度確認して、今夜結果を送りますね。"],
        "long": ["昨日の夜、家で古い写真を整理していたら、友達と旅行に行ったときの写真が何枚も出てきて、思わず笑ってしまい、忘れかけていた小さな出来事をたくさん思い出しました。",
                 "最近、会社では新しい計画について話し合っていて、みんなからいろいろな意見が出ていますが、まずは予定と役割分担をはっきりさせてから、少しずつ進めていくのが大切だと思います。",
                 "もし来週時間があれば、駅の近くの静かな喫茶店で会って、コーヒーを飲みながら、ここ数か月のあいだにあったことをゆっくり話しませんか。",
                 "数日前に市場へ野菜を買いに行ったら、何年も会っていなかった古い友達にばったり会って、道ばたで三十分近く話してから、お互い同じ町に住んでいることに気づきました。"],
    },
}
# ES 切句器（echo_stream.toml 的 split 段）情境用的段落：每段 3 句
PARAS = {
    "zh": ["今天早上我先去了市場，買了新鮮的青菜和水果。回到家以後，我把桌上的東西整理好，順便泡了一壺熱茶。等你有空的時候，我們再一起討論下週的計畫吧。",
           "昨天的會議比我想像的還要順利，大家都提出了不錯的想法。我已經把重點整理成文件，晚點會傳給你看看。如果有哪裡需要修改，隨時跟我說就可以了。"],
    "en": ["I went to the market early this morning and bought some fresh vegetables and fruit. When I got home, I cleaned up my desk and made a pot of hot tea. Let's talk about next week's plan when you have time.",
           "Yesterday's meeting went better than I expected, and everyone shared some good ideas. I have already organized the main points into a document, and I will send it to you later. If anything needs to change, just let me know."],
    "ja": ["今朝は早めに市場へ行って、新鮮な野菜と果物を買ってきました。家に帰ってから、机の上を片付けて、温かいお茶を入れました。時間があるときに、来週の予定を一緒に相談しましょう。",
           "昨日の会議は思ったよりうまくいって、みんながいい意見を出してくれました。大事なところは資料にまとめたので、あとで送りますね。直すところがあれば、いつでも言ってください。"],
}
BUCKET_N = {"short": 20, "mid": 20, "long": 10}
if os.environ.get("LATBENCH_SMOKE"):
    BUCKET_N = {"short": 2, "mid": 2, "long": 2}
LANGS = ("zh", "en", "ja")


# ----------------------------------------------------------------------------- 統計
def pct(xs, ps=(50, 90, 95, 99)):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {}
    a = np.asarray(xs, dtype="float64")
    d = {"n": len(a), "mean": float(a.mean()), "min": float(a.min())}
    for p in ps:
        d[f"p{p}"] = float(np.percentile(a, p))
    d["max"] = float(a.max())
    return d


def outliers(rows, key, k=3.0, min_abs=0.0):
    """中位數 + k*MAD（至少 0.15*median）以上者視為離群；回傳 index 清單。"""
    xs = np.array([r[key] for r in rows if r.get(key) is not None], dtype="float64")
    if len(xs) < 5:
        return []
    med = float(np.median(xs))
    mad = float(np.median(np.abs(xs - med))) * 1.4826
    thr = med + max(k * mad, 0.15 * med, min_abs)
    return [i for i, r in enumerate(rows) if r.get(key) is not None and r[key] > thr]


def jdump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o))


# ----------------------------------------------------------------------------- 播放模擬
def playback_sim(chunks, prebuffer_s=0.0):
    """chunks: [(arrival_s, dur_s)]（arrival 相對於呼叫起點）。

    播放在「第一個 chunk 到手且累積音訊 ≥ prebuffer_s」時開始，之後以即時速度消耗。
    回傳 underrun 次數、總停頓秒數、播放起點。另算 safe_start_s：能做到零 underrun 的最早播放起點。
    """
    if not chunks:
        return {"underruns": 0, "stall_s": 0.0, "start_s": None, "safe_start_s": None}
    cum, need = 0.0, 0.0
    safe = chunks[0][0]
    for arr, d in chunks:
        safe = max(safe, arr - cum)
        cum += d
    # 起播：累積到 prebuffer_s 的那個 chunk 的到達時刻
    cum, start = 0.0, chunks[-1][0]
    for arr, d in chunks:
        cum += d
        if cum >= prebuffer_s:
            start = arr
            break
    t_play, under, stall, pos_audio = start, 0, 0.0, 0.0
    # 逐 chunk 模擬：playhead 應該播到 start + pos_audio；chunk 到達時若 playhead 已越過已緩衝的結尾 → underrun
    buffered_end = None  # 以播放時間軸表示的已緩衝結尾（絕對時間）
    for arr, d in chunks:
        if buffered_end is None:
            buffered_end = max(arr, start) + d
            continue
        if arr > buffered_end:  # 緩衝見底，等到 arr 才有新聲音
            under += 1
            stall += arr - buffered_end
            buffered_end = arr + d
        else:
            buffered_end += d
    return {"underruns": under, "stall_s": float(stall), "start_s": float(start), "safe_start_s": float(safe)}


# ----------------------------------------------------------------------------- Windows 資源（ctypes）
_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
_psapi = ctypes.WinDLL("psapi", use_last_error=True) if os.name == "nt" else None
if _k32 is not None:
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentProcess.argtypes = []


class _FT(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]


if _k32 is not None:
    _P = ctypes.POINTER(_FT)
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE, _P, _P, _P, _P]
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.GetSystemTimes.argtypes = [_P, _P, _P]
    _k32.GetSystemTimes.restype = wintypes.BOOL


def _ft(f):
    return (f.hi << 32) | f.lo


def process_start_epoch():
    """本行程建立時間（Unix epoch 秒）。"""
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    _k32.GetProcessTimes(_k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    return (_ft(c) - 116444736000000000) / 1e7


def sys_cpu_times():
    i, k, u = _FT(), _FT(), _FT()
    _k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    return _ft(i), _ft(k) + _ft(u)  # idle, busy+idle(kernel 含 idle)


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def proc_mem():
    """(working_set_MB, private_MB)。"""
    p = _PMC()
    p.cb = ctypes.sizeof(p)
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(p), p.cb)
    return p.WorkingSetSize / 2 ** 20, p.PrivateUsage / 2 ** 20


def proc_cpu_s():
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    _k32.GetProcessTimes(_k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    return (_ft(k) + _ft(u)) / 1e7


class Monitor:
    """背景取樣：GPU（nvidia-smi）、WDDM 共享/專用記憶體（typeperf）、CPU、RSS、torch 記憶體。"""

    SMI = ("timestamp,utilization.gpu,temperature.gpu,clocks.sm,clocks.max.sm,clocks.mem,power.draw,"
           "memory.used,pstate,clocks_throttle_reasons.active")

    def __init__(self, interval=1.0, torch_mod=None):
        self.interval, self.torch = interval, torch_mod
        self.samples, self._smi, self._pc, self._eng = [], {}, {}, {}
        self._stop = threading.Event()
        self._procs = []
        self.ncpu = os.cpu_count() or 1
        self.t0 = time.perf_counter()

    def _smi_reader(self):
        p = subprocess.Popen(["nvidia-smi", f"--query-gpu={self.SMI}", "--format=csv,noheader,nounits", "-lms", "500"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._procs.append(p)
        for line in p.stdout:
            f = [x.strip() for x in line.split(",")]
            if len(f) < 10:
                continue
            try:
                self._smi = {"gpu_util": float(f[1]), "temp_c": float(f[2]), "sm_mhz": float(f[3]), "sm_max_mhz": float(f[4]),
                             "mem_mhz": float(f[5]), "power_w": float(f[6]), "vram_used_mb": float(f[7]), "pstate": f[8],
                             "throttle": f[9]}
            except ValueError:
                pass

    def _pc_reader(self):
        pid = os.getpid()
        cmd = ["typeperf", r"\GPU Process Memory(*)\Shared Usage", r"\GPU Process Memory(*)\Dedicated Usage",
               r"\GPU Adapter Memory(*)\Shared Usage", "-si", "1"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        cols = None
        for raw in p.stdout:
            line = raw.decode("latin1").strip()
            if line.startswith('"(PDH-CSV'):
                cols = [c.strip('"') for c in line.split('","')]
                continue
            if cols is None or not line.startswith('"'):
                continue
            vals = [v.strip('"') for v in line.split('","')]
            sh = de = ad = 0.0
            for c, v in zip(cols[1:], vals[1:]):
                try:
                    x = float(v)
                except ValueError:
                    continue
                if f"pid_{pid}_" in c:
                    if "Shared Usage" in c:
                        sh += x
                    elif "Dedicated Usage" in c:
                        de += x
                elif "GPU Adapter Memory" in c and "Shared Usage" in c:
                    ad += x
            self._pc = {"proc_shared_mb": sh / 2 ** 20, "proc_dedicated_mb": de / 2 ** 20, "adapter_shared_mb": ad / 2 ** 20}

    def _eng_reader(self):
        """其他行程的 GPU 引擎使用率（判斷是否有外部競爭）。"""
        import re
        pid = os.getpid()
        p = subprocess.Popen(["typeperf", r"\GPU Engine(*)\Utilization Percentage", "-si", "1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        cols = None
        for raw in p.stdout:
            line = raw.decode("latin1").strip()
            if line.startswith('"(PDH-CSV'):
                cols = [c.strip('"') for c in line.split('","')]
                continue
            if cols is None or not line.startswith('"'):
                continue
            vals = [v.strip('"') for v in line.split('","')]
            per = {}
            for c, v in zip(cols[1:], vals[1:]):
                m = re.search(r"pid_(\d+)_", c)
                if not m:
                    continue
                try:
                    x = float(v)
                except ValueError:
                    continue
                q = int(m.group(1))
                per[q] = max(per.get(q, 0.0), x)
            mine = per.pop(pid, 0.0)
            top = max(per.items(), key=lambda kv: kv[1]) if per else (0, 0.0)
            self._eng = {"gpu_eng_mine": mine, "gpu_eng_other_sum": sum(per.values()), "gpu_eng_other_top": top[1], "gpu_eng_other_top_pid": top[0]}

    def start(self):
        for fn in (self._smi_reader, self._pc_reader, self._eng_reader):
            threading.Thread(target=fn, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        idle0, tot0 = sys_cpu_times()
        pc0, tw0 = proc_cpu_s(), time.perf_counter()
        while not self._stop.wait(self.interval):
            idle1, tot1 = sys_cpu_times()
            pc1, tw1 = proc_cpu_s(), time.perf_counter()
            s = {"t": tw1 - self.t0, "sys_cpu_pct": 100.0 * (1 - (idle1 - idle0) / max(1, tot1 - tot0)),
                 "proc_cpu_pct": 100.0 * (pc1 - pc0) / max(1e-6, tw1 - tw0) / self.ncpu}
            idle0, tot0, pc0, tw0 = idle1, tot1, pc1, tw1
            ws, priv = proc_mem()
            s.update(rss_mb=ws, private_mb=priv)
            if self.torch is not None and self.torch.cuda.is_available():
                s.update(t_alloc_mb=self.torch.cuda.memory_allocated() / 2 ** 20, t_reserved_mb=self.torch.cuda.memory_reserved() / 2 ** 20,
                         t_max_alloc_mb=self.torch.cuda.max_memory_allocated() / 2 ** 20)
            s.update(self._smi)
            s.update(self._pc)
            s.update(self._eng)
            self.samples.append(s)

    def stop(self):
        self._stop.set()
        for p in self._procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def window(self, t_a, t_b):
        """perf_counter 區間內的樣本摘要。"""
        a, b = t_a - self.t0, t_b - self.t0
        w = [s for s in self.samples if a <= s["t"] <= b]
        if not w:
            return {}
        out = {"n": len(w)}
        for k in ("gpu_util", "temp_c", "sm_mhz", "power_w", "sys_cpu_pct", "proc_cpu_pct", "rss_mb", "vram_used_mb",
                  "t_alloc_mb", "t_reserved_mb", "proc_shared_mb", "proc_dedicated_mb", "adapter_shared_mb",
                  "gpu_eng_mine", "gpu_eng_other_sum", "gpu_eng_other_top"):
            xs = [s[k] for s in w if k in s]
            if xs:
                out[k] = {"mean": float(np.mean(xs)), "max": float(np.max(xs)), "min": float(np.min(xs))}
        out["throttle_codes"] = sorted({s.get("throttle") for s in w if s.get("throttle")})
        return out


def get_affinity():
    pm, sm = ctypes.c_size_t(), ctypes.c_size_t()
    _k32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    _k32.GetProcessAffinityMask(_k32.GetCurrentProcess(), ctypes.byref(pm), ctypes.byref(sm))
    return pm.value


def set_affinity(mask):
    _k32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    return bool(_k32.SetProcessAffinityMask(_k32.GetCurrentProcess(), mask))


class _PPTS(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def set_power_throttling(enabled):
    """ProcessPowerThrottling（EcoQoS）：enabled=False 明確關閉執行速度節流（不讓排程器把本行程當背景工作丟 E 核）；None = 還原系統管理。"""
    st = _PPTS(1, 0 if enabled is None else 1, 1 if enabled else 0)
    _k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    return bool(_k32.SetProcessInformation(_k32.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st)))


def cur_cpu():
    """目前執行緒所在的邏輯處理器編號（12700F：0–15 為 P 核的 8C16T，16–19 為 E 核）。"""
    return _k32.GetCurrentProcessorNumber()


def now():
    return time.perf_counter()
