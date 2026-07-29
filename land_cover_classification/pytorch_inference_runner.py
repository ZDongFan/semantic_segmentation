# -*- coding: utf-8 -*-
"""PyTorch 推理子进程入口。

该入口复用插件已有的 LCC_EVENT JSON-line 协议，但不初始化 QgsApplication。
"""

from __future__ import print_function

import argparse
import json
import locale
import logging
import os
import sys
import traceback


DEFAULT_DIAGNOSTIC_COUNT_LIMIT = 256
DEFAULT_DIAGNOSTIC_CHAR_LIMIT = 64 * 1024


class BoundedDiagnostics:
    """保存有界、去重的子进程诊断，避免重复警告长期占用 QGIS 内存。"""

    def __init__(self, count_limit=DEFAULT_DIAGNOSTIC_COUNT_LIMIT,
                 char_limit=DEFAULT_DIAGNOSTIC_CHAR_LIMIT):
        self.count_limit = max(1, int(count_limit))
        self.char_limit = max(256, int(char_limit))
        self._lines = []
        self._seen = set()
        self._chars = 0
        self.duplicate_count = 0
        self.dropped_count = 0

    def append(self, line):
        line = str(line)
        key = line if len(line) <= self.char_limit else (len(line), hash(line))
        if key in self._seen:
            self.duplicate_count += 1
            return False
        if len(self._lines) >= self.count_limit:
            self.dropped_count += 1
            return False
        remaining = self.char_limit - self._chars
        if remaining <= 0:
            self.dropped_count += 1
            return False
        if len(line) > remaining:
            marker = "…[单行诊断已截断]"
            line = line[:max(0, remaining - len(marker))] + marker
            self.dropped_count += 1
        self._lines.append(line)
        self._seen.add(key)
        self._chars += len(line)
        return True

    def as_lines(self):
        lines = list(self._lines)
        if self.duplicate_count or self.dropped_count:
            lines.append(
                "[诊断日志已限流：去重 {} 行，因数量或容量上限省略 {} 行]".format(
                    self.duplicate_count, self.dropped_count))
        return lines

    def __bool__(self):
        return bool(self._lines or self.duplicate_count or self.dropped_count)


def decode_process_line(data):
    """按协议 UTF-8 优先解码，并兼容 Windows 本地编码的 GDAL 原生日志。"""
    if isinstance(data, str):
        return data
    data = bytes(data)
    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower().replace("-", "") != "utf8":
        encodings.append(preferred)
    if os.name == "nt" and "mbcs" not in encodings:
        encodings.append("mbcs")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="backslashreplace")


def _configure_process_io():
    """固定协议输出编码，并阻止第三方日志编码异常打印递归 traceback。"""
    logging.raiseExceptions = False
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _plugin_parent():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_import_path():
    parent = _plugin_parent()
    if parent not in sys.path:
        sys.path.insert(0, parent)


def _emit(event, **payload):
    payload["event"] = event
    print("LCC_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


def _exception_details(exc):
    """按外层到根因顺序展开异常链，供 QGIS 主进程显示和记录。"""
    details = []
    visited = set()
    current = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        name = "{}.{}".format(
            type(current).__module__, type(current).__name__)
        details.append("{}: {}".format(name, current))
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause
    return details


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
    _configure_process_io()
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
        _emit(
            "error",
            message=str(exc),
            details=_exception_details(exc),
            traceback=traceback.format_exc(),
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
