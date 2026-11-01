"""全域停止 CLI：``python -m uep.stop {stop,release,status,resolve-recovery} [--url URL]``。

token 只從環境變數／.env 的 ``UEP_STOP_TOKEN`` 讀（不接受命令列參數，避免留在 shell 歷史）；
URL 預設取 ``UEP_STOP_URL``。結束碼：0 成功、1 被拒（token 錯誤或未設定）、2 無法連線或其他錯誤、3 停止未完全生效（旗標失敗或部分步驟失敗）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from uep.config import get_settings
from uep.stop.api import PATH_RECOVERY, PATH_RELEASE, PATH_STOP
from uep.stop.service import MIN_TOKEN_BYTES


def _call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m uep.stop", description="全域停止（本機入口）")
    parser.add_argument("action", choices=("stop", "release", "status", "resolve-recovery"))
    parser.add_argument("--url", default=os.environ.get("UEP_STOP_URL"), help="本機 API 位址（預設取 UEP_STOP_URL）")
    parser.add_argument("--reason", default="global_stop")
    parser.add_argument("--operator", default="local_cli")
    args = parser.parse_args(argv)

    secret = get_settings().stop_token
    token = secret.get_secret_value() if secret else ""
    if len(token.encode("utf-8")) < MIN_TOKEN_BYTES:  # 過短視同未設定（與伺服端一致）
        print(f"未設定 UEP_STOP_TOKEN（至少 {MIN_TOKEN_BYTES} 位元組）", file=sys.stderr)
        return 1
    if not args.url:
        print("請以 --url 或 UEP_STOP_URL 指定本機 API 位址", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
    try:
        if args.action == "stop":
            status, body = _call("POST", base + PATH_STOP, token, {"reason": args.reason, "operator": args.operator})
        elif args.action == "resolve-recovery":
            status, body = _call("POST", base + PATH_RECOVERY, token, {"operator": args.operator})
        elif args.action == "release":
            status, body = _call("POST", base + PATH_RELEASE, token, {"operator": args.operator})
        else:
            status, body = _call("GET", base + PATH_STOP, token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"無法連線到全域停止 API：{type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(body, ensure_ascii=False))
    if status in (401, 503):
        return 1
    if args.action == "stop" and (status in (207, 500) or not body.get("flag_engaged", True) or body.get("errors")):
        print("警告：全域停止未完全生效，請檢查輸出的 errors 並重試或手動處理", file=sys.stderr)
        return 3
    return 0 if status == 200 else 2


if __name__ == "__main__":
    sys.exit(main())
