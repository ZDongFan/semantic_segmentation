"""在一个终端管理内部和公共服务，不安装依赖或初始化数据库。"""

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent.parent


def port_number(value):
    """限制监听端口为有效 TCP 端口。"""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须介于 1 和 65535。")
    return port


def check_ports(endpoints):
    """同时试绑定两个端口，拒绝冲突且不终止已有服务。"""
    sockets = []
    try:
        for host, port in endpoints:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            family, kind, protocol, _, address = addresses[0]
            sock = socket.socket(family, kind, protocol)
            sockets.append(sock)
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(address)
    finally:
        for sock in sockets:
            sock.close()


def stop_processes(processes, timeout=10):
    """仅停止本次创建的子进程，限时等待后强制回收。"""
    for process in processes:
        if process.poll() is None:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            except OSError:
                # 无控制台时 Windows 无法投递 Ctrl+Break，改为终止本次子进程。
                if process.poll() is None:
                    process.terminate()
    deadline = time.monotonic() + timeout
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_services(endpoints):
    """监控两个独立 Uvicorn 进程，退出时统一清理。"""
    processes = []
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    previous = {sig: signal.signal(sig, request_stop) for sig in signals}
    try:
        check_ports(endpoints)
        env = os.environ.copy()
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8:backslashreplace")
        for name, (host, port) in zip(("internal", "public"), endpoints):
            if stopping:
                return 0
            command = [sys.executable, "-m", "uvicorn", f"backend.api:{name}_app",
                       "--factory", "--host", host, "--port", str(port),
                       "--timeout-graceful-shutdown", "8"]
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(command, cwd=ROOT, env=env, **options)
            processes.append(process)
            print(f"{name}: http://{host}:{port} (PID {process.pid})", flush=True)
        print("正在启动两个服务，请查看下方 Uvicorn 日志。按 Ctrl+C 一起停止。", flush=True)
        while not stopping:
            for process in processes:
                code = process.poll()
                if code is not None:
                    print(f"服务进程 {process.pid} 已退出（{code}），正在停止其余服务。", file=sys.stderr)
                    return code if code > 0 else 1
            time.sleep(0.2)
        return 0
    except OSError as exc:
        print(f"启动失败：{exc}。请检查监听地址、端口占用和后端环境。", file=sys.stderr)
        return 1
    finally:
        stop_processes(processes)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    """解析独立监听地址，默认在所有 IPv4 网卡监听两个服务。"""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=("使用独立后端 Python 运行本脚本。服务自动加载仓库根 .env；"
                "LCC_ENV_FILE 可指定配置文件，已有环境变量优先。"
                "数据库须事先启动并初始化。Ctrl+C 同时停止两个服务。"
                "客户端使用服务器实际 IP 访问；内部端口仅向可信网络开放。"),
    )
    parser.add_argument("--internal-host", default="0.0.0.0", help="内部管理服务监听地址")
    parser.add_argument("--internal-port", type=port_number, default=8091, help="内部管理服务端口")
    parser.add_argument("--public-host", default="0.0.0.0", help="公共查询服务监听地址")
    parser.add_argument("--public-port", type=port_number, default=8092, help="公共查询服务端口")
    args = parser.parse_args()
    return run_services([(args.internal_host, args.internal_port),
                         (args.public_host, args.public_port)])


if __name__ == "__main__":
    raise SystemExit(main())
