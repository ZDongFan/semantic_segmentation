# AGENTS.md

## 项目概览

当前仓库是一个面向 **QGIS 3.28+** 的语义分割插件项目，仓库根目录主要放说明文档，真正的插件代码位于 `land_cover_classification/` 目录。

- 仓库目录名：`new_semantic_segmentation`
- 插件目录名：`land_cover_classification`
- 插件显示名：`LandCoverClassification`
- 主要用途：在 QGIS 中对栅格图层或本地影像执行基于 PaddleRS 的遥感语义分割，并将结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工确认流程；推理生成草稿层后，可用 SAM1 ViT-B 进行 AI 辅助编辑并写回草稿层

这个仓库不是“只有一个纯插件目录”的结构，而是：

1. 根目录放项目入口说明、安装文档和模型目录说明。
2. `land_cover_classification/` 放 QGIS 插件主体代码、资源、测试、内置 PaddleRS 与默认模型目录。

开发时请优先保持现有目录布局和 QGIS Plugin Builder 风格，避免无关重构。

## 主要技术栈与运行环境

- 语言：Python
- 宿主：QGIS 3
- 最低 QGIS 版本：`3.28`
- UI：PyQt / QGIS PyQt
- 推理框架：PaddlePaddle + PaddleRS
- AI 辅助编辑：SAM1 ViT-B + PyTorch + segment-anything，运行在插件专用独立 venv 中
- 影像处理：OpenCV、NumPy、GDAL / OSGeo
- 模型配置解析：PyYAML
- 矢量输出：QGIS Vector API + GDAL/OGR
- 打包方式：`pb_tool.cfg`、`Makefile`

PaddleRS 语义分割运行依赖以 `land_cover_classification/deps_check.py` 为准，当前检查的依赖包括：

- `paddlepaddle`（导入模块 `paddle`）
- `paddlers`
- `opencv-contrib-python`（导入模块 `cv2`）
- `PyYAML`（导入模块 `yaml`）
- `GDAL`（导入模块 `osgeo.gdal`）

`metadata.txt` 中当前说明依赖 `paddlepaddle==2.4.2`。在 Windows 下，这些依赖通常应安装到 QGIS / OSGeo4W 自带 Python 环境，而不是系统 Python。

SAM AI 辅助编辑依赖以 `land_cover_classification/sam_deps_check.py` 为准，只在启动 AI 编辑时检查。SAM 相关依赖包括：

- `torch`
- `segment-anything`（导入模块 `segment_anything`）
- `opencv-contrib-python`（导入模块 `cv2`）
- `numpy`

SAM 依赖不安装到 QGIS Python 环境，而是安装到 `land_cover_classification/vendor/sam_runtime/venv/`。在 QGIS 进程中启动 SAM 子进程时必须清理 `PYTHONHOME`、`PYTHONPATH`、`PYTHONUSERBASE` 等 QGIS/OSGeo4W Python 环境变量，避免 Python 3.8 venv 误加载 QGIS Python 3.9 标准库。

## 仓库结构

### 根目录

- `README.md`：项目总说明
- `AGENTS.md`：Codex 协作与项目约定
- `docs/install.md`：依赖安装说明
- `docs/model_layout.md`：模型目录结构说明
- `land_cover_classification/`：QGIS 插件主体目录

### 插件目录 `land_cover_classification/`

- `__init__.py`：QGIS 插件入口，定义 `classFactory(iface)`
- `land_cover_classification.py`：插件主类，负责菜单、工具栏和依赖检查
- `land_cover_classification_dialog.py`：主对话框逻辑
- `land_cover_classification_dialog_base.ui`：Qt Designer 维护的 UI 文件
- `inference.py`：核心推理逻辑
- `inference_runner.py`：独立推理子进程入口
- `preprocess.py`：推理前预处理逻辑
- `model_scan.py`：扫描可用 PaddleRS 分割模型
- `deps_check.py`：依赖检查与安装提示
- `sam_deps_check.py`：SAM 专用运行环境、模型权重和依赖检查
- `sam_worker.py`：SAM 独立子进程入口，使用 JSON line 协议通信，并在子进程内对复杂 mask 轮廓做最大外轮廓提取、简化和点数限制
- `ai_segment_tool.py`：AI 辅助编辑地图交互工具，负责正负点采集和预览 RubberBand；预览只作为画布覆盖显示，不写入真实草稿层
- `metadata.txt`：QGIS 插件元数据
- `pb_tool.cfg`：插件打包配置
- `resources.qrc` / `resources.py`：Qt 资源文件
- `models/semantic_segmentation/`：默认模型根目录
- `models/sam/`：SAM ViT-B 默认权重目录
- `vendor/PaddleRS/`：随插件分发的 PaddleRS 代码
- `vendor/sam_runtime/`：SAM 专用 venv 创建脚本、requirements、NOTICE 与离线 wheels
- `test/`：插件相关测试

