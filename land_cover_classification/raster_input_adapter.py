# -*- coding: utf-8 -*-
"""统一管理 ENVI、ERDAS 及常规栅格的输入适配。"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile


SUPPORTED_SPECIAL_DRIVERS = {"ENVI", "HFA"}
BIGTIFF_CREATION_OPTIONS = ("COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES")
SIDECAR_SUFFIXES = (
    ".hdr", ".ige", ".rrd", ".aux", ".aux.xml", ".ovr", ".xml",
)


class RasterInputError(RuntimeError):
    """表示输入定位、能力探测或转换失败。"""


def _plain_path(path):
    value = os.fspath(path).strip() if path is not None else ""
    return value.split("|", 1)[0] if value else ""


def resolve_source_path(path):
    """规范化本地路径，并把 .ige 入口解析为同名 .img 主文件。"""
    value = _plain_path(path)
    if not value:
        raise RasterInputError("输入影像路径为空。")
    value = os.path.abspath(value)
    if os.path.splitext(value)[1].lower() != ".ige":
        return value
    directory = os.path.dirname(value)
    stem = os.path.splitext(os.path.basename(value))[0]
    exact = os.path.join(directory, stem + ".img")
    if os.path.isfile(exact):
        return exact
    try:
        matches = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if os.path.splitext(name)[0].casefold() == stem.casefold()
            and os.path.splitext(name)[1].casefold() == ".img"
            and os.path.isfile(os.path.join(directory, name))
        ]
    except OSError as exc:
        raise RasterInputError(
            "无法枚举 .ige 所在目录 {}: {}".format(directory, exc)) from exc
    if len(matches) == 1:
        return os.path.abspath(matches[0])
    if not matches:
        raise RasterInputError(
            "ERDAS .ige 不是独立栅格，未找到同目录同名 .img 主文件: {}".format(
                value))
    raise RasterInputError(
        "ERDAS .ige 对应多个大小写不同的 .img 主文件，无法确定应使用哪一个: {}".format(
            ", ".join(sorted(matches))))


def _dataset_metadata(dataset, gdal_module):
    driver = dataset.GetDriver()
    bands = []
    color_interpretations = []
    uncompressed_bytes = 0
    for index in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(index)
        bits = int(gdal_module.GetDataTypeSize(band.DataType) or 8)
        uncompressed_bytes += (
            int(dataset.RasterXSize) * int(dataset.RasterYSize)
            * max(1, (bits + 7) // 8))
        color_name = gdal_module.GetColorInterpretationName(
            band.GetColorInterpretation()) or "Undefined"
        color_interpretations.append(str(color_name))
        bands.append({
            "index": index,
            "data_type": gdal_module.GetDataTypeName(band.DataType),
            "nodata": band.GetNoDataValue(),
            "scale": band.GetScale(),
            "offset": band.GetOffset(),
            "color_interpretation": str(color_name),
        })
    return {
        "driver": driver.ShortName if driver is not None else "",
        "driver_long_name": driver.LongName if driver is not None else "",
        "width": int(dataset.RasterXSize),
        "height": int(dataset.RasterYSize),
        "band_count": int(dataset.RasterCount),
        "bands": bands,
        "color_interpretations": color_interpretations,
        "uncompressed_bytes": int(uncompressed_bytes),
        "gdal_version": gdal_module.VersionInfo("RELEASE_NAME"),
        "projection": dataset.GetProjection() or "",
        "geotransform": list(dataset.GetGeoTransform()),
    }


def inspect_with_qgis_gdal(path):
    """使用 QGIS 进程内 GDAL 打开数据集并读取基础元数据。"""
    from osgeo import gdal

    source_path = resolve_source_path(path)
    if not os.path.isfile(source_path):
        raise RasterInputError("输入影像不存在: {}".format(source_path))
    dataset = None
    try:
        gdal.UseExceptions()
        dataset = gdal.Open(source_path, gdal.GA_ReadOnly)
        if dataset is None:
            raise RasterInputError("GDAL 未返回可用数据集。")
        metadata = _dataset_metadata(dataset, gdal)
        if metadata["width"] <= 0 or metadata["height"] <= 0:
            raise RasterInputError("栅格尺寸无效。")
        if metadata["band_count"] <= 0:
            raise RasterInputError("栅格不包含可读取波段。")
        extension = os.path.splitext(source_path)[1].lower()
        if extension == ".dat" and metadata["driver"] != "ENVI":
            raise RasterInputError(
                ".dat 输入必须由 GDAL 识别为 ENVI，实际驱动为 {}。".format(
                    metadata["driver"] or "未知"))
        if extension == ".img" and metadata["driver"] not in SUPPORTED_SPECIAL_DRIVERS:
            raise RasterInputError(
                ".img 输入必须由 GDAL 识别为 ENVI 或 HFA，实际驱动为 {}。".format(
                    metadata["driver"] or "未知"))
        metadata["source_path"] = source_path
        return metadata
    except RasterInputError:
        raise
    except Exception as exc:
        extension = os.path.splitext(source_path)[1].lower()
        companion = "对应 .hdr" if extension in (".dat", ".img") else "伴随文件"
        raise RasterInputError(
            "QGIS GDAL 无法打开主文件 {}（请检查 {}）：{}".format(
                source_path, companion, exc)) from exc
    finally:
        dataset = None


def _runtime_probe_payload(path):
    """在统一 runtime 中实际打开并读取一个有限窗口。"""
    import rasterio
    from rasterio.windows import Window

    source_path = os.path.abspath(path)
    with rasterio.open(source_path) as dataset:
        if dataset.width <= 0 or dataset.height <= 0 or dataset.count <= 0:
            raise RasterInputError("runtime 读取到的栅格尺寸或波段数无效。")
        width = min(32, int(dataset.width))
        height = min(32, int(dataset.height))
        indexes = list(range(1, min(3, int(dataset.count)) + 1))
        sample = dataset.read(
            indexes=indexes,
            window=Window(0, 0, width, height),
            masked=True,
        )
        if sample.shape != (len(indexes), height, width):
            raise RasterInputError("runtime 有限窗口读取结果尺寸异常。")
        return {
            "ok": True,
            "driver": dataset.driver or "",
            "width": int(dataset.width),
            "height": int(dataset.height),
            "band_count": int(dataset.count),
            "color_interpretations": [
                getattr(value, "name", str(value))
                for value in dataset.colorinterp
            ],
            "gdal_version": getattr(rasterio, "__gdal_version__", ""),
            "rasterio_version": getattr(rasterio, "__version__", ""),
            "sample_shape": list(sample.shape),
        }


def probe_runtime(python_executable, path, environment=None, timeout=60):
    """通过清洁统一 runtime 子进程探测数据集实际读取能力。"""
    command = [
        python_executable,
        os.path.abspath(__file__),
        "--probe",
        os.path.abspath(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": "runtime 探测进程失败: {}".format(exc)}
    output = result.stdout.strip()
    try:
        payload = json.loads(output or "{}")
    except ValueError:
        payload = {
            "ok": False,
            "error": output or result.stderr.strip() or "runtime 探测未返回 JSON。",
        }
    if result.returncode != 0:
        payload["ok"] = False
        payload.setdefault(
            "error", result.stderr.strip() or "runtime 无法读取输入影像。")
    return payload


def _companion_paths(source_path):
    directory = os.path.dirname(source_path)
    filename = os.path.basename(source_path)
    stem = os.path.splitext(filename)[0]
    candidates = {source_path}
    try:
        for name in os.listdir(directory):
            lower = name.casefold()
            same_stem = os.path.splitext(name)[0].casefold() == stem.casefold()
            source_prefix = lower.startswith(filename.casefold() + ".")
            if same_stem or source_prefix:
                if any(lower.endswith(suffix) for suffix in SIDECAR_SUFFIXES):
                    candidates.add(os.path.join(directory, name))
    except OSError:
        pass
    return sorted(path for path in candidates if os.path.isfile(path))


def source_fingerprint(source_path):
    """按主文件和伴随文件的路径、大小、修改时间生成稳定指纹。"""
    records = []
    for path in _companion_paths(resolve_source_path(source_path)):
        stat = os.stat(path)
        records.append({
            "path": os.path.normcase(os.path.abspath(path)),
            "size": int(stat.st_size),
            "mtime_ns": int(getattr(stat, "st_mtime_ns", stat.st_mtime * 1e9)),
        })
    encoded = json.dumps(
        records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def required_conversion_space(metadata):
    """按未压缩大小加安全余量估算转换所需临时空间。"""
    raw = int(metadata.get("uncompressed_bytes") or 0)
    return max(64 * 1024 * 1024, int(raw * 1.25) + 32 * 1024 * 1024)


def convert_to_bigtiff(source_path, output_path, metadata=None,
                       is_canceled=None, progress_callback=None):
    """使用 QGIS GDAL CreateCopy 流式转换，并在成功校验后原子发布。"""
    from osgeo import gdal

    source_path = resolve_source_path(source_path)
    output_path = os.path.abspath(output_path)
    part_path = output_path + ".part.tif"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    metadata = metadata or inspect_with_qgis_gdal(source_path)
    required = required_conversion_space(metadata)
    free = shutil.disk_usage(os.path.dirname(output_path)).free
    if free < required:
        raise RasterInputError(
            "临时目录空间不足：预计至少需要 {:.2f} GiB，当前可用 {:.2f} GiB。".format(
                required / (1024.0 ** 3), free / (1024.0 ** 3)))
    try:
        os.remove(part_path)
    except FileNotFoundError:
        pass

    def _progress(complete, _message, _data):
        if progress_callback is not None:
            progress_callback(float(complete) * 100.0)
        return 0 if is_canceled is not None and is_canceled() else 1

    source = None
    converted = None
    try:
        gdal.UseExceptions()
        source = gdal.Open(source_path, gdal.GA_ReadOnly)
        if source is None:
            raise RasterInputError("转换阶段无法重新打开源数据集。")
        driver = gdal.GetDriverByName("GTiff")
        if driver is None:
            raise RasterInputError("QGIS GDAL 缺少 GTiff 驱动，无法执行 fallback 转换。")
        converted = driver.CreateCopy(
            part_path,
            source,
            strict=0,
            options=list(BIGTIFF_CREATION_OPTIONS),
            callback=_progress,
        )
        if is_canceled is not None and is_canceled():
            raise RasterInputError("输入影像转换已取消。")
        if converted is None:
            raise RasterInputError("GDAL CreateCopy 未生成转换结果。")
        converted.FlushCache()
        converted = None
        source = None
        check = gdal.Open(part_path, gdal.GA_ReadOnly)
        if check is None:
            raise RasterInputError("转换后的临时 GeoTIFF 无法重新打开。")
        if (check.RasterXSize != metadata["width"]
                or check.RasterYSize != metadata["height"]
                or check.RasterCount != metadata["band_count"]):
            raise RasterInputError("转换后的 GeoTIFF 尺寸或波段数与源数据不一致。")
        check = None
        os.replace(part_path, output_path)
        return output_path
    except Exception:
        try:
            os.remove(part_path)
        except OSError:
            pass
        raise
    finally:
        converted = None
        source = None


class RasterInputSession(object):
    """维护单个插件会话中的转换缓存与活动任务。"""

    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="lcc_input_adapter_")
        self._cache = {}
        self.active_task = None
        self._retired_directories = []

    def cached_path(self, source_path):
        fingerprint = source_fingerprint(source_path)
        path = self._cache.get(fingerprint)
        return path if path and os.path.isfile(path) else None

    def output_path(self, source_path):
        fingerprint = source_fingerprint(source_path)
        path = os.path.join(self.directory, "{}.tif".format(fingerprint))
        return path, fingerprint

    def remember(self, fingerprint, path):
        self._cache[fingerprint] = os.path.abspath(path)

    def cancel(self):
        if self.active_task is not None:
            self.active_task.cancel()

    def clear(self):
        self.cancel()
        self._cache.clear()
        self._retired_directories.append(self.directory)
        self.directory = tempfile.mkdtemp(prefix="lcc_input_adapter_")
        self._cleanup_retired()

    def close(self):
        self.cancel()
        self._cache.clear()
        self._retired_directories.append(self.directory)
        self._cleanup_retired()

    def _cleanup_retired(self):
        if self.active_task is not None:
            return
        for directory in self._retired_directories:
            shutil.rmtree(directory, ignore_errors=True)
        self._retired_directories = []


def create_conversion_task(session, source_path, metadata, on_finished):
    """创建可取消的 QgsTask；完成回调接收 (path, error)。"""
    from qgis.core import QgsTask

    output_path, fingerprint = session.output_path(source_path)

    class _RasterConversionTask(QgsTask):
        def __init__(self):
            super().__init__(
                "转换 ENVI/ERDAS 输入为临时 BigTIFF", QgsTask.CanCancel)
            self.output_path = None
            self.error = None

        def run(self):
            try:
                self.output_path = convert_to_bigtiff(
                    source_path,
                    output_path,
                    metadata=metadata,
                    is_canceled=self.isCanceled,
                    progress_callback=self.setProgress,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - QgsTask 边界需要完整诊断。
                self.error = str(exc)
                return False

        def finished(self, success):
            session.active_task = None
            session._cleanup_retired()
            if success and self.output_path:
                session.remember(fingerprint, self.output_path)
                on_finished(self.output_path, None)
            else:
                on_finished(None, self.error or "输入影像转换失败或已取消。")

    task = _RasterConversionTask()
    session.active_task = task
    return task


def _main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe")
    args = parser.parse_args(argv)
    if not args.probe:
        parser.error("必须提供 --probe")
    try:
        payload = _runtime_probe_payload(args.probe)
        code = 0
    except BaseException as exc:  # noqa: BLE001 - 子进程边界需要输出结构化错误。
        payload = {"ok": False, "error": str(exc)}
        code = 2
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(_main())
