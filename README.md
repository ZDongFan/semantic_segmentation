# LandCoverClassification 地物分类 QGIS 插件

这是一个面向 QGIS 3.28 LTR 的地物分类插件，可对当前活动栅格图层或本地影像执行基于 PaddleRS 的遥感语义分割，并将结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工确认流程。

插件主体位于 `land_cover_classification/` 目录下，默认自带 PaddleRS 运行时代码、模型目录和相关测试资源。可用模型默认放在 `land_cover_classification/models/semantic_segmentation/`。

## 主要功能

- 自动判断输入是否带地理坐标。
- 带地理参考的 GeoTIFF 使用 PaddleRS `slider_predict` 推理，并保留原始 CRS 和 GeoTransform。
- `slider_predict` 的 `block_size` 会根据当前 GPU 空闲显存自适应选择，不是固定值。
- 普通 JPG/PNG/TIF 会先 resize 到 `512 x 512`，推理后再按原始尺寸使用最近邻回采样。
- 支持可选预处理链：CLAHE、锐化、中值滤波、高斯滤波。
- 后台子进程执行推理，避免卡住 QGIS 主界面。
- 自动扫描 `land_cover_classification/models/semantic_segmentation/` 下的分割模型并填充下拉框。
- 推理阶段输出单波段类别 GeoTIFF，供后续矢量化与人工确认使用。
- 自动将类别栅格矢量化为草稿图层，并支持确认后写出最终 Shapefile。
- 支持按用户选择导出最终成果：`Raster (GeoTIFF)` 或 `Vector (Shapefile)`。

## 界面结构

插件主对话框采用页签式布局，参考 SamGeo 插件的 Model / Output 分栏方式：

- `推理` 页签：模型目录、模型选择、输入图层或本地影像、预处理选项、推理进度，以及运行、取消、关闭按钮。
- `编辑与导出` 页签：导出格式、导出目录、编辑与导出状态，以及确认选中对象、全部确认、导出结果、关闭按钮。

导出设置只需要选择最终成果格式和导出目录。插件会根据输入影像名称自动生成结果文件名：

- 选择 `Vector (Shapefile)` 时，导出为 `<输入文件名>_final.shp`。
- 选择 `Raster (GeoTIFF)` 时，导出为 `<输入文件名>_final.tif`。

推理完成后仍会先生成草稿矢量图层，用户可在 QGIS 中编辑或确认对象，再导出所选格式的最终成果。

## 安装

1. 安装 QGIS 3.28 LTR。
2. 将 `land_cover_classification/` 目录部署到 QGIS 插件目录。
3. 在 QGIS 对应的 Python 环境中安装运行依赖。
4. 将导出的 PaddleRS 模型子目录放入 `land_cover_classification/models/semantic_segmentation/`。
5. 重启 QGIS，并在插件管理器中启用 `LandCoverClassification`。

更详细的依赖安装说明见 [`docs/install.md`](docs/install.md)。

## 使用流程

1. 在 `推理` 页签中选择输入图层或本地影像。
2. 选择分割模型与可选预处理项。
3. 在 `编辑与导出` 页签中选择导出格式和导出目录。
4. 回到 `推理` 页签点击运行，插件会先生成单波段类别栅格，再自动加载草稿矢量图层。
5. 在 QGIS 中编辑草稿层结果，并通过 `编辑与导出` 页签中的“确认选中对象”或“全部确认”写入最终结果图层。
6. 点击“导出结果”，插件按所选格式导出最终 Shapefile 或 GeoTIFF。

## 模型目录

模型目录结构说明见 [`docs/model_layout.md`](docs/model_layout.md)。

## 仓库结构

```text
new_semantic_segmentation/
|-- README.md                          # 项目说明文档
|-- LICENSE                            # 开源许可证
|-- AGENTS.md                          # Codex 协作与项目约定
|-- docs/                              # 额外文档
|   |-- install.md                     # 依赖安装说明
|   `-- model_layout.md                # 模型目录结构说明
`-- land_cover_classification/         # QGIS 插件主体目录
    |-- __init__.py                    # 插件入口
    |-- land_cover_classification.py   # 主插件类，负责菜单与对话框入口
    |-- land_cover_classification_dialog.py
    |                                   # 主对话框逻辑、草稿层确认与结果导出
    |-- land_cover_classification_dialog_base.ui
    |                                   # Qt Designer 维护的界面文件
    |-- inference.py                   # 核心推理逻辑，输出单波段类别栅格
    |-- inference_runner.py            # 独立推理子进程入口
    |-- preprocess.py                  # 推理前预处理链
    |-- model_scan.py                  # 扫描可用模型并解析 model.yml
    |-- deps_check.py                  # 运行依赖检查与提示
    |-- metadata.txt                   # QGIS 插件元数据
    |-- pb_tool.cfg                    # pb_tool 打包配置
    |-- vendor/                        # 随插件打包的第三方代码
    |   `-- PaddleRS/                  # 内置 PaddleRS 代码与依赖
    `-- models/                        # 模型根目录
        `-- semantic_segmentation/     # 语义分割模型默认存放位置
```

## 许可

本项目以 Apache License 2.0 发布，详见 [`LICENSE`](LICENSE)。内置的 PaddleRS 代码同样遵循 Apache-2.0，相关许可位于 `land_cover_classification/vendor/PaddleRS/LICENSE`。
