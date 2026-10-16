"""一键启动脚本。

用法：
    python run.py                 # 默认 127.0.0.1:8300，自动打开浏览器
    python run.py --port 8301     # 指定端口
    python run.py --no-browser    # 不自动打开浏览器

它会做四件事：
1. 检查依赖是否齐全，缺什么就打印 pip 安装命令（不自动安装，避免污染环境）；
2. 若端口被占用，顺延到下一个可用端口；
3. 生成演示样例（若 samples/ 为空）；
4. 启动 uvicorn，并（可选）打开浏览器。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"

# 依赖名 -> 导入名（两者不一致的单独标注）
REQUIRED = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "python-docx": "docx",
    "PyYAML": "yaml",
    "pydantic": "pydantic",
}


def _say(message: str) -> None:
    """在 Windows 控制台（可能是 GBK 代码页）下也能安全输出中文。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(message, flush=True)


def check_dependencies() -> bool:
    missing = []
    for package, module in REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        _say("缺少以下依赖，请先安装：")
        _say("    pip install " + " ".join(missing))
        _say("（或一次性安装全部：pip install -r requirements.txt）")
        return False
    return True


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, start: int, tries: int = 20) -> int:
    for offset in range(tries):
        candidate = start + offset
        if port_available(host, candidate):
            if offset:
                _say(f"端口 {start} 已被占用，改用 {candidate}。")
            return candidate
    raise SystemExit(f"从 {start} 起的 {tries} 个端口都被占用，请用 --port 指定其它端口。")


def ensure_samples() -> None:
    samples = sorted((ROOT / "samples").glob("*.docx"))
    if samples:
        return
    _say("samples/ 为空，正在生成演示样例……")
    script = ROOT / "scripts" / "make_samples.py"
    if script.exists():
        subprocess.run([sys.executable, str(script)], cwd=str(ROOT), check=False)
    else:
        _say("未找到 scripts/make_samples.py，跳过生成。")


def open_when_ready(url: str, host: str, port: int, timeout: float = 40.0) -> None:
    """轮询端口，确认服务真的起来了再打开浏览器。

    不用固定延迟：配置较低的机器（或 U 盘上的便携版）上，uvicorn 还没绑定端口
    浏览器就先打开了，用户看到「无法访问」会以为程序坏了。等端口被占用即视为就绪。
    """

    def worker() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not port_available(host, port):
                break  # 端口已被 uvicorn 占用 = 服务已就绪
            time.sleep(0.3)
        time.sleep(0.2)  # 留一点时间让 ASGI 生命周期跑完
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="启动格致 · 学术文档格式智能检查与规范化助手")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8300, help="监听端口，默认 8300")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    _say("=" * 62)
    _say("  格致 · 学术文档格式智能检查与规范化助手  DocFmtChecker")
    _say("=" * 62)

    if not check_dependencies():
        return 1

    port = pick_port(args.host, args.port)
    ensure_samples()

    url = f"http://{args.host}:{port}/"

    # 把实际端口落盘：目录被拷到别的机器后端口可能顺延，
    # 留个记录方便用户和辅助脚本查到真实地址。
    try:
        data_dir = ROOT / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "runtime_port.txt").write_text(str(port), encoding="utf-8")
    except OSError:
        pass

    _say("")
    _say(f"服务地址：{url}")
    _say(f"接口文档：{url}docs")
    _say("按 Ctrl+C 停止服务。")
    _say("")

    if not args.no_browser:
        open_when_ready(url, args.host, port)

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        args.host,
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    try:
        return subprocess.call(command, cwd=str(BACKEND))
    except KeyboardInterrupt:
        _say("\n已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
