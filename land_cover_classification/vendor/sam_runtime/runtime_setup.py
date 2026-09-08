#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供两个创建入口复用的独立 venv 创建、安装与验证逻辑。"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import platform
import queue
import signal
import threading
import traceback
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
import subprocess
import sys

DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
CUDA_CANDIDATES = [((12, 8), "cu128"), ((12, 6), "cu126"),
                   ((12, 4), "cu124"), ((12, 1), "cu121"), ((11, 8), "cu118")]
PACKAGES = ["sam2", "opencv-contrib-python", "numpy", "Pillow",
            "segmentation-models-pytorch==0.4.*", "timm", "rasterio", "scipy", "PyYAML"]
TORCH_INFO = (
    "import json, torch, torchvision; "
    "print(json.dumps(dict(torch=torch.__version__, torchvision=torchvision.__version__, "
    "cuda_runtime=torch.version.cuda, cuda_available=torch.cuda.is_available())))"
)


def clean_environment(environ):
    """统一清理安装、依赖探测和推理进程继承的 QGIS 环境。"""
    env = dict(environ)
    roots = [env.get(key, "") for key in ("QGIS_PREFIX_PATH", "OSGEO4W_ROOT")]
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("PYTHON", "GDAL_", "PROJ_", "DYLD_")) or upper in (
                "QGIS_PREFIX_PATH", "OSGEO4W_ROOT", "VIRTUAL_ENV", "GEOTIFF_CSV",
                "LD_LIBRARY_PATH", "LD_PRELOAD", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            env.pop(key, None)
    parts = []
    for part in env.get("PATH", "").split(os.pathsep):
        normalized = part.replace("\\", "/").lower()
        if any(word in normalized for word in ("qgis", "osgeo4w")):
            continue
        if any(root and inside(part, root) for root in roots):
            continue
        if part:
            parts.append(part)
    env["PATH"] = os.pathsep.join(parts)
    env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8:backslashreplace")
    return env


def redact(text):
    """隐藏 URL 的认证、路径和查询凭据，保留主机及其余诊断。"""
    def mask(match):
        try:
            url = urlsplit(match.group())
            return urlunsplit((url.scheme, url.hostname or "[host]", "/[redacted]", "", ""))
        except ValueError:
            return "[redacted URL]"
    return re.sub(r"(?:https?|socks5h?|socks4)://[^\s<>\"']+", mask, text)


class LogStream:
    """将脱敏后的输出同时写入终端及日志文件。"""

    def __init__(self, stream, logfile):
        self.stream, self.logfile = stream, logfile

    def write(self, text):
        text = redact(text)
        self.stream.write(text)
        self.stream.flush()
        if self.logfile:
            self.logfile.write(text)
            self.logfile.flush()
        return len(text)

    def flush(self):
        self.stream.flush()
        if self.logfile:
            self.logfile.flush()


def check_cancelled():
    """界面通过文件协作取消，保留环境及诊断，不进行修复。"""
    cancel = os.environ.get("LCC_CANCEL_FILE")
    if cancel and Path(cancel).exists():
        raise InterruptedError("Installation cancelled by user; incomplete directories retained.")


def general_environment(environ):
    """保留用户通用索引；日志只报告来源，不输出可能含凭据的 URL。"""
    env = clean_environment(environ)
    source = "user-provided index" if env.get("PIP_INDEX_URL") else "default Tsinghua mirror"
    env["PIP_INDEX_URL"] = env.get("PIP_INDEX_URL") or DEFAULT_INDEX
    return env, source


def torch_environment(environ):
    """仅为 PyTorch 清除 pip 来源配置，禁用所有层级配置文件。"""
    env = dict(environ)
    for key in list(env):
        if key.upper().startswith("PIP_"):
            env.pop(key)
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def run(command, env, capture=False):
    """流式保留 stdout/stderr；静默安装期间也响应取消。"""
    check_cancelled()
    lines, pending = [], queue.Queue()
    with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace",
                          start_new_session=(os.name != "nt")) as process:
        def read_output():
            for line in process.stdout:
                pending.put(line)
            pending.put(None)
        threading.Thread(target=read_output, daemon=True).start()
        try:
            while True:
                check_cancelled()
                try:
                    line = pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                lines.append(line)
                if not capture:
                    print(redact(line), end="", flush=True)
            code = process.wait()
        except BaseException:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # 整个进程组包含 pip 构建子进程，取消后不能继续写入环境。
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    output = "".join(lines)
    if code:
        if capture:
            print(redact(output), end="", flush=True)
        raise RuntimeError("Subprocess failed (exit {}): {}. See original output above.".format(code, command[0]))
    return output if capture else None


