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

PyTorch 推理必须同时选择与输入影像覆盖范围相交的 DEM 文件。插件按窗口把 DEM 重投影到输入影像的局部格网，调用 bundle 内的 `dem_factors.py` 计算派生因子，然后执行规则后处理。错误“当前推理块范围内未获得有效 DEM 高程”表示局部输出全为 NoData；空间范围可能仍然相交，应继续检查交叠区 NoData、CRS 和重投影是否正常。

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
