# -*- coding: utf-8 -*-
"""地物分类推理子进程入口。

PaddleRS 在 QGIS 主进程内运行时,底层原生库异常可能直接拖垮 QGIS。
本模块把推理放到独立 Python 进程中执行,并通过标准输出向插件回传进度。
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback


def _discover_osgeo4w_root():
    root = os.environ.get("OSGEO4W_ROOT")
    if root:
        return root
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.basename(exe_dir).lower() == "bin":
        root = os.path.dirname(exe_dir)
        if os.path.isdir(root):
            os.environ["OSGEO4W_ROOT"] = root
            return root
    return None


def _discover_qgis_prefix(root=None):
    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix and os.path.isdir(prefix):
        return prefix

    root = root or _discover_osgeo4w_root()
    apps_dir = os.path.join(root, "apps") if root else None
    if apps_dir and os.path.isdir(apps_dir):
        for name in sorted(os.listdir(apps_dir)):
            candidate = os.path.join(apps_dir, name)
            if name.lower().startswith("qgis") and os.path.isdir(candidate):
                os.environ["QGIS_PREFIX_PATH"] = candidate
                return candidate

    for candidate in (
            "/usr",
            "/usr/local",
            "/opt/qgis",
            "/Applications/QGIS.app/Contents/MacOS",
            "/Applications/QGIS.app/Contents/Resources",
    ):
        if os.path.isdir(os.path.join(candidate, "share", "qgis")):
            os.environ["QGIS_PREFIX_PATH"] = candidate
            return candidate
    return None


def _discover_qgis_python_path(prefix=None):
    prefix = prefix or _discover_qgis_prefix()
    candidates = [
        os.environ.get("QGIS_PYTHON_PATH"),
        os.path.join(prefix or "", "python"),
        os.path.join(prefix or "", "share", "qgis", "python"),
        "/usr/share/qgis/python",
        "/usr/local/share/qgis/python",
        "/Applications/QGIS.app/Contents/Resources/python",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _add_vendor_path():
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    plugin_parent = os.path.dirname(plugin_dir)
    sys.path[:] = [
        path for path in sys.path
        if os.path.abspath(path or os.getcwd()) != plugin_dir
    ]
    if plugin_parent not in sys.path:
        sys.path.insert(0, plugin_parent)
    vendor = os.path.join(plugin_dir, "vendor", "PaddleRS")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)


def _add_qgis_dll_dirs():
    if os.name != "nt":
        return
    root = _discover_osgeo4w_root()
    if not root or not hasattr(os, "add_dll_directory"):
        return

    qgis_prefix = _discover_qgis_prefix(root)
    paths = [
        os.path.join(root, "bin"),
        os.path.join(root, "apps", "Qt5", "bin"),
        os.path.join(qgis_prefix, "bin") if qgis_prefix else None,
    ]
    for path in paths:
        if path and os.path.isdir(path):
            os.add_dll_directory(path)


def _add_qgis_python_path():
    qgis_prefix = _discover_qgis_prefix()
    qgis_python = _discover_qgis_python_path(qgis_prefix)
    if qgis_python and qgis_python not in sys.path:
        sys.path.insert(0, qgis_python)


def _emit(event, **payload):
    payload["event"] = event
    print("LCC_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


def _progress(value):
    _emit("progress", value=int(value))


def _write_debug_snapshot(path):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("executable={}\n".format(sys.executable))
            handle.write("cwd={}\n".format(os.getcwd()))
            handle.write("argv={}\n".format(sys.argv))
            for key in sorted(os.environ):
                handle.write("env:{}={}\n".format(key, os.environ[key]))
            handle.write("sys.path={}\n".format(sys.path))
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", required=True)
    parser.add_argument("--debug-env")
    args = parser.parse_args(argv)

    _add_qgis_dll_dirs()
    _add_qgis_python_path()
    _add_vendor_path()
    _write_debug_snapshot(args.debug_env)

    with open(args.params, "r", encoding="utf-8-sig") as handle:
        params = json.load(handle)

    from qgis.core import QgsApplication

    qgis_app = None
    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    qgis_app = QgsApplication([], False)
    qgis_app.initQgis()

    from land_cover_classification.inference import SegmenterTask

    try:
        task = SegmenterTask(
            "地物分类推理子进程",
            model_path=params["model_path"],
            input_path=params["input_path"],
            output_path=params["output_path"],
            preprocess_flags=params.get("preprocess_flags") or {},
            is_georef=bool(params.get("is_georef")),
            progress_callback=_progress,
        )
        ok = task.run()
        task.finished(ok)
        if ok:
            _emit("done", output_path=params["output_path"])
            return 0
        message = str(task.exception) if task.exception else "推理失败。"
        _emit("error", message=message)
        return 1
    except BaseException as exc:  # noqa: BLE001 - 子进程边界,兜底捕获。
        _emit("error", message=str(exc), traceback=traceback.format_exc())
        return 2
    finally:
        if qgis_app is not None:
            qgis_app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
