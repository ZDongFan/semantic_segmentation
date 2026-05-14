# Vendored PaddleRS

`PaddleRS/` 子目录是 [PaddleCV-SIG/PaddleRS](https://github.com/PaddleCV-SIG/PaddleRS)
项目的 vendor 副本。

本目录只保留运行时所需的 `paddlers/` Python 包,`docs/`、`examples/`、
`tests/`、`setup.py` 等开发构建产物未被包含,以减小插件体积。

上游的 Apache-2.0 LICENSE 保留在 `PaddleRS/LICENSE`。

插件的 `__init__.py` 会把 `vendor/PaddleRS/` 插入到 `sys.path` 最前面,
从而保证插件内 `import paddlers` 始终命中本副本。