def inside(path, parent):
    """比较解析后的目录，避免符号链接绕过目标保护。"""
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or parent in path.parents


def probe(target, link):
    """检查固定独立解释器及创建 venv 必需的标准库。"""
    print("Python: {}\nVersion: {}".format(sys.executable, sys.version), flush=True)
    if sys.version_info[:3] != (3, 12, 12) or sys.prefix != sys.base_prefix:
        raise RuntimeError("Base interpreter must be standalone CPython 3.12.12.")
    if any(inside(sys.executable, path) for path in (target, link)):
        raise RuntimeError("Base Python must be outside the venv.")
    import ssl
    import sqlite3
    import venv
    import ensurepip
    ssl.create_default_context()
    with sqlite3.connect(":memory:") as db:
        assert db.execute("select 40 + 2").fetchone()[0] == 42


def validate_target(target, link):
    """拒绝危险目录、相互包含以及指向其他环境的固定入口。"""
    for path in (target, link):
        resolved = path.resolve()
        protected = [Path.home(), Path.cwd(), Path(__file__).parent, Path(sys.base_prefix)]
        if resolved == Path(resolved.anchor) or any(inside(item, resolved) for item in protected):
            raise RuntimeError("Unsafe runtime path: {}".format(path))
    if target.resolve() != link.resolve():
        if inside(target, link) or inside(link, target):
            raise RuntimeError("Runtime paths must not contain one another.")
        if os.path.lexists(link):
            raise RuntimeError("Fixed entry already points to another location: {} -> {}".format(link, link.resolve()))