## 插件运行流程

1. QGIS 加载插件时调用 `land_cover_classification/__init__.py` 中的 `classFactory(iface)`。
2. `LandCoverClassification.initGui()` 注册菜单和工具栏入口。
3. 用户打开插件后，`run()` 先执行 `deps_check.check()` 检查运行依赖。
4. 依赖满足后创建并显示 `LandCoverClassificationDialog`。
5. 对话框默认扫描 `land_cover_classification/models/semantic_segmentation/` 下的可用模型。
6. 用户在 `推理` 页签选择输入图层或本地影像、模型和可选预处理项。
7. 用户在 `编辑与导出` 页签选择导出格式和导出目录。
8. 对话框通过 `QProcess` 启动 `inference_runner.py` 子进程执行推理。
9. 推理完成后先得到类别栅格，再在主进程中矢量化为草稿图层，并自动切换到 `编辑与导出` 页签。
10. 用户可直接编辑草稿层，或启动 AI 辅助编辑：主进程启动 `sam_worker.py` 独立子进程，左键正点 / 右键负点生成 mask 预览；预览显示在当前草稿层上下文中，不创建独立 AI 图层，只有点击追加或替换时才写回草稿层。正负点快速变化时必须避免重入预测，只保留最新点集排队刷新。
11. 用户确认草稿对象后写入最终结果，再按所选格式导出 Shapefile 或 GeoTIFF。

## 当前 UI 与导出约定

主对话框采用 `QTabWidget` 分成两个页签：

- `推理`：包含模型、输入、预处理、推理进度，以及运行、取消、关闭按钮。
- `编辑与导出`：包含输出设置、AI 辅助编辑、编辑与导出状态，以及确认选中对象、全部确认、导出结果、关闭按钮。

导出设置参考 SamGeo 插件的 Output 页面方式：

- `outputFormatCombo` 提供 `Raster (GeoTIFF)` 和 `Vector (Shapefile)` 两种最终成果格式。
- `outputDirWidget` 只选择导出目录，不直接选择固定文件路径。
- 代码会根据输入影像名称自动生成 `<输入文件名>_final.shp` 或 `<输入文件名>_final.tif`。
- `outputFileWidget` 和 `rasterFileWidget` 仍保留为隐藏兼容控件，避免旧逻辑中依赖控件名的位置直接失效。

AI 辅助编辑控件位于 `编辑与导出` 页签：

- `aiClassCombo`：追加新草稿对象时使用的类别。
- `aiStartBtn` / `aiStopBtn`：启动 / 停止 AI 编辑。
- `aiUndoPointBtn` / `aiClearPointsBtn`：撤销最后一个点 / 清空所有提示点。
- `aiAppendDraftBtn` / `aiReplaceDraftBtn`：将当前 mask 预览追加为新草稿对象 / 替换选中草稿对象。
- `aiHintLabel` / `aiStatusLabel`：交互提示和当前 AI 状态。

进度条和按钮按页签拆分显示：

- 推理页使用 `progressBar`、`statusLabel`、`runBtn`、`cancelBtn`、`closeBtn`。
- 编辑导出页使用 `exportProgressBar`、`exportStatusLabel`、`confirmSelectedBtn`、`confirmAllBtn`、`exportRasterBtn`、`exportCloseBtn`。
- Python 侧通过镜像包装让状态文本和进度值同时同步到两个页签，避免用户切换页签后看不到当前任务状态。

## 输出物与数据流

当前流程不是直接输出彩色分割图，而是分为几步：

