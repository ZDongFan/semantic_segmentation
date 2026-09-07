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
import stat
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
    """清除 QGIS/Python 注入，但保留工具链搜索 PATH。"""
    env = dict(environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "QGIS_PREFIX_PATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    env.update(PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8:backslashreplace")
    return env


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
    """保留子进程错误输出，不打印可能含凭据的命令行。"""
    if capture:
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                                encoding="utf-8", errors="replace")
        code, output = result.returncode, result.stdout
    else:
        # pip 自身不保证隐藏路径或查询参数中的 token，流式隐藏 URL 后再输出。
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace") as process:
            for line in process.stdout:
                print(re.sub(r"https?://[^\s<>\"']+", "[package URL]", line), end="", flush=True)
            code, output = process.wait(), None
    if code:
        raise RuntimeError("Subprocess failed (exit {}). See output above.".format(code))
    return output


def inside(path, parent):
    """比较解析后的目录，避免符号链接绕过目标保护。"""
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or parent in path.parents


def probe(target, link):
    """验证基础解释器能力，禁止用当前 runtime 创建自身。"""
    print("Python: {}\nVersion: {}".format(sys.executable, sys.version), flush=True)
    if any(inside(sys.prefix, path) or inside(sys.executable, path) for path in (target, link)):
        raise RuntimeError("Candidate is the existing plugin runtime; choose a base Python.")
    import venv
    import ensurepip
    return venv, ensurepip


def validate_target(target, link):
    """只允许删除可识别的具体 venv，保护根目录、工程和基础 Python。"""
    target, link = Path(target).absolute(), Path(link).absolute()
    for path in (target, link):
        resolved = path.resolve()
        protected = [Path.home(), Path.cwd(), Path(__file__).parent,
                     Path(sys.base_prefix), Path(sys.prefix)]
        if resolved == Path(resolved.anchor) or any(inside(item, resolved) for item in protected):
            raise RuntimeError("Unsafe runtime path: {}".format(path))
        if path.exists() and not ((path / "pyvenv.cfg").is_file() or (path / ".lcc-runtime").is_file()):
            raise RuntimeError("Refusing to replace an unrecognized runtime directory: {}".format(path))
    if target.resolve() != link.resolve() and (inside(target, link) or inside(link, target)):
        raise RuntimeError("Runtime paths must not contain one another.")


def remove_entry(path):
    """删除已校验的入口；联接和符号链接只移除入口，不遍历目标。"""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        path.unlink()
    elif getattr(info, "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003):
        # 检查入口自身的 reparse tag，兼容失效联接，不能用祖先目录是否联接来判断。
        os.rmdir(str(path))
    else:
        shutil.rmtree(str(path))


def create_venv(target, link, env):
    """只在明确请求重建时移除旧环境，然后创建隔离 venv。"""
    validate_target(target, link)
    entries = list(dict.fromkeys([link, target]))
    if any(os.path.lexists(str(path)) for path in entries):
        if env.get("SAM_RECREATE") != "1":
            raise RuntimeError("Existing runtime found. Set SAM_RECREATE=1 to rebuild.")
        for path in entries:
            remove_entry(path)
    target.mkdir(parents=True)
    (target / ".lcc-runtime").write_text("Unified runtime installer\n", encoding="utf-8")
    # 失败时保留新目录及原始诊断，后续可显式重建。
    run([sys.executable, "-m", "venv", str(target)], env)
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    cfg = (target / "pyvenv.cfg").read_text(encoding="utf-8")
    if not re.search(r"^include-system-site-packages\s*=\s*false\s*$", cfg, re.M | re.I):
        raise RuntimeError("venv must set include-system-site-packages=false")
    run([str(python), "-c", "import sys; assert sys.prefix != sys.base_prefix; print(sys.prefix)"], env)
    run([str(python), "-m", "pip", "--version"], env)
    return str(python)


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
    info = json.loads(run([python, "-c", TORCH_INFO], env, capture=True))
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
    has_nvidia = bool(shutil.which("nvidia-smi", path=env.get("PATH")))
    if os.name != "nt":
        has_nvidia = has_nvidia or Path("/proc/driver/nvidia").is_dir() or Path("/usr/local/cuda").is_dir()

    def install(index):
        run([python, "-m", "pip", "--isolated", "install", "--force-reinstall"] +
            packages + ["--index-url", index], isolated)
        return torch_info(python, env)

    if has_nvidia:
        for number, index in enumerate(cuda_indexes(env), 1):
            print("Trying CUDA PyTorch candidate {}".format(number), flush=True)
            try:
                info = install(index)
                if info["cuda_runtime"] and info["cuda_available"]:
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

    general(["--upgrade", "pip", "setuptools", "wheel"])
    mode, before = install_torch(python, env)
    constraints = target / "torch-constraints.txt"
    constraints.write_text("torch=={}\ntorchvision=={}\n".format(before["torch"], before["torchvision"]), encoding="utf-8")
    compiler = "cl" if os.name == "nt" else "gcc"
    toolchain = shutil.which("nvcc", path=env.get("PATH")) and (
        shutil.which(compiler, path=env.get("PATH")) or
        (os.name != "nt" and shutil.which("clang", path=env.get("PATH"))))
    env["SAM2_BUILD_CUDA"] = "1" if toolchain and env.get("SAM2_BUILD_CUDA") != "0" else "0"
    print("SAM2_BUILD_CUDA=" + env["SAM2_BUILD_CUDA"], flush=True)
    # 复用已安装的构建工具与专用 PyTorch，避免隔离构建从通用源另装 torch。
    env["PIP_CONSTRAINT"] = str(constraints)
    general(["--no-build-isolation", "--constraint", str(constraints)] + PACKAGES)
    verify_torch(before, torch_info(python, env), mode)
    run([python, "-c", "import torch, torchvision, sam2, cv2, numpy, PIL, rasterio, scipy, yaml, timm, segmentation_models_pytorch; print('plugin runtime ok')"], env)
    run([python, "-m", "pip", "check"], env)
    return source, mode


def publish_link(target, link):
    """验证成功后才发布固定入口，失败时保留已创建的实体环境。"""
    if target == link:
        return
    if os.name == "nt":
        # 通过环境传递路径，避免把用户目录拼入 PowerShell 命令文本。
        env = os.environ.copy()
        env["LCC_LINK"], env["LCC_TARGET"] = str(link), str(target)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                 "New-Item -ItemType Junction -Path $env:LCC_LINK -Target $env:LCC_TARGET -ErrorAction Stop | Out-Null"], env=env)
        if result.returncode:
            print('Manual junction command: mklink /J "{}" "{}"'.format(link, target))
            raise RuntimeError("Failed to create runtime junction; physical venv retained.")
    else:
        link.symlink_to(target, target_is_directory=True)


def main():
    """探测模式不创建或安装任何内容；安装模式始终使用选定解释器。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--target", required=True)
    parser.add_argument("--link", required=True)
    parser.add_argument("--source", default="SAM_PYTHON")
    args = parser.parse_args()
    target, link = Path(os.path.abspath(args.target)), Path(os.path.abspath(args.link))
    try:
        probe(target, link)
        if args.probe:
            return 0
        env = clean_environment(os.environ)
        print("Interpreter source: " + args.source, flush=True)
        python = create_venv(target, link, env)
        source, mode = install_dependencies(python, target, env)
        publish_link(target, link)
        print("Runtime created: {}\nInterpreter source: {}\nGeneral index: {}\nPyTorch mode: {}".format(link, args.source, source, mode))
        return 0
    except (OSError, RuntimeError, ImportError, ValueError) as exc:
        print("Runtime setup failed: {}".format(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

