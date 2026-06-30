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
  "schema_version": 1,
  "framework": "pytorch",
  "task": "semantic_segmentation",
  "display_name": "Landslide smoke v0",
  "weights": "weights.pt",
  "class_names": ["background", "landslide"],
  "landslide_class_id": 1
}
```

当前插件消费侧支持的 `schema_version` 为 `1`。版本不匹配时，插件会拒绝运行并提示重新导出 bundle。

### arch.py

`arch.py` 必须提供：

```python
def build_model(cfg):
    ...
```

插件会通过 `importlib.util.spec_from_file_location` 加载该文件，不会从训练仓库 import 任何代码。
`build_model()` 返回的模型会加载 `weights.pt`，然后在独立 PyTorch venv 子进程中执行滑窗推理。

### dem_factors.py

`dem_factors.py` 必须提供：

```python
def compute_factors(dem_array, transform):
    ...
```

返回值可以是 `dict`，也可以是形如 `[C, H, W]` 的数组。数组通道默认按
`elevation, slope, relief, tpi, aspect` 解释；如需自定义顺序，可在 `postprocess.json`
中提供 `factor_names`。

## DEM 后处理规则

PyTorch 推理必须同时选择与输入影像覆盖范围相交的 DEM 文件。插件会把 DEM 重投影到输入影像格网，调用 bundle 内的 `dem_factors.py` 计算派生因子，然后执行规则后处理。

`postprocess.json` 可配置概率阈值、最小面积和三条规则：

```json
{
  "schema_version": 1,
  "threshold": 0.5,
  "min_area_m2": 500,
  "rules": {
    "slope": {"enabled": true, "slope_min_deg": 8.0},
    "relief": {"enabled": true, "relief_min_m": 5.0},
    "tpi": {"enabled": true, "tpi_max_ridge": 4.0}
  }
}
```

后处理顺序为：模型 landslide 概率图、阈值化、3×3 开运算、8 连通域、最小面积过滤、
`slope -> relief -> TPI` 规则流水线。每次运行会写出：

`<output>.postprocess.json`

该文件包含每个 component 的面积、规则指标、保留/丢弃决策和触发规则，便于审计与调参。

## Legacy PaddleRS

`vendor/PaddleRS/` 和旧推理入口以 `_inference_paddlers_legacy.py`、
`_inference_runner_paddlers_legacy.py` 形式保留在仓库中，但插件 UI 不再扫描或调用
PaddleRS 模型。

如果模型子目录只包含 `model.yml`，插件会在 QGIS 消息日志中记录“跳过遗留 PaddleRS 模型”，
但不会把它加入模型下拉框。请使用训练仓库重新训练并导出 PyTorch bundle 后放入模型根目录。
