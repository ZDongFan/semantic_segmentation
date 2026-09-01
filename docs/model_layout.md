# 模型目录结构

插件会扫描指定的模型根目录，把每个一级子目录识别为一个 PyTorch 语义分割 bundle。
默认模型根目录为：

`land_cover_classification/models/semantic_segmentation/`

该路径可在插件对话框的“模型根目录”字段中修改，并持久化到
`QSettings("LandCoverClassification/model_root")`。

## 外部模型资产与本机发现

真实 PyTorch bundle 由外部提供，放置在上述模型根目录下，但默认被 `.gitignore` 忽略，因此远端仓库和干净检出中通常只保留 `.gitkeep`。这表示模型资产不入库，不表示当前工作区没有可用模型。

排查模型选择、`postprocess.json` 参数、DEM 因子或推理结果时，必须先枚举模型根目录中实际存在的本机 bundle，再读取所选 bundle 的配置。当前工作区用于验证的外部 bundle 为 `landslide_mitb2_dem_50m_v1/`；它不是插件代码的固定默认值，且不得提交其 `weights.pt` 或其他 bundle 文件。

## PyTorch Bundle

每个可用模型必须放在独立子目录中，目录结构如下：

```text
models/semantic_segmentation/
└── landslide_smoke_v0/
    ├── manifest.json
    ├── weights.pt
    ├── arch.py
    ├── dem_factors.py
    ├── preprocess.json
    ├── postprocess.json
    └── README.md
```

### manifest.json

`manifest.json` 是插件识别 bundle 的入口，必须包含：

```json
{
  "schema_version": 2,
  "framework": "pytorch",
  "task": "semantic_segmentation",
  "display_name": "Landslide MIT-B2 DEM v3",
  "weights": "weights.pt",
  "class_names": ["background", "landslide"],
  "landslide_class_id": 1
}
```

`schema_version` 可以存在，但推理侧不通过单一版本号判断兼容性；有效性由 `framework`、`task`、`postprocess.json`、`dem_factors.py.FACTOR_NAMES`、规则结构和模型实际输入能力共同校验。bundle 子目录名仅用于组织文件，不参与兼容性判断。
### arch.py

`arch.py` 必须提供：

```python
def build_model(cfg):
    ...
```

插件会通过 `importlib.util.spec_from_file_location` 加载该文件，不会从训练仓库 import 任何代码。
`build_model()` 返回的模型会加载 `weights.pt`，然后在独立 PyTorch venv 子进程中执行滑窗推理。

生产推理使用“核心区 + halo”的窗口化流程：影像通过 `rasterio.windows.Window` 读取，DEM 只重投影到当前局部格网，DEM 因子计算后裁掉 halo，概率与类别结果直接按窗口写入 tiled BigTIFF。插件自动为生产中间栅格和最终类别 GeoTIFF 启用 BigTIFF，不暴露文件大小上限配置。halo 同时覆盖模型上下文、坡度/坡向计算邻域，以及 TPI、relief 最大米制窗口。形态学使用有限邻域 halo，填洞和连通域面积过滤通过磁盘中间栅格进行跨块全局归并，不按块截断对象。

DEM 局部读取使用 `masked=True` 保留数据集 NoData 语义，但传入 `rasterio.warp.reproject()` 前必须转换为带 NaN NoData 的普通 `float32 ndarray`。不要把 `MaskedArray` 直接交给重投影函数，因为不同 rasterio/GDAL 组合可能把实际有效的局部 DEM 误处理为全 NoData。

### preprocess.json 影像波段

`preprocess.json` 可声明 1-based 的模型影像波段和 SAM RGB 波段：

```json
{
  "image_bands": [1, 2, 3],
  "sam_rgb_bands": [1, 2, 3],
  "image_mean": [0.485, 0.456, 0.406],
  "image_std": [0.229, 0.224, 0.225]
}
```

`image_bands` 未声明时保持旧行为，模型读取全部波段；声明后必须为正整数、无重复且不越界，其数量必须与显式模型影像通道及 `image_mean/image_std` 长度一致。`sam_rgb_bands` 必须恰好包含三个不同的有效波段；未声明时依次采用 GDAL RGB 颜色解释、单波段复制或前三波段。双波段数据无法自动构造 RGB，必须显式声明。

### dem_factors.py

`dem_factors.py` 必须声明固定通道顺序并提供显式契约调用入口：

```python
FACTOR_NAMES = ["slope", "aspect_sin", "aspect_cos", "tpi", "relief"]


def compute_factors(dem_array, transform, dem_factors, crs_unit):
    ...
```

