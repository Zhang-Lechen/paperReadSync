"""Start the local UI from any working directory; opening it never starts reading."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def running_here(url):
    # Ignore system proxies for loopback, as the application itself does.
    try:
        with build_opener(ProxyHandler({})).open(url + "/health", timeout=1) as response:
            value = json.load(response)
    except HTTPError:
        raise RuntimeError("端口被其他服务占用，请关闭其他服务或用 --port 指定端口。") from None
    except URLError:
        return False
    except (ValueError, OSError):
        raise RuntimeError("端口上的服务无法确认身份，未启动第二个进程。") from None
    if (not isinstance(value, dict) or value.get("status") != "ok"
            or not isinstance(value.get("project_root"), str)
            or Path(value.get("project_root", "")).resolve() != ROOT
            or value.get("account") != "default" or value.get("mock", True)):
        raise RuntimeError("端口已被其他项目或运行模式占用，请使用 --port 指定端口。")
    return True


def main():
    parser = argparse.ArgumentParser(description="启动 PaperRead Sync 本地界面")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--check", action="store_true", help="只检查现有服务，不启动或打开浏览器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口应在 1024 至 65535 之间")
    url = f"http://127.0.0.1:{args.port}"
    if running_here(url):
        print(f"本项目已在运行：{url}")
        if not args.no_browser and not args.check:
            webbrowser.open(url)
        return 0
    if args.check:
        print("本项目尚未启动。")
        return 1
    print(f"正在启动：{url}\n本窗口保持运行；按 Ctrl+C 停止。", flush=True)
    options = {"cwd": ROOT}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen([sys.executable, "-m", "paperread", "--root", str(ROOT),
                                "serve", "--port", str(args.port)], **options)
    try:
        deadline = time.monotonic() + 20
        while process.poll() is None:
            if running_here(url):
                if not args.no_browser:
                    webbrowser.open(url)
                return process.wait()
            if time.monotonic() >= deadline:
                raise RuntimeError("启动超时，请查看本窗口的错误提示。")
            time.sleep(.2)
        return process.returncode or 1
    except KeyboardInterrupt:
        print("\n正在停止本次启动的服务……", flush=True)
        return 0
    finally:
        if process.poll() is None:
            if sys.platform == "win32":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as error:
        print(f"无法启动：{error}", file=sys.stderr)
        raise SystemExit(1)