1. 生成单波段类别 GeoTIFF。
2. 将类别栅格矢量化为草稿图层。
3. 用户在 QGIS 中确认或编辑草稿要素，也可以用 SAM AI 辅助编辑生成 mask 预览并写回草稿层。
4. 写出最终 Shapefile。
5. 按用户选择直接保留 Shapefile，或将最终矢量结果重新栅格化导出为 GeoTIFF。

这套流程的关键点：

- 推理阶段的主输出是类别栅格。
- 人工确认发生在草稿矢量层。
- AI 辅助编辑只服务推理后的草稿矢量层，不创建独立 AI 图层。
- AI mask 预览应通过 `AiSegmentMapTool` 的 `QgsRubberBand` 显示，禁止在点提示变化时向真实草稿层写入临时要素。
- 当前插件服务滑坡识别，一次 AI 编辑应产出一个滑坡草稿对象；`sam_worker.py` 必须使用外轮廓提取并只返回最大轮廓，不返回洞、多碎片或复杂集合几何。
- 大范围或不明确的 SAM 结果必须在 `sam_worker.py` 中先提取最大外轮廓、简化轮廓并限制输出点数，再交给 QGIS 主进程构建几何，避免复杂 `MultiPolygon` / `GeometryCollection` 触发 QGIS 崩溃。
- QGIS 主进程构建和预览 AI 几何时仍要保留最大单面兜底，`QgsRubberBand` 只显示稳定单面 polygon，避免把多部件几何直接推给画布预览。
- AI 追加的新草稿对象应写入 `class_id`、`class_name`、`review_status = 待确认`、`source_id = 当前最大值 + 1`。
- AI 替换选中草稿对象时应替换几何，并保持或按当前逻辑维护 `class_id/class_name/source_id`，同时将 `review_status` 重置为 `待确认`。
- 最终成果格式由 `编辑与导出` 页签中的导出格式决定。
- 即使最终选择导出 GeoTIFF，流程内部仍会先维护最终矢量结果，再将其栅格化。

## 模型目录约定

默认模型根目录为：

`land_cover_classification/models/semantic_segmentation/`

每个可用模型子目录通常至少需要：

- `model.yml`
- `model.pdmodel`
- `model.pdiparams`

常见附加文件还包括：

- `model.pdiparams.info`
- `pipeline.yml`
- `.success`

`model_scan.py` 当前只接受 `model.yml` 中 `_Attributes.model_type == "segmenter"` 的模型。

SAM 默认权重目录为：

`land_cover_classification/models/sam/`

默认权重文件为：

- `sam_vit_b_01ec64.pth`

SAM runtime 目录为：

`land_cover_classification/vendor/sam_runtime/`

其中 `create_sam_venv.bat` / `create_sam_venv.sh` 用于从离线 wheels 创建本机专用 venv。仓库和插件包应包含 `requirements-sam.txt`、`NOTICE.txt`、`wheels/` 和默认模型说明，但不要在 QGIS 主进程内导入 `torch` 或 `segment_anything`。

## 构建、测试与部署

常用命令主要在 `land_cover_classification/Makefile` 中：

```bash
make compile
make test
make pylint
make pep8
make package VERSION=<commit-or-tag>
```

注意事项：

- 这些命令需要在插件目录上下文理解和执行。
- `make compile` 会从 `resources.qrc` 生成 `resources.py`。
- `make test` 依赖可导入 `qgis.core` 的 QGIS Python 环境。
- Windows 下部分命令更适合在 OSGeo4W Shell、Git Bash 或兼容环境中执行。
- 打包配置以 `land_cover_classification/pb_tool.cfg` 为准。

## 开发约定

