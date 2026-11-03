# 常用命令

- Windows 创建统一 runtime：`land_cover_classification\vendor\sam_runtime\create_sam_venv.bat`；Linux/macOS：`bash land_cover_classification/vendor/sam_runtime/create_sam_venv.sh`。已有环境仅验证，失败不自动修复。
- 完整功能检查：用统一 venv Python 执行 `land_cover_classification/vendor/sam_runtime/runtime_setup.py --functional-check`；安装入口还会执行 `pip check`。
- 本地代码视图同步：`codegraph.cmd sync .`，随后 `codegraph.cmd status .`；符号查询示例：`codegraph.cmd query RuntimeInstallDialog -p .`。
- 验证 PyTorch runtime：`.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\pytorch_deps_check.py --json`。
- 验证 SAM runtime：`.\land_cover_classification\vendor\sam_runtime\venv\Scripts\python.exe land_cover_classification\sam_deps_check.py --backend sam2`。
- 运行独立规则测试：`python land_cover_classification\test\test_rules_synthetic.py`；输入适配器测试：`python land_cover_classification\test\test_pytorch_input_adapter.py`。这些测试可能要求当前 Python 具备 NumPy 等依赖。
- Python 改动的最低编译检查：`python -m py_compile land_cover_classification\pytorch_deps_check.py land_cover_classification\pytorch_inference_core.py land_cover_classification\pytorch_inference_runner.py land_cover_classification\model_scan.py land_cover_classification\land_cover_classification_dialog.py`。
- `land_cover_classification\Makefile` 含历史 `make test`、`make package VERSION=<tag>` 等目标；其测试依赖 QGIS/nosetests，Windows 本地开发通常优先直接运行目标脚本或在 QGIS 中验证。
- Windows 下优先用 `rg --files` 查文件、`rg -n <pattern>` 查文本、`Get-Content -Raw <path>` 读文本；注意部分中文文件通过 PowerShell 可能显示乱码。
- 后端日常统一启动：Windows 根目录 `start_backend.bat`；Linux/macOS `bash start_backend.sh`。复用独立 `.venv-backend` 和 `.env`，同一终端管理两个独立服务，Ctrl+C 联动退出；不安装依赖或初始化数据库。
- 统一后端启动器默认监听 `0.0.0.0:8091` / `0.0.0.0:8092`，客户端使用服务器实际 IP；内部端口由防火墙限制在可信内网。

- QGIS Python 中运行 UI/历史/恢复回归：`python -m unittest land_cover_classification.test.test_workflow_ui land_cover_classification.test.test_independent_workspaces land_cover_classification.test.test_project_workflow`。解释器及旧 AI 测试 `utilities` 导入说明见 `docs/development.md`；不依赖 `.tmp/` 验收脚本。
- 后端定向回归：`.\.venv-backend\Scripts\python.exe -m pytest backend/tests/test_history.py backend/tests/test_api.py backend/tests/test_postgis.py -q`；全量使用 `backend/tests`。真实数据库测试须显式设置指向独立测试库的 `LCC_TEST_DATABASE_URL`，不记录凭据，不自动沿用生产连接；skip 不是通过。
- UI/历史符号索引核对：`codegraph.cmd query AttributeBrowser -p .`、`codegraph.cmd query restore_source -p .`、`codegraph.cmd query compare_versions -p .`。
