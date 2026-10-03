#!/usr/bin/env python3
"""问桥的两个监听端口「你还活着吗」。

在 AstrBot 容器里跑（那两个端口只在容器内可达）：

    docker compose exec -T astrbot python3 - < deploy/probe.py

为什么值得单独做一个探针：这两个端口只有在插件导入成功、后台任务起来了之后
才会存在。插件导入失败时，AstrBot 照常启动、日志里只有一条 traceback、QQ 那边
完全静默——从外面看「服务是好的」，要等用户发现机器人不说话了才知道。部署完立刻
问一句，把这种失败变成红灯。

探针路径故意选一个没有任何路由匹配的：aiohttp 会在路由表里就地 404，不外发请求、
不读文件、不花额度。返回什么状态码不重要，只要有回应就说明 router 在服务。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

# 没有任何路由匹配这个路径，所以答案必然由路由表本身产生。
PROBE_PATH = "/__bridge_probe__"


def env_flag(name: str, default: bool) -> bool:
    """读环境变量里的开关。没配就用默认值，配了但像假值就当关。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def probe_listener(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """发一个请求、拿到任何回应就算活着。

    光 connect 不够：端口被别的进程占着也会连上，recv 会一直挂着——那种情况在
    超时之后会被当成没在服务。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                f"GET {PROBE_PATH} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode()
            )
            reply = sock.recv(64)
    except OSError as exc:
        return False, f"连不上（{type(exc).__name__}: {exc}）"

    if not reply:
        return False, "连上了但没回话（端口被占，但占它的东西不在说 HTTP）"

    status = reply.split(b"\r\n", 1)[0].decode("ascii", "replace")
    # "HTTP/1.0 404 Not Found" → 状态码是第二个字段，别去 match 整行。
    code = status.split()[1] if len(status.split()) > 1 else "?"
    if code == "404":
        return True, f"活着（{status}，路由表自己回的）"
    return True, f"活着，但状态码是「{status}」——有路由吃掉了探针路径，值得看一眼"


def probe_dir(path: str, what: str) -> tuple[bool, str]:
    """目录在不在。图片和 id 库都要往里写，目录不存在时插件不会报错，只会让
    图片发不出去、去重键对不上——所以这里明确查出来。"""
    if not path:
        return True, "没配置，跳过"
    if os.path.isdir(path):
        return True, f"存在（{path}）"
    return False, f"不存在：{path}（{what} 写不进去）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="探活桥在 AstrBot 容器里的两个监听端口。")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--llm-port", type=int, default=env_port("MAX_BRIDGE_LLM_PROXY_PORT", 6199))
    ap.add_argument("--media-port", type=int, default=env_port("MAX_BRIDGE_MEDIA_PORT", 6198))
    ap.add_argument("--media-dir", default=os.environ.get("MAX_BRIDGE_MEDIA_DIR", ""))
    ap.add_argument("--db", default=os.environ.get("MAX_BRIDGE_DB", ""))
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument(
        "--skip-llm",
        action="store_true",
        default=not env_flag("MAX_BRIDGE_LLM_PROXY", True),
        help="不查模型代理端口（MAX_BRIDGE_LLM_PROXY 关掉时自动跳过）",
    )
    args = ap.parse_args(argv)

    checks: list[tuple[str, bool, str]] = []
    if args.skip_llm:
        checks.append(("模型代理", True, "按配置未启用，跳过"))
    else:
        checks.append(("模型代理", *probe_listener(args.host, args.llm_port, args.timeout)))
    checks.append(("媒体服务", *probe_listener(args.host, args.media_port, args.timeout)))
    checks.append(("图片暂存目录", *probe_dir(args.media_dir, "入站图片")))
    checks.append(("id 映射库目录", *probe_dir(os.path.dirname(args.db), "id 映射")))

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    print("桥的探针：")
    for name, ok, detail in checks:
        mark = "ok  " if ok else "失败"
        print(f"  {mark} {name.ljust(width)}  {detail}")
        if not ok:
            failed += 1

    if failed:
        print(f"\n{failed} 项没过。插件可能没加载起来：docker compose logs astrbot | tail -50")
        return 1
    print("\n都活着。")
    return 0


if __name__ == "__main__":
    sys.exit(main())