# -*- coding: utf-8 -*-
"""地物分类插件的后台推理任务。

`SegmenterTask` 在 QgsTask 中运行 PaddleRS 推理,因此 QGIS 主线程
(以及地图画布)不会被阻塞。流程:

  1. 可选的预处理链
  2. 加载 predictor(若可用则使用 GPU)
  3. 带地理坐标的 TIFF 走 `slider_predict`;普通 RGB 图像 resize 到
     512×512 再走一次 `predict()`
  4. 用 PaddleRS 自带的 256 色 LUT 给标签图上色
  5. 写出结果(带地理坐标输入对应输出 GeoTIFF,否则输出 PNG/TIFF)
"""

import math
import os
import os.path as osp
import shutil
import sys
import tempfile
import threading
import time

from qgis.core import QgsTask, QgsMessageLog, Qgis


_LOG_TAG = "LandCoverClassification"


def _log(message, level=Qgis.Info):
    QgsMessageLog.logMessage(message, _LOG_TAG, level)


class _NullStream:
    """提供最小 write/flush 接口的空流对象。"""

    def write(self, *_args, **_kwargs):
        return 0

    def flush(self):
        return None


def _ensure_std_streams():
    # 修复问题:
    # 1. QGIS 重开后, QgsTask 后台线程中的 sys.stdout / sys.stderr 可能为 None。
    # 2. PaddleRS 在模型加载早期会调用 sys.stdout.flush() 输出 warning/info。
    # 3. 当标准流为 None 时, 会在后台任务里触发
    #    "'NoneType' object has no attribute 'flush'" 并导致推理提前失败。
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()


