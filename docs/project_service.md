# 内部项目管理与对外成果查询

本实现按项目管理计划及后续交互修订实现。后端位于 `backend/`，插件入口为“项目”页。后端不执行推理；QGIS 仍使用原有 PyTorch、SAM 和临时 generation 管线。

## 部署前提

- 独立 Python 3.12；不要将服务依赖安装到 QGIS Python 或插件推理 runtime。
- PostgreSQL 15+ / PostGIS 3+，管理员先创建专用数据库。首次建表需要安装 PostGIS 扩展的权限，日常服务无需管理员权限。
- 影像和 DEM 直接来自 QGIS 选择，不限制输入所在目录。后端仅检查文件存在、属于普通文件且服务账户可以读取；影像格式和地理参考仍由 QGIS 与推理流程校验。`LCC_STORAGE_ROOT` 保留为草稿、版本和审计的统一管理目录。
- Windows 局域网推荐用相同 UNC 路径登记输入，QGIS 客户端和内部服务账户都必须可读；不依赖交互用户的映射盘符。
- 内外服务分别启动。内部项目管理不使用 API Key，通过仅监听本机或受控内网隔离；不得把内部端口代理到公网。外部入口只代理 public 应用，通过 HTTPS 和 `X-API-Key` 验证查询请求。
- 本机已验证 PostgreSQL 17.11 / PostGIS 3.6.2 与 QGIS 3.44.11；其他版本和生产网络部署仍需目标环境验收。

新增依赖分别用于 HTTP 服务（FastAPI/Uvicorn）、PostGIS 事务（psycopg）、GeoPackage 读写（Fiona）、几何校验（Shapely）、测地线计算（pyproj）和启动配置读取（python-dotenv）；不会改变插件现有重依赖安装策略。

## Windows 配置与启动

在仓库根目录，用独立 Python 创建环境：

```powershell
py -3.12 -m venv .venv-backend
.\.venv-backend\Scripts\python.exe -m pip install -r backend\requirements.txt
```

将仓库根目录 `.env.example` 复制为 `.env`，填写本机数据库、输入目录、成果存储和外部 Key。当前本机测试 `.env` 已配置，文件被 Git 忽略；模板不含真实密码。服务调用 `from dotenv import load_dotenv`，在读取配置前加载仓库根 `.env`，已有进程环境变量优先（`override=False`）。可用 `LCC_ENV_FILE` 指向另一份部署文件。

| 配置项 | 用途 |
| --- | --- |
| `LCC_DATABASE_URL` | PostgreSQL URI 或 libpq 连接串 |
| `PGPASSWORD` / `PGPASSFILE` | 本机数据库认证；避免将真实口令写入可提交文件 |
| `LCC_STORAGE_ROOT` | 成果存储绝对目录；输入和存储目录不得互相包含 |
| `LCC_PUBLIC_API_KEY` | 仅外部查询使用，至少 32 字符 |
| `LCC_MAX_UPLOAD_BYTES` | 单份草稿上限，默认 1 GiB |
| `LCC_TEST_DATABASE_URL` | 可选，真实集成测试专用数据库 |

生成随机 Key 可使用 `python -c "import secrets; print(secrets.token_urlsafe(32))"`。内部应用不需要 `LCC_INTERNAL_API_KEY`。不要把成果目录配置成 Web 静态根目录。

输入路径保存在 `lcc_images.image_path` / `dem_path`，通过项目、影像、草稿和版本 ID 关联成果；删除白名单不改变数据库结构和既有项目数据。后端位于另一台机器时仍须使用双方可读的共享路径。

插件由管理员配置 `land_cover_classification/project_service.ini` 的 `[service] url`，默认连接本机 8091。部署时可以通过 `LCC_PLUGIN_CONFIG` 指定独立 INI 文件。该配置随插件打包，只包含服务地址，不含数据库密码和外部 Key；界面不提供地址或凭据输入项。QGIS 客户端仍只使用标准库，不需要安装 python-dotenv。

首次由管理员显式初始化：

```powershell
.\.venv-backend\Scripts\python.exe -m backend.api --init-db
```

内部进程与对外进程分别运行，以下地址默认只监听本机，由反向代理按网络策略转发：

