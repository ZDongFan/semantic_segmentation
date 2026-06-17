# 运行时依赖安装

本文档覆盖两部分内容：

1. QGIS 主进程内运行 PaddleRS 语义分割所需依赖。
2. AI 辅助编辑所需的独立运行环境，使用 SAM2.1 Base+。

插件首次启动时会检查依赖并弹窗给出安装提示。下面给出完整手动安装流程。

## 一、部署插件目录

将仓库中的 `land_cover_classification/` 目录复制到 QGIS 插件目录。

- Windows 默认目录：

```text
%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification
```

- Linux 默认目录：

```text
~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/land_cover_classification
```

## 二、安装 PaddleRS 运行依赖

PaddlePaddle **必须** 从飞桨官方 wheel 镜像源安装。默认 PyPI 不提供本插件当前沿用的
`paddlepaddle==2.4.2` mkl/avx 版本。

### Windows（OSGeo4W Shell）

打开 **OSGeo4W Shell**，确保 QGIS 自带的 Python 在 `PATH` 中，然后执行：

```bash
python -m pip install paddlepaddle==2.4.2 -f https://www.paddlepaddle.org.cn/whl/windows/mkl/avx/stable.html
python -m pip install -r "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification\vendor\PaddleRS\requirements.txt"
python -m pip install --upgrade python-dateutil
```

### Linux

使用与 QGIS 一致的 Python 运行：

```bash
python -m pip install paddlepaddle==2.4.2 -f https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html
python -m pip install -r ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/land_cover_classification/vendor/PaddleRS/requirements.txt
python -m pip install --upgrade python-dateutil
```

### GPU（CUDA）

如需使用 GPU 版 Paddle，请把 `paddlepaddle` 替换成
`paddlepaddle_gpu==2.4.2.post<CUDA版本>`。

### GDAL 说明

GDAL 一般由 QGIS 自带。若依赖检查提示 GDAL 缺失，请优先重装 QGIS，不要直接用 `pip` 安装 GDAL。

## 三、准备语义分割模型

将导出的 PaddleRS 分割模型子目录放入：

```text
land_cover_classification/models/semantic_segmentation/
```

每个模型目录通常至少包含：

- `model.yml`
- `model.pdmodel`
- `model.pdiparams`

## 四、准备 SAM AI 编辑资源

默认 SAM2 权重文件路径如下。若当前仓库副本未包含对应权重文件，请由用户或部署方按目标机环境自行准备。

- `land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt`

### 1. 准备模型权重

默认模型权重文件路径：

```text
land_cover_classification/models/sam2/sam2.1_hiera_base_plus.pt
```

请将 `sam2.1_hiera_base_plus.pt` 放到上述默认路径。

如需升级或替换为其他 SAM2.1 规格，除了替换权重文件，还需要同步修改插件代码中的默认模型路径、模型类型与配置路径。

### 2. 创建 SAM 专用虚拟环境

SAM 相关依赖**不要安装到 QGIS Python 环境**。请使用插件自带脚本创建独立 venv：

- Windows：

```bat
land_cover_classification\vendor\sam_runtime\create_sam_venv.bat
```

- Linux/macOS：

```bash
land_cover_classification/vendor/sam_runtime/create_sam_venv.sh
```

默认脚本会在 `land_cover_classification/vendor/sam_runtime/venv/` 下创建本机专用环境，并按当前脚本逻辑安装所需依赖。SAM2 默认优先使用 Python 3.12。

如需指定创建 venv 所使用的 Python，可先设置 `SAM_PYTHON` 环境变量。

- Windows 示例：

```bat
set SAM_PYTHON=C:\Python312\python.exe
land_cover_classification\vendor\sam_runtime\create_sam_venv.bat
```

- Linux/macOS 示例：

```bash
SAM_PYTHON=python3.12 land_cover_classification/vendor/sam_runtime/create_sam_venv.sh
```

### 3. 重建或更新 venv

重建前请手动删除旧的：

```text
land_cover_classification/vendor/sam_runtime/venv/
```

`venv/` 是本机生成目录，不建议复制到其他机器复用，也不应提交到 git。

## 五、验证安装

### 1. 验证 PaddleRS 主依赖

重启 QGIS 后，在 QGIS Python 控制台中执行：

```python
import paddle; print(paddle.__version__)
import paddlers
import cv2, yaml
from osgeo import gdal
```

以上 import 全部成功即可。

### 2. 验证 SAM 独立环境

在插件目录下执行：

```bash
python land_cover_classification/sam_deps_check.py --backend sam2
```

若输出包含 `SAM2 runtime ready` 且返回码为 `0`，说明以下条件已满足：

- `sam2.1_hiera_base_plus.pt` 已就位
- `vendor/sam_runtime/venv/` 已创建
- `torch`、`torchvision`、`sam2`、`cv2`、`numpy` 可从该 venv 正常导入

## 六、首次使用 AI 编辑

1. 在插件中先完成一次推理，生成草稿图层。
2. 切换到 `编辑与导出` 页签。
3. 点击“启动 AI 编辑”。
4. 在画布上左键添加正样本点，右键添加负样本点。
5. 预览满意后点击“追加草稿对象”写回草稿层。
