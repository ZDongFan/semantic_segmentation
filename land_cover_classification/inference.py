# -*- coding: utf-8 -*-
"""地物分类插件的后台推理任务。

`SegmenterTask` 仍以 QgsTask 形式组织推理流程，但实际由独立 Python
子进程直接调用 `run()` 执行，以隔离 PaddleRS / 原生库异常，避免拖垮
QGIS 主进程。当前流程:

  1. 可选的预处理链
  2. 加载 predictor(若可用则使用 GPU)
  3. 带地理坐标的 TIFF 走 `slider_predict`;普通 RGB 图像 resize 到
     512×512 推理后再按原尺寸回采样
  4. 输出单波段类别编号 GeoTIFF，供 QGIS 对话框后续矢量化、确认和导出
"""

import math
import os
import os.path as osp
import shutil
import subprocess
import sys
import tempfile

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
        device = paddle.device.get_device()
        try:
            device_id = int(device.split(":", 1)[1])
        except (IndexError, ValueError):
            device_id = 0

        try:
            output = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free",
                    "--format=csv,noheader,nounits",
                    "-i",
                    str(device_id),
                ],
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            gpu_mb = float(output.strip().splitlines()[0])
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            return 512
        if gpu_mb >= 10000:
            return 1024
        if gpu_mb >= 6000:
            return 768
        return 512
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

    def _set_progress(self, progress):
        self.setProgress(progress)
        if self.progress_callback is not None:
            try:
                self.progress_callback(progress)
            except Exception as exc:  # noqa: BLE001 - 进度回调失败不应中断推理。
                _log("进度回调执行失败:{}".format(exc), Qgis.Warning)

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

            use_gpu = paddle.device.is_compiled_with_cuda()
            _log("加载 predictor:{}(use_gpu={})".format(
                self.model_path, use_gpu))
            predictor = pdrs.deploy.Predictor(self.model_path, use_gpu=use_gpu)
            self._set_progress(30)
            if self.isCanceled():
                return False

            # 3. 推理
            if self.is_georef:
                self._run_georef(prepared, predictor, slider_predict)
            else:
                self._run_plain(prepared, predictor, cv2, np)

            if self.isCanceled():
                return False

            self._set_progress(100)
            return True
        except Exception as exc:  # noqa: BLE001
            self.exception = exc
            _log("SegmenterTask 失败:{}".format(exc), Qgis.Critical)
            return False

    def finished(self, result):
        # 无论成功失败都要清理临时目录。
        if self._temp_dir and osp.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _run_georef(self, prepared, predictor, slider_predict):
        """带地理坐标输入走滑窗预测，并保留原始地理参考。"""
        block_size = _adaptive_block_size()
        slider_dir = osp.join(self._temp_dir, "slider_out")
        os.makedirs(slider_dir, exist_ok=True)
        _log("执行 slider_predict(block_size={}, overlap=64)".format(
            block_size))
        total_blocks = self._estimate_slider_block_count(
            prepared, block_size, 64)
        completed_blocks = [0]
        last_progress = [30]

        def predict_with_progress(batch_data, transforms=None):
            result = predictor.predict(batch_data, transforms=transforms)
            completed_blocks[0] = min(
                total_blocks, completed_blocks[0] + len(batch_data))
            progress = 30 + int(49 * completed_blocks[0] /
                                max(1, total_blocks))
            progress = min(progress, 79)
            if progress > last_progress[0]:
                self._set_progress(progress)
                last_progress[0] = progress
            return result

        slider_predict(
            predict_func=predict_with_progress,
            img_file=prepared,
            save_dir=slider_dir,
            block_size=block_size,
            overlap=64,
            transforms=None,
            merge_strategy="keep_last",
            batch_size=1,
            invalid_value=0,
        )
        if self.isCanceled():
            return

        produced = osp.join(
            slider_dir, osp.splitext(osp.basename(prepared))[0] + ".tif")
        if not osp.exists(produced):
            raise IOError(
                "slider_predict 未生成预期的输出文件:{}".format(
                    produced))
        self._set_progress(80)

        self._write_georef_label_tiff(produced)
        self._set_progress(95)

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
        x_blocks = max(1, int(math.ceil(width / float(stride))))
        y_blocks = max(1, int(math.ceil(height / float(stride))))
        return x_blocks * y_blocks

    def _write_georef_label_tiff(self, label_map_path):
        """把滑窗输出的类别图写成单波段 GeoTIFF，并复制输入地理参考。"""
        from osgeo import gdal

        src = gdal.Open(self.input_path)
        if src is None:
            raise IOError("无法重新打开输入影像以读取地理参考信息:{}".format(
                self.input_path))
        gt = src.GetGeoTransform()
        proj = src.GetProjection()

        label_ds = gdal.Open(label_map_path)
        if label_ds is None:
            src = None
            raise IOError("无法打开 slider_predict 的输出:{}".format(
                label_map_path))

        driver = gdal.GetDriverByName("GTiff")
        dst = driver.Create(
            self.output_path,
            label_ds.RasterXSize,
            label_ds.RasterYSize,
            1,
            gdal.GDT_Byte,
            ["COMPRESS=LZW", "TILED=YES"],
        )
        if dst is None:
            src = None
            label_ds = None
            raise IOError("创建输出 GeoTIFF 失败:{}".format(self.output_path))
        if gt is not None:
            dst.SetGeoTransform(gt)
        if proj:
            dst.SetProjection(proj)

        data = label_ds.GetRasterBand(1).ReadAsArray()
        if data is None:
            src = None
            label_ds = None
            dst = None
            raise IOError("无法读取类别栅格:{}".format(label_map_path))
        dst.GetRasterBand(1).WriteArray(data.astype("uint8"))
        dst.FlushCache()
        src = None
        label_ds = None
        dst = None

    def _run_plain(self, prepared, predictor, cv2, np):
        """普通影像走整图预测，并把类别图回采样到原始尺寸。"""
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
        if isinstance(result, list):
            result = result[0]
        label_map = result["label_map"]
        label_map = cv2.resize(
            label_map.astype(np.uint8),
            (img.shape[1], img.shape[0]),
            interpolation=cv2.INTER_NEAREST)
        self._set_progress(80)

        self._write_plain_label_tiff(label_map, np)
        self._set_progress(95)

    def _write_plain_label_tiff(self, label_map, np):
        """把普通影像预测得到的类别图写成单波段 GeoTIFF。"""
        from osgeo import gdal

        height, width = label_map.shape[:2]
        driver = gdal.GetDriverByName("GTiff")
        dst = driver.Create(
            self.output_path,
            width,
            height,
            1,
            gdal.GDT_Byte,
            ["COMPRESS=LZW", "TILED=YES"],
        )
        if dst is None:
            raise IOError("无法创建类别 GeoTIFF:{}".format(
                self.output_path))
        dst.GetRasterBand(1).WriteArray(label_map.astype(np.uint8))
        dst.FlushCache()
        dst = None
