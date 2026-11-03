# 项目约定

- 保持 QGIS 主进程轻量：打开面板时只检查 venv 解释器文件是否存在；缺少时显示安装引导，点击“开始安装”才用 QProcess 执行安装。安装成功不自动推理；推理和 AI 按钮缺少环境时只提示重新打开插件。存在性检查不等于依赖健康检查；SAM 依赖仍只在启动 AI 编辑时检查。
- 所有 PyTorch/SAM 子进程必须使用对应 `runtime_environment()`，二者复用 `vendor/sam_runtime/runtime_setup.py` 的 `clean_environment()`；安装也复用此清理逻辑，去除 Python/GDAL/PROJ/Qt/动态库变量及 PATH 中 QGIS/OSGeo4W 项。
- 安装固定独立 CPython 3.12.12 / 20251014，不探测 QGIS/系统 Python。已有完整环境只验证，异常目录保留并报错；不自动修复、删除、迁移或下载模型。`SAM_PYTHON` 不再生效，`SAM_RECREATE` 已取消；Windows 拒绝 `SAM_VENV_DIR`，仅 Linux/macOS 支持。保留下载校验、安装锁、取消与脱敏日志。
- UI 不再扫描或调用 legacy PaddleRS `model.yml`；`vendor/PaddleRS/` 与 `_inference_*_paddlers_legacy.py` 仅作历史保留。
- 推理只接受 v3 显式米制 DEM 契约：`dem_factors.py.FACTOR_NAMES` 必须严格为 slope、aspect_sin、aspect_cos、tpi、relief；`postprocess.json.dem_factors` 必须完全一致；tpi/relief 需 `scale_mode: "meters"`、正 `window_m` 和 `unit: "m"`，旧隐式像素窗口必须失败。
- bundle 的 `arch.py` 提供 `build_model(cfg)`；兼容第三方 API 差异时采用 bundle 自描述、能力/签名检测或明确版本范围，不得只适配本机依赖。
- DEM 后处理在形态学、连通域与面积过滤后按 `rule_order` 执行。禁用规则仍须通过结构校验；审计 JSON 应保留规则契约、形态学计数、组件保留/丢弃与原因。
- AI 点提示必须保留 crop、低分辨率 mask 迭代、活动 crop 复用、crop 外负点过滤、失败兜底和负点硬约束；几何 refine 要保留单面/顶点/面积预算、makeValid、负点覆盖检查和逐级回退。
- AI 点提示在 `canvasReleaseEvent()` 中添加并触发预测；不得在鼠标按下回调中同步等待预测，避免事件重入后画布按下状态卡住并拦截 E 键。回归测试应通过真实画布分发鼠标与键盘事件。
- 新增代码注释和 docstring 使用中文；不混入无关重构、格式化或本机绝对路径；真实模型、权重、样例影像和 venv 都是本机资产，不提交。
- 修改 runtime、bundle、推理、AI 协议或 UI 时按 AGENTS.md 的联动文件清单检查。
- 需要取得 CPython 归档时，两个创建入口先检查 `vendor/sam_runtime/cpython_packages/` 下当前平台的精确官方文件名（字面量 `+`）；版本、构建与固定 SHA 不变。匹配包必须为普通文件，通过 SHA-256 和归档安全检查后只读使用；损坏、目录、链接或重解析点必须失败并保留，不联网绕过。没有匹配包才进入现有缓存、完整 `.part` 与联网续传；其他平台、版本、未完成文件不参与选择。已有环境或独立 Python 仍按原规则处理。下载说明及各系统链接统一维护在 docs/install.md，预置目录允许提交 .gitkeep 与固定 Windows 包 cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz；其他平台、版本的归档和临时文件仍忽略，发布暂存目录可人工附带。
- CPython 下载必须在安装锁保护下写入固定 `<SHA-256>.tar.gz.part`，断流、超时及取消保留并支持跨次续传；最多 3 次显式尝试，退避 2/5 秒，每 10 秒输出增量速度和累计大小，零进展也输出。取消约每 200 毫秒检查，等待 curl 退出后释放锁。缓存与完整 `.part` 均需 SHA-256 校验后才发布或解压；Range 不支持、416 且校验失败、SHA 不匹配均停止保留，不覆盖下载。拒绝临时路径链接/重解析点/目录；旧 `.part-<PID>` 不自动迁移或清理。

- Windows/Linux 的 Torch/torchvision 默认从 PyTorch 官方 CPU/CUDA wheel 索引安装；保留 `SAM_TORCH_CPU_INDEX`、`SAM_TORCH_CUDA_INDEX`、`SAM_TORCH_CUDA_INDEXES` 和 `SAM_TORCH_PACKAGES`。Torch 步骤清除通用 `PIP_*` 来源、禁用 pip 配置并使用显式索引和 wheel-only 约束；按驱动能力尝试 CUDA 候选，安装、加载或 GPU 张量验证失败后回退 CPU。macOS 使用官方 PyPI 并按 CPU 验证。通用依赖保持清华默认镜像和 `PIP_INDEX_URL` 覆盖，必须用精确 Torch 约束防止通用安装替换专用 wheel。

