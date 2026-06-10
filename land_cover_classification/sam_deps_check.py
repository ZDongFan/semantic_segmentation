# -*- coding: utf-8 -*-
"""SAM AI 编辑运行环境检查。

QGIS 主进程只负责调度 SAM worker 子进程，因此本模块不得直接 import torch、sam2
或 segment_anything。所有重型依赖都通过插件内 venv 的 Python 子进程探测。
"""

import argparse
import json
import os
import subprocess
import sys


SAM1_BACKEND = "sam1"
SAM2_BACKEND = "sam2"
DEFAULT_BACKEND = SAM2_BACKEND

DEFAULT_SAM1_MODEL_TYPE = "vit_b"
DEFAULT_SAM2_MODEL_TYPE = "sam2.1_hiera_base_plus"
DEFAULT_SAM2_CONFIG = "configs/sam2.1/sam2.1_hiera_b+.yaml"

DEFAULT_SAM1_MODEL_RELATIVE = os.path.join(
    "models", "sam", "sam_vit_b_01ec64.pth")
DEFAULT_SAM2_MODEL_RELATIVE = os.path.join(
    "models", "sam2", "sam2.1_hiera_base_plus.pt")
DEFAULT_VENV_RELATIVE = os.path.join("vendor", "sam_runtime", "venv")

_SAM_REQUIREMENTS = {
    SAM2_BACKEND: [
        ("torch", "torch"),
        ("torchvision", "torchvision"),
        ("sam2", "sam2"),
        ("opencv-contrib-python", "cv2"),
        ("numpy", "numpy"),
    ],
    SAM1_BACKEND: [
        ("torch", "torch"),
        ("segment-anything", "segment_anything"),
        ("opencv-contrib-python", "cv2"),
        ("numpy", "numpy"),
    ],
}


def normalize_backend(backend=None):
    backend = (backend or DEFAULT_BACKEND).lower()
    if backend not in _SAM_REQUIREMENTS:
        raise ValueError("不支持的 SAM 后端: {}".format(backend))
    return backend


def plugin_dir():
    return os.path.dirname(os.path.abspath(__file__))


def default_model_type(backend=None):
    backend = normalize_backend(backend)
    if backend == SAM1_BACKEND:
        return DEFAULT_SAM1_MODEL_TYPE
    return DEFAULT_SAM2_MODEL_TYPE


def default_config_path(backend=None):
    backend = normalize_backend(backend)
    if backend == SAM2_BACKEND:
        return DEFAULT_SAM2_CONFIG
    return ""


def default_model_path(backend=None):
    backend = normalize_backend(backend)
    if backend == SAM1_BACKEND:
        relative = DEFAULT_SAM1_MODEL_RELATIVE
    else:
        relative = DEFAULT_SAM2_MODEL_RELATIVE
    return os.path.join(plugin_dir(), relative)


def default_venv_dir():
    return os.path.join(plugin_dir(), DEFAULT_VENV_RELATIVE)


def default_python_executable():
    """返回 SAM 专用 venv 中的 Python 解释器路径。"""
    venv_dir = default_venv_dir()
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def runtime_environment(python_executable=None):
    """返回启动 SAM worker 时使用的清洁环境变量。"""
    python_executable = python_executable or default_python_executable()
    env = os.environ.copy()

    # QGIS/OSGeo4W 会设置自己的 Python 环境变量，必须清除以免污染 SAM venv。
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


def model_ready(backend=None):
    return os.path.isfile(default_model_path(backend))


def requirements_for_backend(backend=None):
    backend = normalize_backend(backend)
    return list(_SAM_REQUIREMENTS[backend])