返回值可以是 `dict`，也可以是形如 `[C, H, W]` 的数组；数组通道必须与 `FACTOR_NAMES` 和 `postprocess.json.dem_factors` 完全一致。推理侧不再提供 `5x5 pixels` 或旧默认通道顺序回退。

当 `postprocess.json.dem_factors.*.scale_mode` 为 `meters` 时，输入影像 CRS 单位必须为米制，`dem_factors.py` 应按 `window_m / 像素大小` 换算窗口像素数，而不是写死固定像素窗口。
## DEM 后处理规则

PyTorch 推理中的 DEM 文件为可选项。未选择 DEM 时，模型 DEM 分支接收归一化后的全零中性输入；DEM 只覆盖目标范围的一部分时，覆盖区使用真实因子，缺失区使用中性输入。插件按窗口把有效 DEM 重投影到输入影像局部格网，调用 bundle 内的 `dem_factors.py` 计算派生因子，然后执行形态学和最小面积后处理。DEM 规则仅在组件真实有效覆盖率达到 50% 时执行；无覆盖、全 NoData 或低覆盖组件会记录跳过原因，但不会跳过阈值化和形态学处理。损坏文件、缺少 CRS、非法变换或真正的重投影错误仍会终止推理。

`postprocess.json` 必须显式声明 DEM 因子契约、训练数据分辨率和规则结构：

```json
{
  "schema_version": 2,
  "threshold": 0.6,
  "dem_factors": {
    "slope": {"method": "gradient", "unit": "degree"},
    "aspect_sin": {"method": "aspect_sin", "unit": "ratio"},
    "aspect_cos": {"method": "aspect_cos", "unit": "ratio"},
    "tpi": {
      "method": "center_minus_local_mean",
      "scale_mode": "meters",
      "window_m": 50.0,
      "unit": "m"
    },
    "relief": {
      "method": "local_max_minus_min",
      "scale_mode": "meters",
      "window_m": 50.0,
      "unit": "m"
    }
  },
  "training_data": {
    "image_resolution_m": 2.388657,
    "dem_resolution_m": 12.5,
    "crs_unit": "m"
  },
  "min_area_m2": 300,
  "rules": {
    "slope": {
      "enabled": false,
      "slope_min_deg": 8.0,
      "factor": "slope",
      "stat": "median",
      "operator": ">="
    },
    "relief": {
      "enabled": false,
      "relief_min_m": 5.0,
      "factor": "relief",
      "stat": "median",
      "operator": ">="
    },
    "tpi": {
      "enabled": false,
      "tpi_max_ridge": 4.0,
      "factor": "tpi",
      "stat": "mean",
      "operator": "<="
    }
  },
  "rule_order": ["slope", "relief", "tpi"]
}
```

规则即使禁用也必须通过结构校验。支持的 `stat` 为 `median`、`mean`、`min`、`max`，支持的 `operator` 为 `>=`、`>`、`<=`、`<`。规则比较阈值继续使用语义字段：`slope_min_deg`、`relief_min_m`、`tpi_max_ridge`。

后处理顺序为：模型 landslide 概率图、阈值化、可选形态学处理、8 连通域、最小面积过滤、按 `rule_order` 执行规则。每次运行会写出：

`<output>.postprocess.json`

该文件包含 `dem_factors`、`training_data`、运行时分辨率、分辨率差异告警、规则契约、每个 component 的面积、保留/丢弃决策和触发规则，便于审计与调参。

## landslide 工作区契约

会话草稿工作区固定映射为 `background=0`、`landslide=1`。启动推理前，插件会在 `manifest.json.class_names` 中以大小写不敏感的方式查找唯一的 `landslide`；若声明 `landslide_class_id`，它必须和该索引一致。缺失、重复或冲突会在启动子进程前明确失败，而不会把其他类别写入草稿。

运行器还可接收由界面生成的 `roi` 参数，用于“按当前画布范围推理”。其 `mode` 必须为 `canvas_intersection`，`bounds` 必须是输入影像 CRS 下的 `[xmin, ymin, xmax, ymax]`，并同时记录 `crs_wkt`。ROI 先向外取整到影像像素并裁剪到影像边界；没有像素交集必须失败，不能退化为全图推理。核心窗口只在 ROI 内写入，halo 可以超出 ROI 读取上下文；审计 JSON 会记录模式、边界、像素窗口和 halo。

绘制闭合范围使用 `mode: "drawn_polygon"`。除相同 CRS 下的 `bounds` 和 `crs_wkt` 外，还必须提供 GeoJSON Polygon `geometry`；只允许一个闭合外环，不允许孔洞或 MultiPolygon。运行器会从 geometry 重新计算并校验 bounds，以像素中心规则生成每个 core 的局部掩膜；曲线外概率保持 NaN、有效掩膜保持 0，形态学结果也必须裁回真实 polygon。