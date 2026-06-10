# SAM1 模型权重目录

本目录保留给 SAM1 ViT-B 回退后端使用。默认 AI 编辑后端已经切换为 SAM2.1；只有显式使用
`sam1` 后端时才会读取这里的权重。

请将 SAM1 ViT-B 模型权重放在以下默认路径:

```text
land_cover_classification/models/sam/sam_vit_b_01ec64.pth
```

默认文件说明:

- 名称: SAM1 ViT-B
- 文件: `sam_vit_b_01ec64.pth`
- 用途: AI 辅助编辑功能的 SAM1 回退后端
- 分发方式: 仓库不直接附带权重文件，需要用户或部署方自行准备

如果要切换为其他 SAM1 规格，例如 ViT-L 或 ViT-H，需要同步修改
`sam_deps_check.py` 中的默认模型路径和 `model_type`，并在启动 worker 时传入匹配的
`model_type`。

注意:

- 插件不会自动联网下载权重文件。
- SAM1 依赖 `segment-anything`，默认 SAM2 环境不一定安装该回退依赖。
- 仅替换 `.pth` 文件而不修改 `model_type` 时，建议继续使用 ViT-B 兼容权重。