```powershell
.\.venv-backend\Scripts\python.exe -m uvicorn backend.api:internal_app --factory --host 127.0.0.1 --port 8091
.\.venv-backend\Scripts\python.exe -m uvicorn backend.api:public_app --factory --host 127.0.0.1 --port 8092
```

服务启停由部署方 Windows 服务管理器负责。本仓库不会自动安装系统服务或修改防火墙。

建议数据库角色权限：

- 内部角色：业务表 SELECT、INSERT，以及 `lcc_projects`、`lcc_images`、`lcc_versions` 的 UPDATE。不给 DROP、TRUNCATE 等管理权限。
- 公共角色：只对 `lcc_projects`、`lcc_versions`、`lcc_version_images`、`lcc_features` 授予 SELECT，不授予任何写权限，也不授予 `lcc_images` / `lcc_drafts` 读取权限。
- 为两种角色授予 schema USAGE 及 PostGIS 函数运行所需的权限；撤销普通账户不必要的 schema CREATE 权限。运行时不执行建表。
- 文件 ACL：内部账户可读输入、可写存储；公共账户只需读 `versions/`。公共服务不需要输入目录配置或输入文件读取权限。
- 备份数据库和受控文件目录时应采用同一一致性时间点；恢复后核对版本 SHA-256。

## QGIS 工作流程

1. 打开插件，页签按“项目 → 模型推理 → 草稿编辑 → 导出”排列。项目列表自动加载，展开时重新读取；创建项目直接请求服务。没有连接、刷新或本地模式按钮。尚未选择项目时输入区禁用。
2. 创建或选择项目后，在原输入区选择影像和可选 DEM。插件自动登记，重复选择同一路径不会创建重复影像；修改 DEM 会更新关联，保留当前草稿和已发布版本。
3. 选择另一张输入影像前自动保存当前草稿；选择已有影像会自动恢复草稿。也可直接选择“项目影像”列表继续编辑。切换项目同样先保存，失败时保留旧会话并恢复选择。
4. 继续模型推理、AI 和手工编辑。“保存当前草稿”支持主动保存；关闭插件、按 Escape 或退出 QGIS 时也保存。退出保存最多等待 5 秒网络总期限；失败、离线、超时或忙碌均放行关闭，记录非阻塞警告并保留本地 generation 目录。手动保存、切换及发布前保存失败仍保留当前会话。
5. “预览当前成果属性”显示完整 17 个标准成果字段，以及要素编号、分类、来源编号、周长和中心经纬度，共 24 列；表头包含中文名称和字段名，向右滚动查看全部字段。图编号和制图者取自当前项目影像，面积 `mj` 使用 WGS84 椭球测量；未赋值显示 NULL。预览只读，不修改草稿字段，也不创建发布版本；版本编号、版本创建时间等发布元数据不在草稿预览中生成。发布时重新校验计算，Shapefile 合并、拆分后可能与预览要素划分不同。
6. “导出”页只导出当前影像的 Raster / Shapefile / DXF 文件，不会发布项目版本。
7. 在项目页点击“发布项目版本”：先保存当前编辑，读取项目全部最新草稿，显示纳入景数和未生成成果的景数；人工确认后一次生成不可变完整项目版本。曾经发布与尚未发布的影像成果均纳入，每景一层；没有草稿的影像不纳入。无需逐景导出才能发布。

外部用户下载现有 GeoPackage 使用 download API，不走插件导出发布按钮，不产生新版本。

持久化保存的是六个固定字段与最终几何，包括 origin / run_id。AI 暂态预览、负点、拒绝区和撤销栈不保存；恢复后不承诺跨会话撤销上一次融合。崩溃或强制结束进程只保证恢复最近一次成功保存的草稿，长时间编辑请主动保存。服务不可用时可从草稿图层另存本地副本。

## 发布与文件一致性

