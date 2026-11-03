# 技术栈

- Python、PyQt/QGIS API；最低 QGIS 3.44，元数据在 `metadata.txt`，打包配置在 `pb_tool.cfg`。
- 重依赖绝不装入/导入 QGIS Python：`torch`、`torchvision`、`sam2`、`rasterio`、`segmentation_models_pytorch` 等均由统一 venv 子进程使用。
- 唯一运行时：`land_cover_classification/vendor/sam_runtime/venv/`，同时服务 `pytorch_inference_runner.py` 与 `sam_worker.py`。创建入口为 `create_sam_venv.bat` / `bash create_sam_venv.sh`，优先使用 cpython_packages/ 中精确文件名的预置包，否则下载 python-build-standalone CPython 3.12.12 / 20251014；公共实现为 `runtime_setup.py`，Windows 内联 PowerShell，独立 `.ps1` 已删除。Windows 使用 LOCALAPPDATA 下的固定实体目录和目录联接；Linux/macOS 支持自定义 venv 符号链接。
- 运行时依赖：torch、torchvision、sam2、opencv-contrib-python、numpy、Pillow、segmentation-models-pytorch==0.4.*、timm、rasterio、scipy、PyYAML。Windows/Linux 默认使用 PyTorch 官方 CPU/CUDA wheel 索引，保留 `SAM_TORCH_CPU_INDEX`、`SAM_TORCH_CUDA_INDEX`、`SAM_TORCH_CUDA_INDEXES` 与 `SAM_TORCH_PACKAGES`；CUDA wheel 安装、加载或 GPU 验证失败后回退 CPU。macOS 使用官方 PyPI、禁用 SAM2 CUDA 扩展并按 CPU 验证，Intel 当前因官方 Torch/SAM2 依赖无交集而明确失败。
- 依赖检查模块：`pytorch_deps_check.py` 和 `sam_deps_check.py`；二者复用公共 `clean_environment()` 提供清洁 `runtime_environment()`，启动子进程必须使用，隔离 QGIS/OSGeo 环境变量。
- 模型 bundle：默认根 `models/semantic_segmentation/`；每个 bundle 至少含 `manifest.json`、`weights.pt`、`arch.py`、`dem_factors.py`。SAM2 默认权重为 `models/sam2/sam2.1_hiera_base_plus.pt`。
- 测试为 `unittest`，位于 `land_cover_classification/test/`；重点测试 `test_rules_synthetic.py` 和 `test_pytorch_input_adapter.py`。

- 内部项目服务为独立 FastAPI + PostgreSQL/PostGIS，Fiona/Shapely/pyproj 处理成果，运行于 `.venv-backend/`；其 pytest 测试在 `backend/tests/`，与 QGIS unittest、推理/SAM venv 分离。
- 属性及恢复 UI 使用现有 PyQt/PyQGIS，无新增重依赖；`attribute_browser.py` 按页读取草稿，`history_dialogs.py` 消费内部历史接口，`recovery.py` 复用 SQLite 快照与原子 generation 切换。模块一并在 `pb_tool.cfg` 打包。
