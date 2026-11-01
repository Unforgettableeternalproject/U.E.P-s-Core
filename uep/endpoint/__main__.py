"""本機 CLI 端點入口：``python -m uep.endpoint --port <Core 控制埠>``。

密鑰由環境變數 ``UEP_ENDPOINT_SECRET`` 提供（不接受命令列參數，避免出現在行程列表）。
連線後取得 Lease，背景自動續約與 watchdog；stdin 指令：``status``、``reconnect``、``release``、``quit``。
輸出資料通道由組裝層以 ``CliEndpoint`` 當作 ``OutputAdapter`` 接線（此入口只負責控制面與 Lease）。
"""
from __future__ import annotations

import argparse
import sys
import threading

from uep.contracts.presence import AcquireResult
from uep.control.socket_transport import SocketControlClient
from uep.endpoint.auth import EndpointAuthError, load_secret_from_env
from uep.endpoint.endpoint import CliEndpoint
from uep.endpoint.protocol import CoreApiError
from uep.endpoint.reconnect import ReconnectSupervisor, start_supervisor


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m uep.endpoint", description="U.E.P 本機 CLI 端點")
    parser.add_argument("--endpoint-id", default="ep_cli")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--location", default="desktop", choices=["desktop", "discord", "website"])
    args = parser.parse_args(argv)

    try:
        secret = load_secret_from_env()
    except EndpointAuthError as exc:
        _out(f"錯誤：{exc}")
        return 2

    holder: dict[str, CliEndpoint] = {}
    try:
        client = SocketControlClient(
            args.host, args.port, args.endpoint_id, secret, on_control=lambda s: holder["ep"].on_control(s)
        )
    except ValueError as exc:  # 非 loopback
        _out(f"錯誤：{exc}")
        return 2
    endpoint = CliEndpoint(args.endpoint_id, client.api, location=args.location, sink=_out)
    holder["ep"] = endpoint
    try:
        client.connect()
        result = endpoint.start_lease()
    except (OSError, CoreApiError, EndpointAuthError) as exc:
        _out(f"錯誤：{exc}")
        return 1
    if result is not AcquireResult.GRANTED:
        reason = endpoint.last_deny_reason
        if result is AcquireResult.NOT_QUIESCED:
            _out("Core 仍在等待停止證據（可能是其他端點，或需操作員 resolve-recovery）")
        _out(f"無法取得 Lease：{result.value}" + (f"（{reason.value}）" if reason is not None else ""))
        if not endpoint.acquire_retryable:
            _out("全域停止中，不再重試；解除後請重新啟動端點")
        client.close()
        return 1
    endpoint.start()
    stop = threading.Event()
    supervisor = ReconnectSupervisor(lambda: client.connected, client.connect, endpoint.reconnect)
    start_supervisor(supervisor, stop)  # 斷線後自動以退避重連並同步快照
    try:
        for line in sys.stdin:
            command = line.strip().lower()
            if command == "quit":
                break
            if command == "status":
                remaining = endpoint.remaining_ttl()
                if endpoint.lease is None or remaining is None:
                    _out("未持有 Lease")
                else:
                    _out(f"持有 Lease，剩餘 {remaining:.1f} 秒")
            elif command == "reconnect":
                try:
                    client.connect()
                    _out(f"重連結果：{endpoint.reconnect().value}")
                except (OSError, CoreApiError, EndpointAuthError) as exc:
                    _out(f"重連失敗：{exc}")
            elif command == "release":
                endpoint.release()
    finally:
        stop.set()
        endpoint.release()
        endpoint.close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
