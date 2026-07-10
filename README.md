# LandCoverClassification 地物分类 QGIS 插件

这是一个面向 QGIS 3.28+ 的语义分割插件，可对当前栅格图层或本地影像执行基于 PyTorch bundle 的遥感滑坡 / 地物语义分割，并将结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工编辑流程。推理生成草稿层后，插件支持基于 SAM2.1 Base+ 的 AI 辅助编辑，用正负点提示生成 mask 预览，并将结果追加到现有草稿图层。

插件主体位于 `land_cover_classification/` 目录下。可用 PyTorch bundle 默认放在 `land_cover_classification/models/semantic_segmentation/`；SAM2 权重默认放在 `land_cover_classification/models/sam2/`。

## 主要功能

- 扫描 `manifest.json` 格式的 PyTorch 语义分割 bundle，并在模型下拉框中列出。
- 使用插件统一运行环境 `vendor/sam_runtime/venv/` 子进程执行 PyTorch 主推理和 SAM AI 编辑，QGIS 主进程不导入 `torch`；打开插件面板时不检查 / 加载 venv，点击“运行”后才检查 PyTorch 主推理环境。
- 推理时必选 DEM 文件，插件会把 DEM 对齐到输入影像格网，并调用 bundle 内 `dem_factors.py` 计算派生因子。
- 支持 GPU 推理，并在 CUDA 不可用时自动降级到 CPU；CPU 路径使用更小 tile 保护内存。
- 对 landslide 概率图执行 threshold、连通域、最小面积过滤，以及 slope、relief、TPI 三条 DEM 规则后处理。
- 每次推理写出单波段类别 GeoTIFF 和 `<output>.postprocess.json` 审计文件。
- 自动将类别栅格矢量化为草稿图层，支持编辑后导出 `Raster (GeoTIFF)`、`Vector (Shapefile)` 或 `Vector (DXF / CAD)`。
- AI 辅助编辑使用独立 SAM worker 子进程，支持左键正样本点、右键负样本点、撤销、清空、停止和“回到 AI 点选模式”。

## 安装

1. 安装 QGIS 3.28+。
2. 将 `land_cover_classification/` 目录部署到 QGIS 插件目录。
3. 运行 `land_cover_classification/vendor/sam_runtime/create_sam_venv.bat` 或 `create_sam_venv.sh` 创建插件统一运行环境。
4. 将训练仓导出的 PyTorch bundle 子目录放入 `land_cover_classification/models/semantic_segmentation/`。
5. 如需使用 AI 辅助编辑，准备 `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt`。
6. 重启 QGIS，并在插件管理器中启用 `LandCoverClassification`。

更详细的模型目录说明见 [`docs/model_layout.md`](docs/model_layout.md)。

## 使用流程

1. 在 `推理` 页签中选择 PyTorch bundle、输入图层或本地影像、DEM 文件和可选预处理项。
2. 在 `编辑与导出` 页签中选择导出格式和导出目录。
3. 回到 `推理` 页签点击运行；插件会在基础参数校验通过后检查 PyTorch 统一运行环境，然后生成类别 GeoTIFF、后处理审计 JSON 和草稿矢量图层。
4. 推理完成后插件自动切到 `编辑与导出` 页签，可手动编辑草稿层，也可启动 AI 辅助编辑追加对象。
5. 编辑完成后点击“导出结果”，插件会从当前草稿层导出所选格式。

## 仓库结构

```text
semantic_segmentation/
|-- README.md
|-- LICENSE
|-- docs/
|   |-- install.md
|   `-- model_layout.md
`-- land_cover_classification/
    |-- __init__.py
    |-- land_cover_classification.py
    |-- land_cover_classification_dialog.py
    |-- land_cover_classification_dialog_base.ui
    |-- pytorch_inference_core.py
    |-- pytorch_inference_runner.py
    |-- pytorch_deps_check.py
    |-- preprocess.py
    |-- model_scan.py
    |-- sam_deps_check.py
    |-- sam_worker.py
    |-- ai_segment_tool.py
    |-- vendor/
    |   `-- sam_runtime/      # 插件统一运行环境
    `-- models/
        |-- semantic_segmentation/
        `-- sam2/
```

## 许可

本项目以 Apache License 2.0 发布，详见 [`LICENSE`](LICENSE)。统一运行环境中的第三方依赖遵循各自上游许可证。