- `lcc_drafts` 保存不可变草稿快照，`lcc_images.draft_id` 是当前编辑快照；临时 generation 不是公开版本。
- 发布引用明确的全部 `(image_id, draft_id)` 列表，同时携带 base_version 和 UUID 幂等标识。
- 服务对项目行加锁；只有基础版本仍等于当前版本才发布。幂等命中先于基础版本检查，相同请求重试返回原版本；相同标识不同请求返回 409。
- 在项目锁内核对请求中的全部快照与当前已保存草稿完全一致；缺景、过期或跨项目草稿均拒绝。不依赖逐景历史发布，第一次即可包含多景。空草稿可以表示确认没有目标要素。登记、保存和发布共用项目锁，避免确认后出现漏景。每景单独图层，不进行跨景去重。
- 在新版本独立目录准备 GeoPackage，逐景验证输入摘要、逐要素写文件与 PostGIS。文件关闭、fsync、原子改名并生成发布 audit.json 后才提交版本和当前指针。
- 文件写入或事务失败会回滚数据库可见状态。失败目录可能保留，提交响应丢失时不删除文件，因为提交结果可能已成功；管理员只能核对数据库后清理确认为未引用的文件，禁止按文件年龄直接删除已发布版本。
- 本地 QSettings 保存待确认发布请求，断线重试复用同一幂等标识。409 冲突时再次点击发布会自动读取最新项目并重新确认；网络超时保持请求，需重试确认旧请求结果。旧请求成功后若草稿又有修改，界面提示再次点击发布以生成新版本。
- 保存草稿也检查 base_draft_id；其他客户端已保存不同内容时返回 409。同内容重试不创建额外草稿。
- 上传是内部草稿传输，绝不提供外部影像上传或输入登记接口。服务登记 QGIS 选择的实际影像/DEM 路径，不按目录筛选。
- 发布审计记录项目、基础版本、影像快照、请求摘要和成果 SHA-256；现有推理审计仍按本地导出流程旁存。

## 属性与几何约定

标准成果保留最终人工确认的 Polygon / MultiPolygon、孔洞和源 CRS；图层名为 `image_<影像 UUID 去横线>`。API 输出统一 EPSG:4326，多面几何不拆散。

属性为 `feature_id`、`class_id`、`class_name`、`source_id`、`project_id`、`image_id`、`version_id`、`run_id`、`created_at`、`perimeter_m`、`center_lon`、`center_lat`，再追加下文 17 个标准字段，共 29 个业务属性。feature_id 源于草稿 UUID；同一特征跨影像以 image_id + feature_id 区分。created_at 是该项目版本成果记录的创建时间。人工特征 run_id 可为空。标准成果不暴露 origin，也不暴露任何输入/存储路径。

面积与周长按 WGS84 椭球计算；面积减去孔洞，周长包含外边界与孔洞边界，多面取总和。中心点定义为源 CRS 几何质心转换到 EPSG:4326，因此可位于多面的空隙内，不宣称一定在面内。无效、空的单个要素、三维几何、超出有效经纬范围或横跨日期变更线的几何明确拒绝，不静默修复。空草稿允许保存和发布，用于人工确认某景没有滑坡。

## API 协议

仅外部查询请求携带 `X-API-Key`，内部项目管理请求不需要此头。UUID 路径/参数不合法返回 422；未认证 401；不存在或跨项目版本 404；并发/幂等冲突 409；草稿过大 413；文件不可用 503；非业务异常只返回通用 500，完整错误留在服务日志。

| 外部只读接口 | 行为 |
| --- | --- |
| GET /projects | 只列已有发布版本的项目，包含 current_version、published_at 和 SHA-256 摘要 |
| GET /projects/{project_id} | 当前或指定 version_id 的摘要与实际影像列表、要素数 |
| GET /projects/{project_id}/versions | 历史版本列表，is_current 标明当前版本 |
| GET /projects/{project_id}/features | 分页 GeoJSON，version_id、offset=0、limit=100（最大 1000） |
| GET /projects/{project_id}/download | 当前或指定 version_id 的完整 GeoPackage；X-Version-ID 与 ETag 标识实际内容 |

分页首个响应含 version_id、next_offset。后续必须携带首个响应的 version_id；offset > 0 而没有 version_id 会被拒绝。数据库以 image_id / feature_id 稳定排序。查询版本不存在时不回退当前版本。首期不支持 bbox、裁剪、单景下载和区域查询。

内部应用仅包含：

