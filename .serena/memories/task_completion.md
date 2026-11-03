# 完成验证

- 纯 Python 逻辑：优先运行受影响的 `unittest` 脚本，至少运行相关 `py_compile`。
- 改主推理：编译 `pytorch_deps_check.py`、`pytorch_inference_core.py`、`pytorch_inference_runner.py`、`model_scan.py`、`land_cover_classification_dialog.py`。
- 改 DEM 契约/规则/形态学：运行 `land_cover_classification/test/test_rules_synthetic.py`，覆盖契约、rule_order、禁用规则、组件保留/丢弃；检查审计 JSON 字段。
- 改输入适配或第三方模型兼容：运行 `test_pytorch_input_adapter.py`，覆盖 dual/concat 输入与 decoder API 兼容。
- 改 CPython 预置包：覆盖精确平台选择、只读 SHA 校验、损坏/目录/链接拒绝、归档安全、取消、已有环境优先、缓存与续传回退；验证 Git 忽略与发布目录可附带包。真实引导在临时目录验证，不修改现有 runtime。
- 改 runtime：确认打开面板仅检查解释器存在性，缺少时引导而不自动安装；验证 QProcess 日志、取消、重复打开、成功不自动推理。覆盖固定资产校验、目录保留、并发锁、环境隔离、Torch 约束及 Windows/Linux CUDA 回退；复验功能检查与两套依赖检查。模拟结果不替代目标平台实测。
- 改 QGIS/UI/导出/AI：在 QGIS 手动验证入口、运行、草稿层、GeoTIFF/Shapefile/DXF 导出，及 AI 正负点、撤销、清空、停止、恢复点选和预览约束。
- 端到端推理需使用地理参考影像及相交 DEM，确认输出分类 GeoTIFF、后处理 JSON、草稿矢量和日志。
- 文档改动：保持 AGENTS.md/CLAUDE.md 正文同步，核对相对链接及源码入口；历史验收记录与本次验证分开报告。
- 完成源码或文档更新后运行 `codegraph.cmd sync .`、`codegraph.cmd status .`，查询新增/变更符号确认索引生效；同步相关 Serena 记忆，不手改符号缓存或 CodeGraph 数据库。
- 完成后保留用户既有工作区改动；不自动提交、建分支或推送。
- 改 Torch 来源：覆盖官方 CUDA 候选顺序、`SAM_TORCH_CPU_INDEX` / `SAM_TORCH_CUDA_INDEX` / `SAM_TORCH_CUDA_INDEXES` 自定义来源、`SAM_TORCH_PACKAGES` 约束、隔离 pip 配置、GPU 验证失败后的 CPU 回退，以及 macOS 官方 PyPI 分支；真实安装使用独立测试 venv，不修改现有 runtime，并记录未实测平台。

- 改属性/版本/恢复：运行 `test_independent_workspaces.py`、既有工作区与项目回归；覆盖真实地图点选、QObject 销毁、generation 换代、分页、跨身份拒绝、恢复取消/失败保留和下载摘要。版本叠加清理须验证 QgsRubberBand 释放。
- 改内部历史接口：运行 `backend/tests/test_history.py`、`test_api.py`、`test_postgis.py`；真实事务与几何比较在独立测试数据库验收，同时确认公共五个 GET 接口不变。部署前区分源码完成与运行进程已加载，保护用户当前编辑；新增路由需重启后端。