@contextmanager
def installation_lock(target):
    """原子创建目标旁的锁目录，绝不移除其他安装持有的锁。"""
    lock = target.parent / ("." + target.name + ".install-lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("Another installation or interrupted lock exists: {}".format(lock))
    try:
        (lock / "owner.txt").write_text(str(os.getpid()), encoding="utf-8")
        yield
    finally:
        (lock / "owner.txt").unlink()
        lock.rmdir()


def venv_python(target):
    """返回目标平台的 venv 解释器。"""
    return str(target / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def check_venv(target):
    """拒绝不完整环境和继承系统包的环境，保留原目录。"""
    cfg = (target / "pyvenv.cfg").read_text(encoding="utf-8")
    if not re.search(r"^include-system-site-packages\s*=\s*false\s*$", cfg, re.M | re.I):
        raise RuntimeError("venv must set include-system-site-packages=false: {}".format(target))
    if not Path(venv_python(target)).is_file():
        raise RuntimeError("Incomplete venv: {}".format(target))


def create_venv(target, link, env):
    """解释器和 venv 从一开始就放在最终位置，不覆盖已有目录。"""
    validate_target(target, link)
    if os.path.lexists(target):
        raise RuntimeError("Existing venv retained; validation only is allowed: {}".format(target))
    target.mkdir(parents=True)
    print("[stage] Creating venv: {}".format(target), flush=True)
    run([sys.executable, "-m", "venv", str(target)], env)
    check_venv(target)
    return venv_python(target)


def cuda_indexes(env):
    """沿用驱动能力对应的官方 CUDA wheel 候选顺序。"""
    if env.get("SAM_TORCH_CUDA_INDEX"):
        return [env["SAM_TORCH_CUDA_INDEX"]]
    if env.get("SAM_TORCH_CUDA_INDEXES"):
        return env["SAM_TORCH_CUDA_INDEXES"].split()
    version = (99, 99)
    try:
        output = subprocess.run(["nvidia-smi"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8", errors="replace").stdout
        match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", output)
        if match:
            version = tuple(map(int, match.groups()))
    except OSError:
        pass
    return ["https://download.pytorch.org/whl/" + name
            for required, name in CUDA_CANDIDATES if version >= required]


def torch_info(python, env):
    """读取安装版本与真实 CUDA 状态。"""
    output = run([python, "-c", TORCH_INFO], env, capture=True)
    info = json.loads(output.strip().splitlines()[-1])
    print("PyTorch: " + json.dumps(info), flush=True)
    return info


def torch_packages(env):
    """仅接受包及版本约束，防止覆盖专用索引或注入安装选项。"""
    packages = shlex.split(env.get("SAM_TORCH_PACKAGES") or "torch torchvision")
    pattern = r"(?:torch|torchvision)(?:(?:===|==|~=|!=|>=|<=|>|<)[A-Za-z0-9.*+!_-]+(?:,(?:==|~=|!=|>=|<=|>|<)[A-Za-z0-9.*+!_-]+)*)?"
    if len(packages) != 2 or not all(re.fullmatch(pattern, item) for item in packages):
        raise RuntimeError("SAM_TORCH_PACKAGES accepts torch and torchvision package/version requirements only.")
    if {re.split(r"[=~!<>]", item)[0] for item in packages} != {"torch", "torchvision"}:
        raise RuntimeError("SAM_TORCH_PACKAGES must specify both torch and torchvision.")
    return packages


def install_torch(python, env):
    """CUDA 候选逐个验证，全部失败才从独立 CPU 索引回退。"""
    packages = torch_packages(env)
    isolated = torch_environment(env)
    if sys.platform == "darwin":
        if env.get("SAM_TORCH_CUDA_INDEX") or env.get("SAM_TORCH_CUDA_INDEXES") or re.search(r"/cu[0-9]+(?:/|$)", env.get("SAM_TORCH_CPU_INDEX", "")):
            raise RuntimeError("CUDA-specific indexes are not applicable to macOS.")
        print("macOS: official PyPI Torch wheels; CPU validation; SAM2 CUDA extension disabled.", flush=True)
        if platform.machine() == "x86_64":
            raise RuntimeError(
                "Dependency conflict on macOS Intel: official Torch x86_64 wheels stop at 2.2.2; "
                "SAM2 1.1.0 requires torch>=2.5.1 and torchvision>=0.20.1. "
                "No compatible official wheel combination. Python is available, full AI environment is unsupported.")
        run([python, "-m", "pip", "--isolated", "install", "--only-binary=:all:"] +
            packages + ["--index-url", "https://pypi.org/simple"], isolated)
        return "cpu", torch_info(python, env)
    has_nvidia = bool(shutil.which("nvidia-smi", path=env.get("PATH")))
    if os.name != "nt":
        has_nvidia = has_nvidia or Path("/proc/driver/nvidia").is_dir() or Path("/usr/local/cuda").is_dir()

    def install(index):
        run([python, "-m", "pip", "--isolated", "install", "--force-reinstall"] +
            packages + ["--only-binary=:all:", "--index-url", index], isolated)
        return torch_info(python, env)

    if has_nvidia:
        for number, index in enumerate(cuda_indexes(env), 1):
            print("Trying CUDA PyTorch candidate {}".format(number), flush=True)
            try:
                info = install(index)
                if info["cuda_runtime"] and info["cuda_available"]:
                    run([python, "-c", "import torch; x=torch.ones(8,device='cuda'); assert (x+x).sum().item()==16; torch.cuda.synchronize()"], env)
                    return "cuda", info
            except (RuntimeError, ValueError) as exc:
                print(str(exc), flush=True)
        print("CUDA installation/verification failed. Falling back to CPU wheels.", flush=True)
    info = install(env.get("SAM_TORCH_CPU_INDEX") or CPU_INDEX)
    if info["cuda_runtime"]:
        raise RuntimeError("CPU index did not provide a CPU PyTorch build.")
    return "cpu", info


def verify_torch(before, after, mode):
    """禁止通用依赖替换专用 wheel，并保留最终 CUDA 校验。"""
    if any(before[key] != after[key] for key in ("torch", "torchvision", "cuda_runtime")):
        raise RuntimeError("General dependencies replaced PyTorch. Resolve the version conflict using SAM_TORCH_PACKAGES.")
    if mode == "cuda" and not (after["cuda_runtime"] and after["cuda_available"]):
        raise RuntimeError("Expected CUDA PyTorch, but CUDA is unavailable after dependency installation.")


def install_dependencies(python, target, env):
    """先装专用 wheel，再用精确约束安装通用包，冲突时让 pip 明确失败。"""
    env, source = general_environment(env)
    print("General dependencies: " + source, flush=True)

    def general(arguments):
        try:
            run([python, "-m", "pip", "install", "--index-url", env["PIP_INDEX_URL"]] + arguments, env)
        except RuntimeError:
            print("General dependency installation failed. Check compatibility/network; "
                  "set PIP_INDEX_URL=https://pypi.org/simple or another trusted mirror and retry.", flush=True)
            raise

    print("[stage] Installing pip build tools", flush=True)
    general(["--upgrade", "pip", "setuptools", "wheel"])
    print("[stage] Installing Torch/torchvision", flush=True)
    mode, before = install_torch(python, env)
    constraints = target / "torch-constraints.txt"
    constraints.write_text("torch=={}\ntorchvision=={}\n".format(before["torch"], before["torchvision"]), encoding="utf-8")
    compiler = "cl" if os.name == "nt" else "gcc"
    toolchain = shutil.which("nvcc", path=env.get("PATH")) and (
        shutil.which(compiler, path=env.get("PATH")) or
        (os.name != "nt" and shutil.which("clang", path=env.get("PATH"))))
    env["SAM2_BUILD_CUDA"] = "1" if sys.platform != "darwin" and toolchain and env.get("SAM2_BUILD_CUDA") != "0" else "0"
    print("SAM2_BUILD_CUDA=" + env["SAM2_BUILD_CUDA"], flush=True)
    # 复用已安装的构建工具与专用 PyTorch，避免隔离构建从通用源另装 torch。
    env["PIP_CONSTRAINT"] = str(constraints)
    print("[stage] Installing shared dependencies with exact Torch constraints", flush=True)
    general(["--no-build-isolation", "--constraint", str(constraints)] + PACKAGES)
    verify_torch(before, torch_info(python, env), mode)
    run([python, "-c", "import torch, torchvision, sam2, cv2, numpy, PIL, rasterio, scipy, yaml, timm, segmentation_models_pytorch; print('plugin runtime ok')"], env)
    run([python, "-m", "pip", "check"], env)
    return source, mode


def publish_link(target, link):
    """验证成功后才发布固定入口，失败时保留已创建的实体环境。"""
    if target.resolve() == link.resolve():
        return
    if os.name == "nt":
        # 通过环境传递路径，避免把用户目录拼入 PowerShell 命令文本。
        env = os.environ.copy()
        env["LCC_LINK"], env["LCC_TARGET"] = str(link), str(target)
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                                 "New-Item -ItemType Junction -Path $env:LCC_LINK -Target $env:LCC_TARGET -ErrorAction Stop | Out-Null"], env=env)
        if result.returncode:
            print('Manual junction command: mklink /J "{}" "{}"'.format(link, target))
            raise RuntimeError("Failed to create runtime junction; physical venv retained.")
    else:
        link.symlink_to(target, target_is_directory=True)


def functional_check(mode):
    """按 worker 的独立启动条件测试原生能力；不要求或下载模型。"""
    import ssl
    import sqlite3
    import numpy as np
    import torch
    import torchvision
    import cv2
    import PIL
    import scipy
    import yaml
    import timm
    import segmentation_models_pytorch
    from sam2.build_sam import build_sam2
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    from rasterio.warp import transform
    assert sys.version_info[:2] == (3, 12), sys.version
    assert sys.prefix != sys.base_prefix, "Expected isolated venv"
    ssl.create_default_context()
    with sqlite3.connect(":memory:") as db:
        assert db.execute("select 6 * 7").fetchone()[0] == 42
    data = np.arange(16, dtype=np.float32).reshape(4, 4)
    assert np.matmul(data, np.eye(4)).sum() == 120
    assert (torch.ones(4) * 2).sum().item() == 8
    boxes = torch.tensor([[0., 0., 2., 2.], [0., 0., 2., 2.]])
    assert torchvision.ops.nms(boxes, torch.tensor([0.9, 0.8]), 0.5).tolist() == [0]
    model = build_sam2("configs/sam2.1/sam2.1_hiera_t.yaml", ckpt_path=None, device="cpu")
    assert model is not None
    del model
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=4, height=4, count=1, dtype="float32",
                         crs="EPSG:4326", transform=from_origin(100, 30, 0.01, 0.01)) as ds:
            ds.write(data, 1)
        with memory.open() as ds:
            np.testing.assert_array_equal(ds.read(1), data)
    x, y = transform("EPSG:4326", "EPSG:3857", [100.], [30.])
    assert abs(x[0] - 11131949.079) < 1 and abs(y[0] - 3503549.844) < 1
    if mode == "cuda" or (mode == "auto" and torch.cuda.is_available()):
        assert (torch.ones(8, device="cuda") * 2).sum().item() == 16
        torch.cuda.synchronize()
        print("CUDA tensor operation: OK")
    print("SQLite, SSL, NumPy, Torch CPU, torchvision NMS, SAM2 build, Rasterio IO/CRS: OK")


def verify_environment(target, env, mode="auto"):
    """已有环境只执行完整验证；不修改包、不下载模型。"""
    print("[stage] Verifying environment: {}".format(target), flush=True)
    check_venv(target)
    python = venv_python(target)
    run([python, str(Path(__file__).resolve()), "--functional-check", "--mode", mode], env)
    run([python, "-m", "pip", "check"], env)


def main():
    """由平台入口启动公共安装，或独立执行环境验收。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--link")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--functional-check", action="store_true")
    parser.add_argument("--mode", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()
    logfile = None
    if os.environ.get("LCC_LOG_PATH") and os.environ.get("LCC_LOG_CAPTURED") != "1":
        logfile = open(os.environ["LCC_LOG_PATH"], "a", encoding="utf-8")
    original = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = LogStream(sys.stdout, logfile), LogStream(sys.stderr, logfile)
    try:
        if args.functional_check:
            functional_check(args.mode)
            return 0
        if not args.target or not args.link:
            parser.error("--target and --link are required")
        target, link = Path(os.path.abspath(args.target)), Path(os.path.abspath(args.link))
        env = clean_environment(os.environ)
        # 子进程输出由当前进程统一落盘，避免一条日志写入两次。
        env["LCC_LOG_CAPTURED"] = "1"
        print("Target: {}\nFixed entry: {}".format(target, link), flush=True)
        validate_target(target, link)
        with installation_lock(target):
            if os.path.lexists(target):
                verify_environment(target, env, args.mode)
            elif args.verify_only:
                raise RuntimeError("Missing venv: {}".format(target))
            else:
                check_cancelled()
                probe(target, link)
                python = create_venv(target, link, env)
                source, mode = install_dependencies(python, target, env)
                verify_environment(target, env, mode)
            check_cancelled()
            publish_link(target, link)
        print("[stage] Complete: {}. Models are provided externally.".format(link), flush=True)
        return 0
    except BaseException as exc:
        print(redact(traceback.format_exc()), file=sys.stderr, end="", flush=True)
        print("Runtime setup stopped: {}\nLog: {}".format(exc, os.environ.get("LCC_LOG_PATH", "terminal")), flush=True)
        return 130 if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 1
    finally:
        sys.stdout, sys.stderr = original
        if logfile:
            logfile.close()


if __name__ == "__main__":
    sys.exit(main())