| 接口 | 请求 |
| --- | --- |
| GET /internal/projects | 包含未发布项目 |
| POST /internal/projects | JSON：name |
| GET /internal/projects/{id}/images | 内部影像、DEM 引用和当前 draft_id |
| POST /internal/projects/{id}/images | JSON：name、image_path、dem_path（可空） |
| PUT /internal/projects/{id}/images/{image_id}/draft | GeoPackage 原始请求体，可选 base_draft_id 查询参数 |
| GET /internal/projects/{id}/images/{image_id}/draft | 当前保存草稿，X-Draft-ID 返回实际快照 |
| POST /internal/projects/{id}/publish | JSON：drafts=[{image_id,draft_id}, ...]、base_version（首版为 null）、idempotency_key、confirmed=true |

这两个应用互不挂载；公共应用没有内部写入路由。内部应用免 Key，依靠独立监听地址和网络访问范围隔离。文档/OpenAPI 入口默认关闭。

## 测试与未完成的环境验收

```powershell
.\.venv-backend\Scripts\python.exe -m pip install -r backend\requirements-test.txt
.\.venv-backend\Scripts\python.exe -m pytest backend\tests -q
```

真实事务验证需要单独测试数据库，通过 `LCC_TEST_DATABASE_URL` 注入（测试命令也可先 `load_dotenv(".env")` 加载本机配置）。测试会创建随机 `lcc_test_<UUID>` schema 并在结束后删除，仅在明确的测试数据库运行；账号需要建 schema 权限，PostGIS 扩展可由管理员提前准备。

QGIS 3.44 的配置好环境的 Python 中运行：

```text
python -m unittest land_cover_classification.test.test_project_workflow land_cover_classification.test.test_draft_generation_publish land_cover_classification.test.test_inference_controller
```

本次验证情况见 [project_service_validation.md](project_service_validation.md)。无数据库时不得把被跳过的集成测试当作事务或并发验收通过。部署后还需完成两台机器共享路径访问、QGIS 实际关闭重开、内外网隔离、数据库角色权限、下载及备份恢复验收。


## 标准成果属性（2026-09）

来源为《遥感地质解译属性信息》中的“表 A.0.1-2 遥感解译图表属性信息（ygjytb）”和“1 滑坡错落（bldz_hpcl）”。原表只有 tid 必填；两个 tid 合并，两个 bz 按统一备注合并。说明字段正确拼写为 **ygtsm**，禁止使用 ygtm。来源表标识不代表新增数据库业务表。

固定追加顺序及存储映射如下，应用层空值为 None，GeoPackage 为 NULL，API 显式输出 null；Shapefile 使用对应类型空值，UTF-8 编码并生成 .cpg。

| 字段 | 中文名称 | GeoPackage/API 类型 | Shapefile 存储 | 本期赋值 |
| --- | --- | --- | --- | --- |
| tid | 遥感解译图编号 | TEXT/string | String(32) | 影像 UUID 的 32 位小写 hex |
| ygtsm | 遥感解译图说明 | TEXT/string | String(254) | 空 |
| blc | 比例尺分母 | INTEGER/integer | Integer | 空 |
| ztz | 制图者 | TEXT/string | String(128) | 项目创建时选填 |
| fhz | 复核者 | TEXT/string | String(128) | 空 |
| shz | 审核者 | TEXT/string | String(128) | 空 |
| sdz | 审定者 | TEXT/string | String(128) | 空 |
| rq | 制图日期 | DATE/date string | Date | 空 |
| bz | 统一备注 | TEXT/string | String(254) | 空 |
| lx | 类型 | TEXT/string | String(128) | 空 |
| hdfx | 滑动方向（方位角） | TEXT/string | String(128) | 空 |
| cd | 长度（m） | REAL/number | Double(10,2) | 空 |
| pjkd | 平均宽度（m） | REAL/number | Double(10,2) | 空 |
| mj | 面积（m²） | REAL/number | Double(24,3) | 最终几何 WGS84 椭球面积 |
| wzcf | 物质成分 | TEXT/string | String(128) | 空 |
| yfys | 诱发因素 | TEXT/string | String(128) | 空 |
| jytz | 解译特征描述 | TEXT/string | String(254) | 空 |

