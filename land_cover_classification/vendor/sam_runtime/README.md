# SAM 运行时离线环境

本目录用于承载 AI 编辑功能(SAM1 ViT-B)所需的独立 Python 虚拟环境
与离线 wheel 文件。插件主体只负责调度 SAM 子进程,不在 QGIS 主进程
内 import torch、segment_anything 等重型依赖,以保持稳定性。

## 目录约定

- `requirements-sam.txt`  SAM 子进程依赖列表(锁定版本)
- `wheels/`               由用户按目标机环境准备的离线 wheel 文件目录
- `create_sam_venv.bat`   Windows 下离线创建 SAM venv 的入口脚本
- `create_sam_venv.sh`    Linux/macOS 下离线创建 SAM venv 的入口脚本
- `LICENSE_THIRD_PARTY/`  PyTorch、segment-anything 等第三方许可证

创建后的 venv 默认位于 `vendor/sam_runtime/venv/`,插件通过
`sam_deps_check.py` 中的 `default_python_executable()` 查找该解释器。
`venv/` 是本机生成目录,不应提交到 git,也不建议直接拷贝到其他机器复用。

## 最低依赖

- Python 3.8 或与所选 wheels 匹配的 Python 版本
- PyTorch 1.x 或 2.x 的 CPU/GPU 版本
- segment-anything
- numpy
- opencv-contrib-python

## 离线 wheels

仓库仅保留 `wheels/` 目录占位,不直接分发实际 wheel 文件。请根据目标机环境
自行准备并放入该目录,然后再创建 `venv/`。

如需准备或更新离线 wheels,可参考:

```bash
pip download -r requirements-sam.txt -d wheels --no-deps --platform <platform>
```

对于 Windows CPU 用户,通常应准备 `win_amd64` 的 wheel。若改用 GPU 版
PyTorch,请同步替换 `torch`、`torchvision` 及相关依赖文件。

## 创建 venv

- Windows:

```bat
create_sam_venv.bat
```

- Linux/macOS:

```bash
./create_sam_venv.sh
```

如需指定 Python 解释器,可通过 `SAM_PYTHON` 环境变量覆盖默认值。
创建完成后,插件会自动使用 `vendor/sam_runtime/venv/` 中的解释器启动
`sam_worker.py`。

## 许可证

本目录不复制 segment-anything、PyTorch 源代码,仅在创建 venv 时通过
pip 安装。第三方许可证副本放置于 `LICENSE_THIRD_PARTY/`,请保留与发布。