def check_runtime(python_executable=None, backend=None):
    """通过子进程探测目标解释器是否能导入指定后端依赖。

    返回值为 (missing_modules, error_message)。
    """
    backend = normalize_backend(backend)
    requirements = requirements_for_backend(backend)
    python_executable = python_executable or default_python_executable()
    if not python_executable or not os.path.isfile(python_executable):
        return [name for name, _ in requirements], (
            "未找到 SAM 专用 Python 解释器: {}".format(python_executable))

    probe = (
        "import json, sys\n"
        "missing = []\n"
        "for display, mod in {modules!r}:\n"
        "    try:\n"
        "        __import__(mod)\n"
        "    except Exception:\n"
        "        missing.append(display)\n"
        "sys.stdout.write(json.dumps(missing, ensure_ascii=False))\n"
    ).format(modules=requirements)

    try:
        result = subprocess.run(
            [python_executable, "-c", probe],
            capture_output=True,
            env=runtime_environment(python_executable),
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [name for name, _ in requirements], (
            "调用 SAM 解释器失败: {}".format(exc))

    if result.returncode != 0:
        return [name for name, _ in requirements], (
            result.stderr.strip() or "SAM 解释器探测失败。")

    try:
        missing = json.loads(result.stdout.strip() or "[]")
    except ValueError:
        missing = [name for name, _ in requirements]
    return list(missing), ""


def installation_hint(missing, error="", backend=None):
    backend = normalize_backend(backend)
    lines = [
        "AI 编辑功能需要插件内独立的 SAM 运行环境。",
        "当前后端: {}".format(backend),
    ]

    if not venv_ready():
        lines.extend([
            "",
            "未发现 SAM 专用虚拟环境:",
            "  {}".format(default_venv_dir()),
            "请运行 vendor/sam_runtime/ 下的在线环境创建脚本:",
        ])
        if os.name == "nt":
            lines.append("  {}".format(os.path.join(
                plugin_dir(), "vendor", "sam_runtime",
                "create_sam_venv.bat")))
        else:
            lines.append("  {}".format(os.path.join(
                plugin_dir(), "vendor", "sam_runtime",
                "create_sam_venv.sh")))
    elif missing:
        lines.extend([
            "",
            "虚拟环境已存在，但下列模块缺失或导入失败:",
            "  {}".format(", ".join(missing)),
            "请重新运行环境创建脚本；如需重建，可先删除 venv 或设置 SAM_RECREATE=1。",
        ])

    if error:
        lines.extend(["", "诊断信息:", "  {}".format(error)])

    if not model_ready(backend):
        lines.extend([
            "",
            "{} 模型权重缺失，默认应放置在:".format(backend.upper()),
            "  {}".format(default_model_path(backend)),
        ])
        if backend == SAM2_BACKEND:
            lines.append("请准备 sam2.1_hiera_base_plus.pt 后再启动 AI 编辑。")
        else:
            lines.append("请准备 sam_vit_b_01ec64.pth 后再启动 AI 编辑。")

    if backend == SAM2_BACKEND:
        lines.extend([
            "",
            "默认 SAM2 配置:",
            "  {}".format(default_config_path(backend)),
        ])
    return "\n".join(lines)


def ensure_ready(python_executable=None, backend=None):
    """统一入口，返回 (ok, message)。"""
    backend = normalize_backend(backend)
    if not model_ready(backend):
        return False, installation_hint([], "", backend)
    if not venv_ready():
        return False, installation_hint(
            [name for name, _ in requirements_for_backend(backend)],
            "",
            backend,
        )
    missing, error = check_runtime(python_executable, backend)
    if missing or error:
        return False, installation_hint(missing, error, backend)
    return True, ""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", choices=(SAM2_BACKEND, SAM1_BACKEND),
        default=DEFAULT_BACKEND)
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    ok, message = ensure_ready(args.python_executable, args.backend)
    if args.as_json:
        payload = {
            "ok": ok,
            "backend": args.backend,
            "python": args.python_executable or default_python_executable(),
            "model_path": default_model_path(args.backend),
            "config_path": default_config_path(args.backend),
            "message": message,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif ok:
        print("{} runtime ready: {}".format(
            args.backend.upper(), default_python_executable()))
    else:
        print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
