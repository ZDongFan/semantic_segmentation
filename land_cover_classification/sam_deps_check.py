# -*- coding: utf-8 -*-
"""SAM AI 编辑所需的依赖与运行时检查。

主进程负责调度 SAM 子进程,因此本检查关注两件事:
- 默认 SAM 模型权重是否存在
- 用户机器上是否已经准备好 SAM 专用 venv,且该 venv 能够导入 torch、
  segment_anything、cv2、numpy

为了不污染 QGIS 主进程的 import 环境,这里只通过 subprocess 调用候选
Python 解释器,不在主进程内 import torch。
"""

import json
import os
import subprocess
import sys


# (显示名, 实际导入模块名)
_SAM_REQUIREMENTS = [
    ("torch", "torch"),
    ("segment-anything", "segment_anything"),
    ("opencv-contrib-python", "cv2"),
    ("numpy", "numpy"),
]

DEFAULT_MODEL_RELATIVE = os.path.join("models", "sam", "sam_vit_b_01ec64.pth")
DEFAULT_VENV_RELATIVE = os.path.join("vendor", "sam_runtime", "venv")


def plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_model_path():
    return os.path.join(plugin_dir(), DEFAULT_MODEL_RELATIVE)


def default_venv_dir():
    return os.path.join(plugin_dir(), DEFAULT_VENV_RELATIVE)


def default_python_executable():
    """返回 SAM 专用 venv 中的 Python 解释器路径。"""
    venv_dir = default_venv_dir()
    if os.name == "nt":
        candidate = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        candidate = os.path.join(venv_dir, "bin", "python")
    return candidate


def runtime_environment(python_executable=None):
    """返回启动 SAM 子进程时使用的清洁环境变量。"""
    python_executable = python_executable or default_python_executable()
    env = os.environ.copy()

    # QGIS/OSGeo4W 会设置自己的 Python 环境变量。SAM venv 的 Python
    # 版本可能不同,继承这些变量会导致加载错误版本的标准库。
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
        env.pop(key, None)

    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["VIRTUAL_ENV"] = default_venv_dir()

    python_dir = os.path.dirname(os.path.abspath(python_executable))
    path_parts = [python_dir]
    current_path = env.get("PATH", "")
    path_parts.extend(
        part for part in current_path.split(os.pathsep)
        if part and part not in path_parts)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def venv_ready():
    return os.path.isfile(default_python_executable())


def model_ready():
    return os.path.isfile(default_model_path())


def check_runtime(python_executable=None):
    """通过子进程探测目标解释器是否能导入 SAM 依赖。

    返回值: (missing_modules, error_message)
    """
    python_executable = python_executable or default_python_executable()
    if not python_executable or not os.path.isfile(python_executable):
        return [name for name, _ in _SAM_REQUIREMENTS], (
            "未找到 SAM 专用 Python 解释器: {}".format(python_executable))

    probe = (
        "import json, sys\n"
        "missing = []\n"
        "for display, mod in {modules!r}:\n"
        "    try:\n"
        "        __import__(mod)\n"
        "    except Exception:\n"
        "        missing.append(display)\n"
        "sys.stdout.write(json.dumps(missing))\n"
    ).format(modules=_SAM_REQUIREMENTS)

    try:
        result = subprocess.run(
            [python_executable, "-c", probe],
            capture_output=True,
            env=runtime_environment(python_executable),
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [name for name, _ in _SAM_REQUIREMENTS], (
            "调用 SAM 解释器失败: {}".format(exc))

    if result.returncode != 0:
        return [name for name, _ in _SAM_REQUIREMENTS], (
            result.stderr.strip() or "SAM 解释器探测失败。")

    try:
        missing = json.loads(result.stdout.strip() or "[]")
    except ValueError:
        missing = [name for name, _ in _SAM_REQUIREMENTS]
    return list(missing), ""


def installation_hint(missing, error=""):
    lines = ["AI 编辑功能需要独立的 SAM 运行环境:"]
    if not venv_ready():
        lines.extend([
            "  当前未发现 SAM 专用虚拟环境:{}".format(default_venv_dir()),
            "  请运行 vendor/sam_runtime/ 下的离线环境创建脚本:",
        ])
        if os.name == "nt":
            lines.append(
                "    {}".format(os.path.join(plugin_dir(), "vendor",
                                             "sam_runtime",
                                             "create_sam_venv.bat")))
        else:
            lines.append(
                "    {}".format(os.path.join(plugin_dir(), "vendor",
                                             "sam_runtime",
                                             "create_sam_venv.sh")))
    else:
        if missing:
            lines.extend([
                "  虚拟环境已存在,但下列模块缺失或导入失败:",
                "  {}".format(", ".join(missing)),
                "  请检查 vendor/sam_runtime/wheels 内的离线 wheels 是否完整,",
                "  必要时重新运行离线环境创建脚本。",
            ])
        if error:
            lines.extend(["", "诊断信息:", "  {}".format(error)])

    if not model_ready():
        lines.extend([
            "",
            "SAM 模型权重缺失,默认应放置在:",
            "  {}".format(default_model_path()),
            "请把 sam_vit_b_01ec64.pth 复制到该位置后再启动 AI 编辑。",
        ])
    return "\n".join(lines)


def ensure_ready(python_executable=None):
    """统一入口,返回 (ok, message)。

    ok 为 True 时表示模型权重、venv、依赖全部就绪;
    ok 为 False 时,message 是给用户的友好提示。
    """
    if not model_ready():
        return False, installation_hint([], "")
    if not venv_ready():
        return False, installation_hint(
            [name for name, _ in _SAM_REQUIREMENTS],
            "")
    missing, error = check_runtime(python_executable)
    if missing or error:
        return False, installation_hint(missing, error)
    return True, ""


if __name__ == "__main__":
    ok, message = ensure_ready()
    if ok:
        print("SAM runtime ready: {}".format(default_python_executable()))
        sys.exit(0)
    print(message)
    sys.exit(1)