- 这是一个 QGIS 插件仓库，修改前先确认自己是在“仓库根目录”还是“插件子目录”语境下工作。
- 涉及插件逻辑时，优先查看 `land_cover_classification/` 下对应文件，不要只根据根目录文档做假设。
- 修改 UI 时优先编辑 `.ui` 文件，并确认 Python 侧控件名仍然匹配。
- 当前 UI 分页依赖 `land_cover_classification_dialog_base.ui` 中的 `mainTabWidget`，不要把推理、编辑和导出控件重新混回同一个按钮区。
- 导出相关逻辑依赖 `outputFormatCombo` 和 `outputDirWidget`，隐藏的 `outputFileWidget`、`rasterFileWidget` 主要用于兼容旧代码路径。
- AI 编辑相关逻辑依赖 `aiClassCombo`、`aiStartBtn`、`aiStopBtn`、`aiUndoPointBtn`、`aiClearPointsBtn`、`aiAppendDraftBtn`、`aiReplaceDraftBtn`、`aiStatusLabel`，不要在未同步 Python 侧逻辑时改名。
- AI 预览和推理草稿都属于同一草稿工作流；不要恢复独立 `AI mask 预览` 图层，也不要把预览几何作为临时要素写进 OGR/GPKG 草稿层。
- 不要随意移除 `vendor/PaddleRS` 相关路径注入和延迟导入逻辑。
- 不要轻易把推理挪回 QGIS 主进程同步执行，当前子进程设计是为了隔离依赖与降低宿主崩溃风险。
- 修改推理事件协议时，要同时检查 `inference_runner.py` 和 `land_cover_classification_dialog.py`。
- 不要把 SAM 推理挪回 QGIS 主进程，不要在主进程内 `import torch` / `import segment_anything`。
- 修改 SAM worker 协议时，要同时检查 `sam_worker.py`、`sam_deps_check.py` 和 `land_cover_classification_dialog.py`。
- 修改 SAM mask 轮廓输出逻辑时，要保留复杂度保护，包括 `cv2.RETR_EXTERNAL` 外轮廓提取、最大轮廓选择、轮廓简化、面数量限制和总点数限制；这些保护是防止 QGIS 在大 mask 预览时异常退出的关键。
- 修改 AI 点提示响应逻辑时，要保留 `_ai_predicting` / `_ai_queued_points` 防重入机制；SAM 正在预测时只排队最新点集，不要让多轮预测同时更新预览或按钮状态。
- 启动 SAM venv Python 时必须使用清洁环境，避免继承 QGIS 的 `PYTHONHOME` / `PYTHONPATH` 导致版本串扰。
- 修改模型目录、打包资源或额外文件时，同步检查 `pb_tool.cfg`。

## 编码与文档注意事项

- 仓库里部分中文内容在某些 Windows / PowerShell 读取方式下可能显示乱码，修改时要特别注意文件实际编码。
- `metadata.txt`、部分 Python 文件和说明文档都可能受编码影响，除非必要，不要整文件重写。
- 若新增说明文档，优先放在根目录 `docs/` 或仓库根目录，而不是随意混入插件内部。
- 新增代码注释和文档字符串必须使用中文；专有名词、API 名称、变量名和命令行输出可以保留英文。

## 验证建议

根据改动范围选择验证方式：

- 只改文档：检查链接、路径和目录描述是否与当前仓库一致。
- 只改纯 Python 逻辑：优先运行对应单元测试，或至少验证模块可导入。
- 改 UI 或 QGIS 集成：在 QGIS 中手动验证插件入口、两个页签、进度条、按钮、输出目录和草稿流程。
- 改导出流程：分别验证 `Raster (GeoTIFF)` 和 `Vector (Shapefile)` 两种导出格式。
- 改推理流程：至少分别验证普通影像和带地理坐标的 GeoTIFF。
- 改 AI 编辑流程：验证推理完成后自动进入 `编辑与导出` 页签，启动 AI 编辑，左键 / 右键 / 撤销 / 清空 / 停止恢复 map tool，追加和替换草稿对象字段完整；还要验证正负点组合、小范围 mask、大范围/不明确 mask、快速连续加点的最新点集排队刷新，确认 QGIS 不崩溃、预览为单个稳定 polygon、图层树不出现独立 `AI mask 预览` 图层。
- 改 SAM runtime 或环境检查：验证 `sam_deps_check.ensure_ready()`、`sam_worker.py init/set_image/predict`，并特别检查 QGIS 环境变量不会污染 SAM venv。
- 改资源或打包配置：检查 `resources.py`、`pb_tool.cfg` 与实际文件列表是否一致。

如果当前 shell 无法导入 `qgis.core`，不要把它当成普通 pip 包处理；应切换到 QGIS / OSGeo4W 提供的 Python 环境。

## Git 与协作注意事项

- 未经用户明确要求，不要自动提交、创建分支、推送或执行破坏性 Git 操作。
- 工作区中已有改动默认视为用户改动，不要回滚或覆盖。
- 修改前先确认任务相关文件，保持改动小而清晰。
