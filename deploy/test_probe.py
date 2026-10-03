#!/usr/bin/env python3
"""给 deploy/probe.py 本身做测试。

探针的唯一职责就是在部署失败时说真话，所以它自己得先被验证过：每个分支都用一个
真的监听端口或一个真的空目录跑一遍，看它报的结论对不对。只用 stdlib。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PROBE = Path(__file__).resolve().parent / "probe.py"
PY = sys.executable


def responder(status_line: str, *, silent: bool = False) -> int:
    """起一个监听端口，接一个请求、回一行状态行。返回端口号。

    silent=True 时接了连接但不回话——模拟「端口被占但没人在服务 HTTP」。
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(5)
                conn.recv(4096)
                if not silent:
                    body = b"probe"
                    conn.sendall(
                        f"{status_line}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
                    )
        except OSError:
            pass
        finally:
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def dead_port() -> int:
    """先占一个端口再放掉，保证没人监听（而不是挑一个碰巧没人用的号）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, str(PROBE), *args], capture_output=True, text=True, timeout=30
    )
    return proc.returncode, proc.stdout + proc.stderr


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    failures = 0

    # 全都活着 → 退出 0
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        llm = responder("HTTP/1.0 404 Not Found")
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(llm),
            "--media-dir", tmp,
            "--db", f"{tmp}/ids.sqlite3",
            "--timeout", "5",
        )
        failures += not check("两个端口都回 404 时退出 0", code == 0, out.strip().splitlines()[-1] if out else "")

    # 一个端口没人监听 → 退出 1，并说清是哪个
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(dead_port()),
            "--media-dir", tmp,
            "--db", f"{tmp}/ids.sqlite3",
            "--timeout", "2",
        )
        failures += not check(
            "模型代理没人监听时退出 1", code == 1, "报了模型代理这一项"
        )
        failures += not check(
            "并且指明是模型代理坏了", "失败 模型代理" in out, "找得到失败项"
        )

    # 接了连接但不回话 → 退出 1（光 connect 会误判成活着）
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        mute = responder("HTTP/1.0 200 OK", silent=True)
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(mute),
            "--media-dir", tmp,
            "--db", f"{tmp}/ids.sqlite3",
            "--timeout", "1",
        )
        failures += not check("只连上不回话时退出 1", code == 1)
        failures += not check("并且说清是没回话", "没回话" in out)

    # 非 404 的回应仍然算活着，但要提醒路径被吃了
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        llm = responder("HTTP/1.0 200 OK")
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(llm),
            "--media-dir", tmp,
            "--db", f"{tmp}/ids.sqlite3",
            "--timeout", "5",
        )
        failures += not check("非 404 也算活着", code == 0)
        failures += not check("并且提示有路由吃掉了探针路径", "吃掉了探针路径" in out)

    # 目录不存在 → 退出 1（图片会发不出去，但插件本身不会报错）
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        llm = responder("HTTP/1.0 404 Not Found")
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(llm),
            "--media-dir", f"{tmp}/根本不存在",
            "--db", f"{tmp}/也不存在/ids.sqlite3",
            "--timeout", "5",
        )
        failures += not check("目录缺失时退出 1", code == 1)
        failures += not check("并且两个目录都点出来", "图片暂存目录" in out and "id 映射库目录" in out)

    # 关掉代理时不查那个端口（否则会误报失败）
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        code, out = run(
            "--media-port", str(media),
            "--llm-port", str(dead_port()),
            "--media-dir", tmp,
            "--db", f"{tmp}/ids.sqlite3",
            "--timeout", "2",
            "--skip-llm",
        )
        failures += not check("--skip-llm 时不查代理端口", code == 0, "退出 0")
        failures += not check("并且说明是按配置跳过", "未启用" in out)

    # 没传 --db 时 dirname 是空串，应该跳过而不是报错
    with tempfile.TemporaryDirectory() as tmp:
        media = responder("HTTP/1.0 404 Not Found")
        code, out = run("--media-port", str(media), "--llm-port", str(dead_port()), "--timeout", "2", "--skip-llm")
        failures += not check("没传目录参数时跳过目录检查", code == 0, "空配置不当失败")

    print()
    if failures:
        print(f"{failures} 项没过")
        return 1
    print("probe 自测全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())