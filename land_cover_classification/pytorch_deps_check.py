# -*- coding: utf-8 -*-
"""PyTorch 主推理在插件统一运行环境中的依赖检查。

QGIS 主进程不直接导入 torch、rasterio 等重依赖。本模块只通过插件内统一 venv
的 Python 子进程探测运行状态；该 venv 同时服务 PyTorch 主推理和 SAM AI 编辑。
"""

import argparse
import json
import os
import subprocess
import sys


# 复用现有 SAM runtime 目录作为插件级统一运行环境，避免维护两套 venv。
DEFAULT_VENV_RELATIVE = os.path.join("vendor", "sam_runtime", "venv")

PYTORCH_REQUIREMENTS = [
    ("torch", "torch"),
    ("torchvision", "torchvision"),
    ("segmentation-models-pytorch", "segmentation_models_pytorch"),
    ("timm", "timm"),
    ("rasterio", "rasterio"),
    ("opencv-contrib-python-headless", "cv2"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("PyYAML", "yaml"),
]


def plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_venv_dir():
    return os.path.join(plugin_dir(), DEFAULT_VENV_RELATIVE)


def default_python_executable():
    """返回插件统一 venv 中的 Python 解释器路径。"""
    venv_dir = default_venv_dir()
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def runtime_environment(python_executable=None):
    """返回启动 PyTorch runner 时使用的清洁环境变量。"""
    python_executable = python_executable or default_python_executable()
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "QGIS_PREFIX_PATH"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["VIRTUAL_ENV"] = default_venv_dir()

    python_dir = os.path.dirname(os.path.abspath(python_executable))
    path_parts = [python_dir]
    for part in env.get("PATH", "").split(os.pathsep):
        if part and part not in path_parts:
            path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def venv_ready():
    return os.path.isfile(default_python_executable())


def requirements():
    return list(PYTORCH_REQUIREMENTS)


def check_runtime(python_executable=None):
    """探测目标解释器是否能导入 PyTorch 主推理依赖。"""
    python_executable = python_executable or default_python_executable()
    if not python_executable or not os.path.isfile(python_executable):
        return {
            "missing": [name for name, _ in requirements()],
            "error": "未找到插件统一 Python 解释器: {}".format(python_executable),
            "cuda_available": False,
            "torch_version": "",
        }

    probe = (
        "import json\n"
        "missing = []\n"
        "for display, mod in {modules!r}:\n"
        "    try:\n"
        "        __import__(mod)\n"
        "    except Exception:\n"
        "        missing.append(display)\n"
        "cuda_available = False\n"
        "torch_version = ''\n"
        "try:\n"
        "    import torch\n"
        "    torch_version = getattr(torch, '__version__', '')\n"
        "    cuda_available = bool(torch.cuda.is_available())\n"
        "except Exception:\n"
        "    pass\n"
        "print(json.dumps({{'missing': missing, 'cuda_available': cuda_available, "
        "'torch_version': torch_version}}, ensure_ascii=False))\n"
    ).format(modules=requirements())

    try:
        result = subprocess.run(
            [python_executable, "-c", probe],
            capture_output=True,
            env=runtime_environment(python_executable),
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "missing": [name for name, _ in requirements()],
            "error": "调用插件统一解释器失败: {}".format(exc),
            "cuda_available": False,
            "torch_version": "",
        }

    if result.returncode != 0:
        return {
            "missing": [name for name, _ in requirements()],
            "error": result.stderr.strip() or "PyTorch 主推理依赖探测失败。",
            "cuda_available": False,
            "torch_version": "",
        }

    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except ValueError:
        payload = {}
    payload.setdefault("missing", [name for name, _ in requirements()])
    payload.setdefault("error", "")
    payload.setdefault("cuda_available", False)
    payload.setdefault("torch_version", "")
    return payload


def _create_script_path():
    script = "create_sam_venv.bat" if os.name == "nt" else "create_sam_venv.sh"
    return os.path.join(plugin_dir(), "vendor", "sam_runtime", script)


def installation_hint(status=None):
    status = status or {}
    lines = [
        "PyTorch 主推理需要插件统一运行环境。",
        "该环境同时服务 PyTorch 主推理和 SAM AI 编辑，不再单独创建第二套 venv。",
        "",
        "运行环境目录:",
        "  {}".format(default_venv_dir()),
    ]
    if not venv_ready():
        lines.extend([
            "",
            "未发现插件统一虚拟环境，请运行:",
            "  {}".format(_create_script_path()),
        ])
    elif status.get("missing"):
        lines.extend([
            "",
            "虚拟环境已存在，但以下模块缺失或导入失败:",
            "  {}".format(", ".join(status.get("missing") or [])),
            "请重新运行 vendor/sam_runtime/ 下的环境创建脚本；必要时先删除 venv 或设置 SAM_RECREATE=1。",
        ])

    if status.get("error"):
        lines.extend(["", "诊断信息:", "  {}".format(status["error"])])

    if venv_ready() and not status.get("cuda_available", False):
        lines.extend([
            "",
            "未检测到可用 CUDA，插件会自动降级为 CPU 推理。大幅面影像可能耗时较长。",
        ])
    return "\n".join(lines)


def ensure_ready(python_executable=None):
    """统一入口，返回 `(ok, message)`。"""
    if not venv_ready():
        status = {
            "missing": [name for name, _ in requirements()],
            "error": "",
            "cuda_available": False,
        }
        return False, installation_hint(status)
    status = check_runtime(python_executable)
    if status.get("missing") or status.get("error"):
        return False, installation_hint(status)
    return True, ""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    status = check_runtime(args.python_executable)
    ok = not status.get("missing") and not status.get("error")
    payload = {
        "ok": ok,
        "python": args.python_executable or default_python_executable(),
        "venv": default_venv_dir(),
        "message": "" if ok else installation_hint(status),
    }
    payload.update(status)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif ok:
        print("PyTorch runtime ready: {}".format(default_python_executable()))
        if not payload.get("cuda_available"):
            print("CUDA unavailable; CPU fallback will be used.")
    else:
        print(payload["message"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
