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
import tempfile

from qgis.core import QgsTask, QgsMessageLog, Qgis


_LOG_TAG = "LandCoverClassification"


def _log(message, level=Qgis.Info):
    QgsMessageLog.logMessage(message, _LOG_TAG, level)


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
                 preprocess_flags, is_georef):
        super().__init__(description, QgsTask.CanCancel)
        self.model_path = model_path
        self.input_path = input_path
        self.output_path = output_path
        self.preprocess_flags = preprocess_flags or {}
        self.is_georef = is_georef
        self.exception = None
        self._temp_dir = None

    # --------------------------------------------------------------- QgsTask
    def run(self):
        try:
            self._temp_dir = tempfile.mkdtemp(prefix="lcc_")
            self.setProgress(2)

            # 1. 可选预处理
            from . import preprocess
            self.setProgress(5)
            if self.isCanceled():
                return False
            prepared = preprocess.apply_chain(
                self.input_path, self.preprocess_flags, self._temp_dir)
            self.setProgress(15)
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
            self.setProgress(30)
            if self.isCanceled():
                return False

            lut = np.array(get_color_map_list(256), dtype=np.uint8)

            # 3. 推理
            if self.is_georef:
                self._run_georef(prepared, predictor, slider_predict, lut, cv2)
            else:
                self._run_plain(prepared, predictor, lut, cv2)

            self.setProgress(100)
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
    def _run_georef(self, prepared, predictor, slider_predict, lut, cv2):
        block_size = _adaptive_block_size()
        slider_dir = osp.join(self._temp_dir, "slider_out")
        os.makedirs(slider_dir, exist_ok=True)
        _log("执行 slider_predict(block_size={}, overlap=64)".format(
            block_size))
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
        if self.isCanceled():
            return

        produced = osp.join(
            slider_dir, osp.splitext(osp.basename(prepared))[0] + ".tif")
        if not osp.exists(produced):
            raise IOError(
                "slider_predict 未生成预期的输出文件:{}".format(
                    produced))
        self.setProgress(80)

        label_map = cv2.imread(produced, cv2.IMREAD_UNCHANGED)
        if label_map is None:
            raise IOError("无法读取 slider_predict 的输出:{}".format(
                produced))
        color_rgb = lut[label_map]
        self._write_georef_tiff(color_rgb)
        self.setProgress(95)

    def _write_georef_tiff(self, color_rgb):
        """写出 3 波段 RGB GeoTIFF,继承输入影像的地理元数据。"""
        from osgeo import gdal
        import numpy as np

        src = gdal.Open(self.input_path)
        if src is None:
            raise IOError("重新打开输入获取地理元数据失败:{}".format(
                self.input_path))
        gt = src.GetGeoTransform()
        proj = src.GetProjection()
        src = None

        height, width = color_rgb.shape[:2]
        driver = gdal.GetDriverByName("GTiff")
        dst = driver.Create(self.output_path, width, height, 3,
                            gdal.GDT_Byte,
                            ["COMPRESS=LZW", "TILED=YES"])
        if gt is not None:
            dst.SetGeoTransform(gt)
        if proj:
            dst.SetProjection(proj)
        # color_rgb 形状为 HxWx3,通道顺序为 RGB
        for i in range(3):
            dst.GetRasterBand(i + 1).WriteArray(color_rgb[:, :, i])
        dst.FlushCache()
        dst = None

    # ---------------------------------------------------------- 普通图像分支
    def _run_plain(self, prepared, predictor, lut, cv2):
        img = cv2.imread(prepared, cv2.IMREAD_COLOR)
        if img is None:
            raise IOError("cv2 无法读取影像:{}".format(prepared))
        resized = cv2.resize(img, (512, 512))
        self.setProgress(50)
        if self.isCanceled():
            return

        result = predictor.predict(resized)
        # 单个 ndarray 输入时 PaddleRS 可能返回包含单元素的列表,这里统一展开。
        if isinstance(result, list):
            result = result[0]
        label_map = result["label_map"]
        self.setProgress(80)

        color_rgb = lut[label_map]
        color_bgr = cv2.cvtColor(color_rgb, cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(self.output_path, color_bgr):
            raise IOError("cv2.imwrite 写出失败:{}".format(self.output_path))
        self.setProgress(95)
