# 运行时依赖安装

插件首次启动时会检查依赖并弹窗给出与你当前平台对应的安装命令。
下面是参考命令,可在弹窗外手动复制使用。

PaddlePaddle **必须**从飞桨官方 wheel 镜像源安装 —— 默认 PyPI 不提供
本插件需要的 mkl/avx 优化版本。版本须为 `2.4.2`(沿用 PaddleRS 1.x 的
版本约束)。

## Windows(OSGeo4W Shell)

打开 **OSGeo4W Shell**,确保 QGIS 自带的 Python 在 `PATH` 中,然后执行:

```
python -m pip install paddlepaddle==2.4.2 -f https://www.paddlepaddle.org.cn/whl/windows/mkl/avx/stable.html
python -m pip install -r "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\land_cover_classification\vendor\PaddleRS\requirements.txt"
python -m pip install --upgrade python-dateutil
```



## Linux

使用与 QGIS 一致的 Python(大多数发行版下就是系统 Python)运行:

```
python -m pip install paddlepaddle==2.4.2 -f https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html
python -m pip install -r ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/land_cover_classification/vendor/PaddleRS/requirements.txt
python -m pip install --upgrade python-dateutil
```

## GPU(CUDA)

把 `paddlepaddle` 换成 `paddlepaddle-gpu==2.4.2.post<CUDA 版本>`
(例如 CUDA 11.7 对应 `paddlepaddle-gpu==2.4.2.post117`),并到
<https://www.paddlepaddle.org.cn/install/quick> 选择对应平台的 `-f`
镜像源 URL。

## GDAL

GDAL 一般由 QGIS 自带(Windows 来自 OSGeo4W,Linux 来自系统 QGIS 包)。
如果依赖检查提示 GDAL 缺失,请**重装 QGIS**,不要尝试用 pip 装 GDAL ——
pip wheel 与 QGIS Python 的兼容性非常脆弱。

## 验证

依赖装好后重启 QGIS。点击插件图标应当不再弹出依赖提示窗。在 QGIS 的
Python 控制台中:

```python
import paddle; print(paddle.__version__)   # 应输出 2.4.2
import paddlers
import cv2, yaml
from osgeo import gdal
```

以上 import 全部成功即代表依赖已就位。