def _adaptive_block_size():
    """根据可用 GPU 显存自适应选择滑窗块大小。"""
    try:
        import paddle
    except ImportError:
        return 512

    if (paddle.device.is_compiled_with_cuda()
            and paddle.device.get_device().startswith("gpu")):
        gpu_mb = paddle.device.cuda.max_memory_allocated() / 1024 / 1024
        if gpu_mb <= 0:
            return 512
        ratio = 0.3
        base_mb = 8192
        base_block = 512
        calculated = int(base_block * math.sqrt((gpu_mb * ratio) / base_mb))
        return max(256, (calculated // 32) * 32)
    return 512


def is_georeferenced(image_path):
    """如果 `image_path` 带有非单位仿射的地理变换,返回 True。"""
    try:
        from osgeo import gdal
    except ImportError:
        return False
    ds = gdal.Open(image_path)
    if ds is None:
        return False
    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    ds = None
    if gt is None:
        return False
    # GDAL 的默认单位仿射是 (0, 1, 0, 0, 0, 1)。某些文件仅有投影但仿射为
    # 单位矩阵;两种情况都视为「带地理坐标」。
    if gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        return True
    return bool(proj)


class SegmenterTask(QgsTask):
    """在后台执行一次 PaddleRS 分割推理。"""

    def __init__(self, description, model_path, input_path, output_path,
                 preprocess_flags, is_georef, progress_callback=None):
        super().__init__(description, QgsTask.CanCancel)
        self.model_path = model_path
        self.input_path = input_path
        self.output_path = output_path
        self.preprocess_flags = preprocess_flags or {}
        self.is_georef = is_georef
        self.progress_callback = progress_callback
        self.exception = None
        self._temp_dir = None

    # --------------------------------------------------------------- QgsTask
    def _set_progress(self, progress):
        self.setProgress(progress)
        if self.progress_callback is not None:
            try:
                self.progress_callback(progress)
            except Exception as exc:  # noqa: BLE001 - progress is best effort.
                _log("Progress callback failed:{}".format(exc), Qgis.Warning)

    def run(self):
        try:
            _ensure_std_streams()
            self._temp_dir = tempfile.mkdtemp(prefix="lcc_")
            self._set_progress(2)

            # 1. 可选预处理
            from . import preprocess
            self._set_progress(5)
            if self.isCanceled():
                return False
            prepared = preprocess.apply_chain(
                self.input_path, self.preprocess_flags, self._temp_dir)
            self._set_progress(15)
            if self.isCanceled():
                return False

            # 2. 延迟导入重依赖(避免插件加载阶段就触发依赖检查失败)
            import cv2
            import numpy as np
            import paddle
            import paddlers as pdrs
            from paddlers.tasks.utils.slider_predict import slider_predict
            from paddlers.tasks.utils.visualize import get_color_map_list

            use_gpu = paddle.device.is_compiled_with_cuda()
            _log("加载 predictor:{}(use_gpu={})".format(
                self.model_path, use_gpu))
            predictor = pdrs.deploy.Predictor(self.model_path, use_gpu=use_gpu)
            self._set_progress(30)
            if self.isCanceled():
                return False

            lut = np.array(get_color_map_list(256), dtype=np.uint8)

            # 3. 推理
            if self.is_georef:
                self._run_georef(prepared, predictor, slider_predict, lut)
            else:
                self._run_plain(prepared, predictor, lut, cv2)

            if self.isCanceled():
                return False

            self._set_progress(100)
            return True
        except Exception as exc:  # noqa: BLE001 — 将异常传给 UI 线程显示
            self.exception = exc
            _log("SegmenterTask 失败:{}".format(exc), Qgis.Critical)
            return False

    def finished(self, result):
        # 无论成功失败都要清理临时目录。
        if self._temp_dir and osp.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    # --------------------------------------------------------- 带地理坐标分支
    def _run_georef(self, prepared, predictor, slider_predict, lut):
        block_size = _adaptive_block_size()
        slider_dir = osp.join(self._temp_dir, "slider_out")
        os.makedirs(slider_dir, exist_ok=True)
        _log("执行 slider_predict(block_size={}, overlap=64)".format(
            block_size))
        progress_stop = threading.Event()
        progress_thread = threading.Thread(
            target=self._report_slider_progress,
            args=(prepared, block_size, 64, progress_stop),
        )
        progress_thread.daemon = True
        progress_thread.start()
        try:
            slider_predict(
                predict_func=predictor.predict,
                img_file=prepared,
                save_dir=slider_dir,
                block_size=block_size,
                overlap=64,
                transforms=None,
                merge_strategy="keep_last",
                batch_size=1,
                invalid_value=0,
            )
        finally:
            progress_stop.set()
            progress_thread.join(1.0)
        if self.isCanceled():
            return

        produced = osp.join(
            slider_dir, osp.splitext(osp.basename(prepared))[0] + ".tif")
        if not osp.exists(produced):
            raise IOError(
                "slider_predict 未生成预期的输出文件:{}".format(
                    produced))
        self._set_progress(80)

        self._write_georef_tiff(produced, lut)
        self._set_progress(95)

    def _report_slider_progress(self, image_path, block_size, overlap, stop_event):
        """在 PaddleRS slider_predict 运行期间回报估算进度。"""
        total_blocks = self._estimate_slider_block_count(
            image_path, block_size, overlap)
        if total_blocks <= 0:
            total_blocks = 1
        # 当前 PaddleRS slider_predict 没有暴露逐窗口回调。这里让界面保持推进，
        # 但把 80-100% 留给已确认完成的滑窗推理和写出阶段。
        estimated_seconds = max(8.0, min(300.0, total_blocks * 0.8))
        start_time = time.time()
        last_progress = 30
        while not stop_event.wait(1.0):
            if self.isCanceled():
                return
            elapsed = time.time() - start_time
            ratio = 1.0 - math.exp(-elapsed / estimated_seconds)
            progress = 30 + int(49 * ratio)
            progress = max(last_progress, min(progress, 79))
            if progress > last_progress:
                self._set_progress(progress)
                last_progress = progress

    def _estimate_slider_block_count(self, image_path, block_size, overlap):
        try:
            from osgeo import gdal
        except ImportError:
            return 0
        ds = gdal.Open(image_path)
        if ds is None:
            return 0
        width = ds.RasterXSize
        height = ds.RasterYSize
        ds = None
        stride = max(1, block_size - overlap)
        x_blocks = max(1, int(math.ceil(max(1, width - overlap) / stride)))
        y_blocks = max(1, int(math.ceil(max(1, height - overlap) / stride)))
        return x_blocks * y_blocks

    def _write_georef_tiff(self, label_map_path, lut):
        """分块读取标签 GeoTIFF 并写出 3 波段 RGB GeoTIFF。"""
        from osgeo import gdal

        src = gdal.Open(self.input_path)
        if src is None:
            raise IOError("重新打开输入获取地理元数据失败:{}".format(
                self.input_path))
        gt = src.GetGeoTransform()
        proj = src.GetProjection()
        label_ds = gdal.Open(label_map_path)
        if label_ds is None:
            src = None
            raise IOError("无法打开 slider_predict 的输出:{}".format(
                label_map_path))

        label_band = label_ds.GetRasterBand(1)
        width = label_ds.RasterXSize
        height = label_ds.RasterYSize

        driver = gdal.GetDriverByName("GTiff")
        dst = driver.Create(self.output_path, width, height, 3,
                            gdal.GDT_Byte,
                            ["COMPRESS=LZW", "TILED=YES"])
        if dst is None:
            src = None
            label_ds = None
            raise IOError("创建输出 GeoTIFF 失败:{}".format(self.output_path))
        if gt is not None:
            dst.SetGeoTransform(gt)
        if proj:
            dst.SetProjection(proj)

        chunk_size = 1024
        total_rows = max(1, height)
        out_bands = [dst.GetRasterBand(i + 1) for i in range(3)]

        for yoff in range(0, height, chunk_size):
            ysize = min(chunk_size, height - yoff)
            for xoff in range(0, width, chunk_size):
                xsize = min(chunk_size, width - xoff)
                label_block = label_band.ReadAsArray(xoff, yoff, xsize, ysize)
                if label_block is None:
                    dst = None
                    label_ds = None
                    src = None
                    raise IOError(
                        "读取 slider_predict 输出块失败:{} ({}, {}, {}, {})".
                        format(label_map_path, xoff, yoff, xsize, ysize))

                color_block = lut[label_block]
                for idx, out_band in enumerate(out_bands):
                    out_band.WriteArray(color_block[:, :, idx], xoff, yoff)

            progress = 80 + int(15 * (yoff + ysize) / total_rows)
            self._set_progress(min(progress, 95))

        dst.FlushCache()

    # ---------------------------------------------------------- 普通图像分支
    def _run_plain(self, prepared, predictor, lut, cv2):
        img = cv2.imread(prepared, cv2.IMREAD_COLOR)
        if img is None:
            raise IOError("cv2 无法读取影像:{}".format(prepared))
        resized = cv2.resize(img, (512, 512))
        self._set_progress(50)
        if self.isCanceled():
            return

        result = predictor.predict(resized)
        if self.isCanceled():
            return
        # 单个 ndarray 输入时 PaddleRS 可能返回包含单元素的列表,这里统一展开。
        if isinstance(result, list):
            result = result[0]
        label_map = result["label_map"]
        self._set_progress(80)

        color_rgb = lut[label_map]
        color_bgr = cv2.cvtColor(color_rgb, cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(self.output_path, color_bgr):
            raise IOError("cv2.imwrite 写出失败:{}".format(self.output_path))
        self._set_progress(95)
