# LandCoverClassification — 地物分类 QGIS 插件

一个面向 QGIS 3.28 LTR 的插件,可对当前活动栅格图层或磁盘上的任意支持
影像运行 PaddleRS 语义分割模型,并把上色后的结果作为新的栅格图层加入
画布。

插件自包含:`land_cover_classification/vendor/PaddleRS/` 内置了 `paddlers`
库,模型默认从 `land_cover_classification/models/semantic_segmentation/`
扫描(也可在对话框中改到其他目录)。

## 功能

- 自动判断输入是否带地理坐标:
  - GeoTIFF 走 PaddleRS 的 `slider_predict`,产物保留原始 CRS 与
    GeoTransform,可直接与 QGIS 中的底图对齐叠加。
  - 普通 JPG/PNG 被 resize 到 512×512,一次性走 `predict()`。
- 可选的预处理链(CLAHE → 锐化 → 中值滤波 → 高斯滤波)。
- 后台 `QgsTask` 执行 —— QGIS 界面不会卡死,任务可在任务管理器中取消。
- 按指定根目录扫描已导出的 PaddleRS 分割模型并填入下拉框。

## 安装

1. **安装 QGIS 3.28 LTR。**
2. **部署插件**(开发阶段推荐用 `pb_tool`):
   ```
   cd land_cover_classification
   pb_tool deploy
   ```
   该命令会把整个包(含 vendor 的 `PaddleRS/` 与 `models/`)拷贝到
   `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification\`
   (Windows)或 `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   (Linux)。
3. **在 QGIS 的 Python 环境中安装运行时依赖。** 插件首次启动时会检查
   依赖并弹窗给出具体命令。核心一条是在 **OSGeo4W Shell** 中执行
   `pip install -r "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification\vendor\PaddleRS\requirements.txt"`,
   它会装齐 PaddleRS 的全部直接依赖。完整列表与平台相关 URL 见
   [`docs/install.md`](docs/install.md)。
4. **放入模型** —— 把已导出的 PaddleRS 分割模型子目录拷贝到
   `land_cover_classification/models/semantic_segmentation/`。模型目录
   结构见 [`docs/model_layout.md`](docs/model_layout.md)。
5. 重启 QGIS,在「插件管理器」中启用 **LandCoverClassification**,然后
   点工具栏图标即可使用。

## 项目结构

```
semantic_segmentation/                    # 本仓库根目录
├── docs/
│   ├── install.md                        # 依赖安装命令
│   └── model_layout.md                   # 模型目录结构
└── land_cover_classification/            # 真正会被 QGIS 加载的插件包
    ├── __init__.py                       # 把 vendor/PaddleRS 注入 sys.path
    ├── land_cover_classification.py      # 主类,负责弹出 modeless 对话框
    ├── land_cover_classification_dialog.py
    ├── land_cover_classification_dialog_base.ui
    ├── inference.py                      # SegmenterTask(QgsTask)
    ├── preprocess.py                     # CLAHE / 锐化 / 中值 / 高斯
    ├── model_scan.py                     # 扫描模型 + 解析 model.yml
    ├── deps_check.py                     # paddle/cv2/gdal/yaml 检查
    ├── metadata.txt                      # qgisMinimumVersion=3.28 等
    ├── pb_tool.cfg                       # extra_dirs: vendor models
    ├── vendor/PaddleRS/                  # vendor 的 PaddleCV-SIG/PaddleRS
    └── models/semantic_segmentation/     # 用户在此放置导出的分割模型
```

## 许可

本项目以 Apache License 2.0 发布,详见 [`LICENSE`](LICENSE)。内置的
PaddleRS 同为 Apache-2.0,其上游 LICENSE 保留在
`land_cover_classification/vendor/PaddleRS/LICENSE`。
