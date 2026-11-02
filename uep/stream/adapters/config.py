"""設定解析——環境變數與 `.env`（自 ES 搬入的過渡版，F1 併入 UepSettings 後移除；`.env` 改查 Core repo 根）。

零第三方依賴（不用 python-dotenv），因為 Phase 0 的骨架承諾了零依賴，
而讀一個 KEY=VALUE 檔案不值得為此破例。

## 為什麼需要這一層

Echo Stream 是整合層，本身不含模型也不含金鑰，但要知道：

* 子系統 repo 在哪（`echo_tts` / `echo_stt` 等是獨立 repo，不是本專案的套件）
* LLM 的金鑰與端點（**由使用者填入 `.env`，不進版控**）

## `.env` 範例

見 `.env.example`。`.env` 已在 `.gitignore` 裡。

## 為什麼用路徑注入而不是 pip install -e

子系統各自有重量級依賴（torch、faster-whisper、pyannote），
把它們 install 進來會讓 Echo Stream 從「整合層」變成「什麼都裝」的胖專案，
也讓 Phase 0 的零依賴承諾破功。路徑注入讓依賴留在原地，
誰要用哪一層就只載入哪一層。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

ENV_FILENAME = ".env"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]  # uep/stream/adapters/config.py -> Core repo 根（N1：原為 ES repo 根）


@lru_cache(maxsize=1)
def _dotenv_values() -> dict[str, str]:
    """讀取專案根目錄的 `.env`。

    格式是最樸素的 ``KEY=VALUE``，支援 ``#`` 註解與前後引號。
    引號包起來的值可以跨多行（``SYSTEM_PROMPT`` 這類人設會用到）——
    開引號後一路收集到出現閉引號的那一行為止，換行保留。
    不支援變數展開——需要那個就該用真的設定系統了。
    """
    path = _project_root() / ENV_FILENAME
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'":
            quote = value[0]
            if len(value) >= 2 and value.endswith(quote):
                value = value[1:-1]
            else:
                # 多行值：逐行收集到閉引號。後續行**不** strip——
                # 縮排可能是人設內容的一部分
                parts = [value[1:]]
                while i < len(lines):
                    raw = lines[i]
                    i += 1
                    if raw.rstrip().endswith(quote):
                        parts.append(raw.rstrip()[:-1])
                        break
                    parts.append(raw)
                value = "\n".join(parts)
        if key:
            values[key] = value
    return values


def get(name: str, default: str | None = None) -> str | None:
    """取設定值。**環境變數優先於 `.env`**——臨時覆寫不必改檔案。"""
    from_env = os.environ.get(name)
    if from_env not in (None, ""):
        return from_env
    value = _dotenv_values().get(name)
    return value if value not in (None, "") else default


def require(name: str, hint: str = "") -> str:
    """取必要設定，缺少時給出可行動的錯誤訊息。"""
    value = get(name)
    if value:
        return value
    suffix = f"\n{hint}" if hint else ""
    raise RuntimeError(
        f"缺少設定 {name}。請在專案根目錄的 .env 填入，或設為環境變數。{suffix}"
    )


def get_bool(name: str, default: bool = False) -> bool:
    value = get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_float(name: str, default: float) -> float:
    value = get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


# --- 子系統路徑 ---

_SIBLING_GUESSES = {
    "TTS": "TestSeparateTTSSystem",
    "STT": "TestSeparateSTTSystem",
    "MEMORY": "TestSeperateMemorySystem",  # 註：原 repo 名就是這個拼法
    "SESSION": "TestSeparateSessionControl",
}


def subsystem_path(kind: str) -> Path:
    """解析子系統 repo 的路徑。

    先看 ``ECHO_STREAM_{KIND}_REPO``，沒有的話猜同層目錄——
    五個 repo 都在 ``Unforgettableeternalproject/`` 底下，
    這個猜測在我們的環境裡幾乎總是對的，省掉一次設定。
    """
    kind = kind.upper()
    configured = get(f"ECHO_STREAM_{kind}_REPO")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(
                f"ECHO_STREAM_{kind}_REPO 指向不存在的路徑：{path}"
            )
        return path

    guess = _SIBLING_GUESSES.get(kind)
    if guess:
        candidate = _project_root().parent / guess
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        f"找不到 {kind} 子系統。請在 .env 設定 ECHO_STREAM_{kind}_REPO=<repo 路徑>。"
    )


def ensure_subsystem(kind: str, package: str, repo: str | Path | None = None) -> Path | None:
    """讓子系統套件可 import，回傳採用的 repo 根目錄（無法判定時為 None）。

    優先序：

    1. **明確指定**（``repo`` 參數或 ``ECHO_STREAM_{KIND}_REPO``）→ 放進 ``sys.path`` 最前面。
    2. **環境已裝**（共用環境以 editable 安裝、或之後遷入 Core 的 ``subsystems/``）
       → 不動 ``sys.path``。若照舊猜同層 repo 並插到最前面，會蓋過環境裡的版本，
       遷移後就默默載到凍結的舊 repo（11/01 bot 的 PYTHONPATH 踩過同一個坑）。
    3. 都沒有 → 猜同層 repo（:func:`subsystem_path`）。
    """
    import importlib.util

    explicit = repo or get(f"ECHO_STREAM_{kind.upper()}_REPO")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        ensure_importable(path)
        return path

    try:
        spec = importlib.util.find_spec(package)
    except ValueError:  # 已在 sys.modules 但沒有 __spec__（測試替身）：視為已可 import
        return None
    if spec is not None:
        origin = spec.origin or next(iter(spec.submodule_search_locations or []), None)
        if origin is None:
            return None
        pkg_dir = Path(origin)
        if pkg_dir.name == "__init__.py":
            pkg_dir = pkg_dir.parent
        return pkg_dir.parent

    path = subsystem_path(kind)
    ensure_importable(path)
    return path


def ensure_importable(path: Path) -> None:
    """把 repo 根目錄放進 ``sys.path``，讓 ``import echo_tts`` 之類能成功。

    只有 ``adapters/`` 會呼叫這個——contracts 與 core 不碰子系統。
    """
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)