- 项目页置首，服务地址来自 project_service.ini，内部管理免 API Key，后端自动读取 .env；用户先选项目，输入自动关联，切换先保存并恢复草稿。独立按钮一次发布全部已保存成果，文件导出不触发发布；generation 不等于公开版本。内部/公共 FastAPI 应用隔离，发布使用项目行锁、基础版本和幂等标识；公共分页固定 version_id。后端独立 venv 不修改推理 runtime。部署和验证见 docs/project_service.md、docs/project_service_validation.md。

- 项目输入不设目录白名单，影像/DEM 采用 QGIS 选择的实际路径，后端检查普通文件可读取；保留 LCC_STORAGE_ROOT 及成果下载目录边界，数据库关系不变。


- 成果共享契约位于仅依赖标准库的 result_attributes.py：17 个标准字段，正确拼写 ygtsm，两个 tid/bz 分别合并；tid 为影像 UUID.hex，项目创建选填 ztz（规范化后最多 32 字符），mj 为最终几何 WGS84 椭球平方米面积，其他标准字段为空。公开 area_m2 归并为 mj；class_name 保留、lx 为空，不把周长或版本时间映射到 cd/rq。Shapefile 保留三公开字段后共 20 字段，mj 使用 Double(24,3)；GeoPackage/API 共 29 项、不舍入，空值完整输出。覆盖文件前校验影像/项目/草稿关联；不扩展草稿字段或 GeoTIFF/DXF。
- 退出保存为尽力保存：QtNetwork + QFile 流式上传，单个 QTimer 总期限 5 秒、超时 abort、不重试；失败/离线/忙碌不拦截 Close、Escape、dock 或 unload。QGIS 自身退出确认不受干预；同轮只尝试一次，取消退出/重新打开可重试。普通保存/切换/发布前失败保护保留。后台普通线程和快照独立持有至操作结束，不无限 wait/terminate；失败或跳过解除图层引用但保留 generation 路径，日志明确未确认成功及编辑缓冲风险，不弹阻塞确认。
- 未交付旧项目仅允许显式一次性重置，核实六表与文件路径、停写后事务清空、不用 CASCADE；只删除已登记关联文件，保留原始资产及未知文件。不得把清理或旧属性兼容放入启动和日常业务。

- 项目页成果属性预览须复用 `result_attributes.RESULT_FIELDS` 与 `build_result_attributes` 显示全部 17 个标准字段，空值在界面显示为空白，实际数据及后端记录保持 NULL，保留分类与几何信息；校验当前项目、影像和草稿一致。预览只读，不扩展草稿 schema，不提前生成发布版本元数据。

- UI 工作区通过 workflow_ui.py 统一推导按钮条件和摘要；项目/模型/推理/AI/导出状态独立。有效空草稿可保存、预览、导出；同步标记绑定影像与 generation，编辑缓冲优先显示未提交。推理单入口复用全图/画布/绘制的既有校验，只有融合成功才可撤销。属性常用视图不改变完整字段契约。详见 docs/ui_workspaces.md。

- UI Qt 信号生命周期：`WorkflowUi` 必须为以对话框为父对象的 QObject，外部画布信号使用 Qt 槽管理接收者寿命；刷新装饰器保留无参槽签名，避免 clicked(bool) 误传。网络请求线程在调用栈读取结果前不得 deleteLater，退出后的后台释放保持有效。AI 同影像/DEM 无操作绑定不应被自身追加忙碌状态阻止，真实切换仍保留保护。回归必须包含真实按钮点击、面板销毁后画布事件和请求返回前 DeferredDelete。

- 后续独立工作区：attribute_browser.py 以非模态分页预览联动地图；history_dialogs.py 比较已发布版本；recovery.py 登记成功 generation，恢复前备份并原子切换，仅本地待同步。详情与边界见 docs/ui_workspaces.md 和 AGENTS.md。

- 属性每页最多 200 个，预览使用共享成果字段但不改草稿 schema；窗口固定项目/影像身份，generation 换代刷新、图层移除失效，关闭恢复地图工具。地图/窗口的外部信号必须考虑 QObject 寿命。
- 历史对比固定两个版本，按 image_id + feature_id 匹配；ST_Equals 比较几何，属性只排除 version_id/created_at。内部空版本列表与公共未发布失败语义不同；不能为复用接口改变公共契约。QgsRubberBand 从场景移除再释放，不是 QObject。
- 恢复仅使用匹配身份的成功 generation 记录；下载核对 ID/SHA-256，恢复前备份，失败保留源和原 generation；恢复只产生本地待同步草稿。旧无身份记录文件须人工选择和确认，不能自动推断关联，不承诺未提交编辑缓冲的崩溃恢复。
