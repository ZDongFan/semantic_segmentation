# 模型目录结构

插件会扫描指定的模型根目录，把每个一级子目录识别为一个 PyTorch 语义分割 bundle。
默认模型根目录为：

`land_cover_classification/models/semantic_segmentation/`

该路径可在插件对话框的“模型根目录”字段中修改，并持久化到
`QSettings("LandCoverClassification/model_root")`。

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

`schema_version` 可以存在，但推理侧不再通过它判断新旧兼容性；有效性完全由 `postprocess.json`、`dem_factors.py.FACTOR_NAMES` 和规则结构校验决定。
### arch.py

`arch.py` 必须提供：

```python
def build_model(cfg):
    ...
```

插件会通过 `importlib.util.spec_from_file_location` 加载该文件，不会从训练仓库 import 任何代码。
`build_model()` 返回的模型会加载 `weights.pt`，然后在独立 PyTorch venv 子进程中执行滑窗推理。

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

PyTorch 推理必须同时选择与输入影像覆盖范围相交的 DEM 文件。插件会把 DEM 重投影到输入影像格网，调用 bundle 内的 `dem_factors.py` 计算派生因子，然后执行规则后处理。

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
## Legacy PaddleRS

`vendor/PaddleRS/` 和旧推理入口以 `_inference_paddlers_legacy.py`、
`_inference_runner_paddlers_legacy.py` 形式保留在仓库中，但插件 UI 不再扫描或调用
PaddleRS 模型。

如果模型子目录只包含 `model.yml`，插件会在 QGIS 消息日志中记录“跳过遗留 PaddleRS 模型”，
但不会把它加入模型下拉框。请使用训练仓库重新训练并导出 PyTorch bundle 后放入模型根目录。
