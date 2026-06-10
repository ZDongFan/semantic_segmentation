# LandCoverClassification 地物分类 QGIS 插件

这是一个面向 QGIS 3.28 LTR 的地物分类插件，可对当前活动栅格图层或本地影像执行基于 PaddleRS 的遥感语义分割，并将结果组织为“类别栅格 -> 草稿矢量 -> 最终成果”的人工确认流程。推理生成草稿层后，插件还支持基于 SAM2.1 Base+ 的 AI 辅助编辑，用正负点提示生成 mask 预览，并将结果追加或替换到现有草稿图层；如需兼容旧流程，也可显式回退到 SAM1 ViT-B 后端。

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
- 推理完成后自动切换到 `编辑与导出` 页签，并可启动 `AI 辅助编辑`。
- AI 辅助编辑使用独立 SAM worker 子进程，支持左键正样本点、右键负样本点、mask/polygon 预览、撤销点、清除点。
- SAM worker 会参考 TerraLab 的交互策略，在子进程内按提示点裁剪 1024 x 1024 crop 进行 SAM 编码；没有 `mask_input` 的首轮或兜底重跑会内部启用多候选筛选，一正一负同样适用，后续正负点优先复用上一轮 `low_res_masks` 作为 `mask_input` 迭代细化；已有活动 crop 时优先保持原 crop，不因边界附近新增负点频繁重切 crop，crop 外负点也不会参与当前轮细化。
- AI mask 预览与推理草稿统一服务于同一草稿流程，不创建独立 `AI mask 预览` 图层；启动 AI 编辑后，预览通过画布 RubberBand 显示在当前草稿层上下文中。
- SAM worker 会在子进程内只保留最大外轮廓并输出单个滑坡草稿对象，同时严格简化轮廓和限制点数，避免大范围或不明确提示点生成的超复杂 mask 导致 QGIS 主进程异常退出；负点是硬约束，最终预览 polygon 不应覆盖负点；当负点落在包含正点的主连通域边界附近时，优先通过候选重选和迭代细化收缩边界，而不是直接把整块主区域删除。
- 正负点快速连续变化时，插件会避免重入预测；如果上一轮 SAM 预测尚未结束，只保留最新点集排队，完成后再预测最新结果。
- AI 结果直接写回当前草稿层，支持“追加为新草稿对象”和“替换选中草稿对象”。
- 仓库约定了默认 SAM2.1 Base+ 权重位置与 SAM runtime 目录，用户可按文档准备 `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt` 后，在目标机创建插件专用 SAM 虚拟环境。

## 界面结构

插件主对话框采用页签式布局，参考 SamGeo 插件的 Model / Output 分栏方式：

- `推理` 页签：模型目录、模型选择、输入图层或本地影像、预处理选项、推理进度，以及运行、取消、关闭按钮。
- `编辑与导出` 页签：导出格式、导出目录、AI 辅助编辑控件、编辑与导出状态，以及确认选中对象、全部确认、导出结果、关闭按钮。

导出设置只需要选择最终成果格式和导出目录。插件会根据输入影像名称自动生成结果文件名：

- 选择 `Vector (Shapefile)` 时，导出为 `<输入文件名>_final.shp`。
- 选择 `Raster (GeoTIFF)` 时，导出为 `<输入文件名>_final.tif`。

推理完成后仍会先生成草稿矢量图层，用户可在 QGIS 中编辑或确认对象，再导出所选格式的最终成果。

AI 辅助编辑控件位于 `编辑与导出` 页签中。默认后端为 `sam2`，启动后插件会加载 `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt`，并使用 `land_cover_classification/vendor/sam_runtime/venv/` 中的独立 Python 环境运行 `sam_worker.py`。QGIS 主进程只负责界面、坐标转换、预览显示和草稿层写回，不在主进程中导入 `torch`、`sam2` 或 `segment_anything`。预览不会写入真实草稿层，也不会创建单独的 AI 图层；只有用户点击追加或替换时，AI 结果才提交到草稿层。为适配当前滑坡识别任务，一次 AI 编辑只生成一个滑坡草稿对象：SAM 子进程按提示点裁剪 crop、使用多候选筛选和 `low_res_masks` 迭代细化、尽量复用当前活动 crop、忽略 crop 外负点、提取最大外轮廓，并保证负点不被最终预览覆盖；QGIS 侧也会兜底选择最大单面几何，并用稳定的单面 RubberBand 显示预览。

## 安装

1. 安装 QGIS 3.28 LTR。
2. 将 `land_cover_classification/` 目录部署到 QGIS 插件目录。
3. 在 QGIS 对应的 Python 环境中安装运行依赖。
4. 将导出的 PaddleRS 模型子目录放入 `land_cover_classification/models/semantic_segmentation/`。
5. 如需使用 AI 辅助编辑，请先准备 `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt`，再在目标机运行 `land_cover_classification/vendor/sam_runtime/create_sam_venv.bat` 或 `create_sam_venv.sh` 创建 SAM 专用虚拟环境；创建完成后可用 `python land_cover_classification/sam_deps_check.py --backend sam2` 验证环境。
6. 重启 QGIS，并在插件管理器中启用 `LandCoverClassification`。

更详细的依赖安装说明见 [`docs/install.md`](docs/install.md)。

## 使用流程

1. 在 `推理` 页签中选择输入图层或本地影像。
2. 选择分割模型与可选预处理项。
3. 在 `编辑与导出` 页签中选择导出格式和导出目录。
4. 回到 `推理` 页签点击运行，插件会先生成单波段类别栅格，再自动加载草稿矢量图层。
5. 推理完成后插件自动切到 `编辑与导出` 页签。可直接手动编辑草稿层，也可以点击“启动 AI 编辑”后在地图画布上左键添加正样本点、右键添加负样本点，预览满意后追加或替换草稿对象。连续添加点时界面只以最新点集刷新预览，避免多轮 SAM 预测同时回写画布；负点用于从当前活动 crop 内的迭代 mask 中排除区域，预览不应覆盖负点，边界附近连续补负点时应表现为沿上一轮边界逐步收缩。
6. 通过 `编辑与导出` 页签中的“确认选中对象”或“全部确认”写入最终结果图层。
7. 点击“导出结果”，插件按所选格式导出最终 Shapefile 或 GeoTIFF。

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
    |-- sam_deps_check.py              # SAM 专用运行环境检查
    |-- sam_worker.py                  # SAM 独立子进程入口
    |-- ai_segment_tool.py             # AI 编辑地图交互工具
    |-- metadata.txt                   # QGIS 插件元数据
    |-- pb_tool.cfg                    # pb_tool 打包配置
    |-- vendor/                        # 随插件打包的第三方代码
    |   |-- PaddleRS/                  # 内置 PaddleRS 代码与依赖
    |   `-- sam_runtime/               # SAM 专用 venv 创建脚本与运行环境说明
    `-- models/                        # 模型根目录
        |-- semantic_segmentation/     # 语义分割模型默认存放位置
        |-- sam/                       # SAM1 ViT-B 回退权重默认存放位置
        `-- sam2/                      # SAM2.1 权重默认存放位置
```

## 许可

本项目以 Apache License 2.0 发布，详见 [`LICENSE`](LICENSE)。内置的 PaddleRS 代码同样遵循 Apache-2.0，相关许可位于 `land_cover_classification/vendor/PaddleRS/LICENSE`。SAM2、PyTorch、segment-anything 及其他 SAM runtime 依赖的第三方说明位于 `land_cover_classification/vendor/sam_runtime/NOTICE.txt`。
