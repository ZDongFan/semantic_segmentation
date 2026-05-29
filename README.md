# LandCoverClassification 地物分类 QGIS 插件

这是一个面向 QGIS 3.28 LTR 的地物分类插件，可对当前活动栅格图层或磁盘影像执行 PaddleRS 语义分割推理，并将结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工确认流程。

插件主体位于 `land_cover_classification/` 目录下，默认自带 PaddleRS 运行时、模型目录和相关测试资源。可用模型默认放在 `land_cover_classification/models/semantic_segmentation/`。

## 主要功能

- 自动判断输入是否带地理坐标。
- GeoTIFF 走 PaddleRS `slider_predict`，保留原始 CRS 和 GeoTransform。
- 普通 JPG/PNG/TIF 先 resize 到 `512 x 512`，推理后再按原始尺寸回采样。
- 支持可选预处理链：CLAHE、锐化、中值滤波、高斯滤波。
- 后台子进程执行推理，避免卡住 QGIS 主界面。
- 自动扫描 `land_cover_classification/models/semantic_segmentation/` 下的分割模型并填充下拉框。
- 推理阶段输出单波段类别 GeoTIFF，供后续矢量化与人工确认使用。
- 自动将类别栅格矢量化为草稿图层，并支持确认后写出最终 Shapefile。
- 支持将最终矢量结果重新栅格化导出为 GeoTIFF。

## 安装

1. 安装 QGIS 3.28 LTR。
2. 将 `land_cover_classification/` 目录部署到 QGIS 插件目录。
3. 在 QGIS 对应的 Python 环境中安装运行依赖。
4. 把导出的 PaddleRS 模型子目录放入 `land_cover_classification/models/semantic_segmentation/`。
5. 重启 QGIS，在插件管理器中启用 `LandCoverClassification`。

更详细的依赖安装说明见 [`docs/install.md`](docs/install.md)。

## 使用流程

1. 在插件对话框中选择输入图层或本地影像。
2. 选择分割模型与可选预处理项。
3. 设置最终 Shapefile 和最终 GeoTIFF 输出路径。
4. 点击运行后，插件会先生成单波段类别栅格，再自动加载草稿矢量图层。
5. 用户可在草稿层中编辑结果，并通过“确认选中对象”或“全部确认”写入最终 Shapefile。
6. 如有需要，可继续导出最终 GeoTIFF。

## 模型目录

模型目录结构说明见 [`docs/model_layout.md`](docs/model_layout.md)。

## 仓库结构

```text
semantic_segmentation/
├─ README.md                           # 项目说明文档
├─ LICENSE                             # 开源许可证
├─ docs/                               # 额外文档
│  ├─ install.md                       # 依赖安装说明
│  └─ model_layout.md                  # 模型目录结构说明
└─ land_cover_classification/          # QGIS 插件主体目录
   ├─ __init__.py                      # 插件入口，负责注入 vendor 路径
   ├─ land_cover_classification.py     # 主插件类，负责菜单与对话框入口
   ├─ land_cover_classification_dialog.py
   │                                   # 主对话框逻辑，负责参数收集、草稿层确认与结果导出
   ├─ land_cover_classification_dialog_base.ui
   │                                   # Qt Designer 设计的界面文件
   ├─ inference.py                     # 核心推理逻辑，输出单波段类别栅格
   ├─ inference_runner.py              # 独立推理子进程入口
   ├─ preprocess.py                    # 推理前预处理链
   ├─ model_scan.py                    # 扫描可用模型并解析 model.yml
   ├─ deps_check.py                    # 运行依赖检查与提示
   ├─ metadata.txt                     # QGIS 插件元数据
   ├─ pb_tool.cfg                      # pb_tool 打包配置
   ├─ vendor/                          # 随插件打包的第三方代码
   │  └─ PaddleRS/                     # 内置 PaddleRS 代码与依赖
   └─ models/                          # 模型根目录
      └─ semantic_segmentation/        # 语义分割模型默认存放位置
```

## 许可

本项目以 Apache License 2.0 发布，详见 [`LICENSE`](LICENSE)。
内置的 PaddleRS 代码同样遵循 Apache-2.0，相关许可位于 `land_cover_classification/vendor/PaddleRS/LICENSE`。
