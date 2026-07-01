# -*- coding: utf-8 -*-
"""PyTorch 推理子进程入口。

该入口复用插件已有的 LCC_EVENT JSON-line 协议，但不初始化 QgsApplication。
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback


def _plugin_parent():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_import_path():
    parent = _plugin_parent()
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _emit(event, **payload):
    payload["event"] = event
    print("LCC_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


def _progress(stage, done, total, **extra):
    ranges = {
        "load": (0, 8),
        "dem": (8, 18),
        "predict": (18, 88),
        "postprocess": (88, 98),
        "write": (98, 100),
    }
    start, end = ranges.get(stage, (0, 100))
    ratio = min(1.0, max(0.0, float(done) / float(max(1, total))))
    payload = {
        "value": int(start + (end - start) * ratio),
        "stage": stage,
    }
    payload.update(extra)
    _emit("progress", **payload)


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

    _ensure_import_path()
    _write_debug_snapshot(args.debug_env)

    try:
        from land_cover_classification.pytorch_inference_core import (
            run_inference_from_file,
        )

        _emit("progress", value=0, stage="start")
        result = run_inference_from_file(args.params, progress_callback=_progress)
        _emit("progress", value=100, stage="done")
        _emit("done", **result)
        return 0
    except BaseException as exc:  # noqa: BLE001 - 子进程边界需要兜底。
        _emit("error", message=str(exc), traceback=traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(main())