长度为 32 的标准字符字段逻辑上限为 32 个 Unicode 字符；Shapefile 使用 128 字节容纳 UTF-8，tid 使用 32 字节。原表浮点长度 10 未规定小数位；cd/pjkd 使用两位小数，mj 为支持大面积与小斑块适配为 Double(24,3)，这是明确的存储扩展。GeoPackage/API 保留双精度，不人为舍入。Shapefile 遇到三位小数会变为零或超过 24 位宽度的面积明确报错，不静默截断。

面积公开键由 area_m2 统一为 mj，不输出双字段；min_area_m2 等后处理配置不受影响。class_name 保持 landslide，lx 为空；周长 perimeter_m 不等于 cd，created_at 不等于 rq，均不自动映射。tid 不替代 image_id、feature_id 或 source_id，同景跨导出和版本稳定，不同景独立。非空要素的 tid、mj 必有值，mj 必须为有限正数；其余标准字段仅 ztz 可在创建时填写。空成果仍输出完整 schema。

Shapefile 保留 class_id、class_name、source_id 后追加 17 字段，共 20 字段；在 dissolve 和 multipart-to-singleparts 后逐面以 QgsDistanceArea、源 CRS、项目 transformContext 和 WGS84 椭球计算平方米面积。无效影像 ID、项目/草稿关联、制图者或面积在覆盖已有成果前失败。GeoPackage/API 保留原 13 项中的 12 项并追加标准字段，共 29 项；服务端保留孔洞扣除、多面求和、周长和源 CRS 质心转换算法。草稿内部六字段及几何融合规则不变，GeoTIFF/DXF 不扩展。


## 创建项目与退出保存

POST /internal/projects 接受 {"name":"示例项目","ztz":"张三"}。ztz 可省略、null 或空白，首尾空白去除后空值存 NULL，超过 32 个字符返回 422。lcc_projects.ztz 为可空 varchar(32)，非空值不得全为空白；内部创建响应、项目列表携带 ztz，外部项目列表/详情保持原结构。影像登记请求不接受 ztz。创建表单取消不创建项目、不继承上次制图者；本期没有创建后修改入口。

关闭插件、停靠窗口、Escape、卸载及退出 QGIS 均尝试自动保存。退出 HTTP 使用 QtNetwork QNetworkAccessManager/QNetworkReply 和 QFile 流式上传快照；单个精确 QTimer 限定 5 秒总期限，abort 后结束局部事件循环，无重试、不接受重定向。普通保存仍为 120 秒请求策略。忙碌时跳过，后台请求独立持有线程和临时文件直至完成；不使用 QThread.terminate 或无限 wait。

主窗口事件过滤器始终放行 Close，不修改 QGIS 自身的保存决定。同一关闭流程只保存一次，取消宿主退出或重新打开插件后允许再次尝试。手动保存、切换影像/项目及发布前保存失败仍保护会话；退出失败只写日志及存活消息栏，不弹阻塞确认、不谎报成功。失败/跳过解除草稿图层引用但保留 generation 目录并记录路径供人工恢复；未提交编辑缓冲不保证在磁盘中，提交失败日志明确提示风险。未关联草稿确认不能阻止宿主退出。

## 未交付测试数据的一次性重置

本期不迁移旧属性。管理员先停止服务写入并结束旧编辑会话，记录数据库、存储根目录、项目 ID、六表计数及所有关联路径，不输出凭据。在事务中锁定并显式清空 lcc_features、lcc_version_images、lcc_versions、lcc_drafts、lcc_images、lcc_projects，不使用 CASCADE；空表增加 ztz 及约束，新环境使用 schema.sql。

事务成功后仅清理已核实的草稿、版本 GeoPackage 和 audit.json。解析绝对路径，验证位于预期 drafts/versions 子目录，拒绝链接及重解析点；不递归清空存储根目录，不删除未知文件。清除已删除项目对应的 LandCoverClassification/pending_project_publish/<project_id> 设置。输入影像、DEM、模型、runtime、配置、用户另行导出和其他数据库对象保留。清理不纳入初始化、启动或项目创建逻辑。实际执行范围见验证记录。
