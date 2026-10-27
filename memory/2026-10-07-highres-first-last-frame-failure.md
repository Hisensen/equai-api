# 1080p / 2K 首尾帧任务失败：根因与修复

## 根因

原生产 1080p / 2K r50 镜像内已经有 `runtime/custom_nodes/ComfyUI-YCNodes-MiniMax-H3/py/h3_tiled_sampler.py`，并定义 `H3TiledSampler`。标准 Worker 启动脚本此前会从腾讯 API 服务器下载 `/data/runtime/h3-long-worker.tar.gz` 并覆盖镜像文件；覆盖包中的精简 `__init__.py` 只导入 `h3_sigma_refiner`，屏蔽了镜像内原有的 `H3TiledSampler` 自动注册，因此 ComfyUI 报 `missing_node_type: H3TiledSampler`。

## 修复

2026-10-07 基于现有 1080p、2K r50 镜像分别构建了 baked-runtime 派生镜像，把 Worker 运行代码、自定义节点注册入口及所需依赖文件直接烘焙进镜像。1080p 与 2K 路由现已切到新镜像；`gongji.py` 对带 `-baked-runtime-` 标记的镜像不再附加腾讯运行包 URL 或下载启动脚本。720p 路由保持旧值，不在本次变更范围内。

API 更新由 `equai-api:highres-baked-runtime-20261007-v1` 提供，基于线上数字人 API 镜像派生，只替换 `/app/gongji.py`。旧 API 容器保留为 `equai-api-local-rollback-20261007`，健康检查通过。音乐 API 与上传中继均未重启。

镜像源码构建输入保存在 `worker_long/baked_runtime/`，构建文件为 `worker_long/Dockerfile.baked-runtime`。API 路由补丁构建使用 `Dockerfile.highres-api-patch`。

## 验证与限制

- 1080p GPU Worker 冒烟任务 `3387208` 成功；ComfyUI 启动后 `H3TiledSampler` 注册正常。
- 2K GPU Worker 冒烟任务 `3387203` 成功；ComfyUI 启动后 `H3TiledSampler` 注册正常。
- API 线上 `/health` 返回 ready，线上 1080p/2K 配置均指向 baked-runtime 标签，模块检查确认运行时跳过标记生效。
- 本地回归测试：`PYTHONPATH=. pytest -q tests/test_standard_overlay_patch.py tests/test_gongji_status.py tests/test_scheduler_routing.py tests/test_reference_sampling.py`，31 passed。
- 上述 GPU 冒烟验证的是镜像启动和节点注册，不是完整视频生成；尚未提交 1080p、2K 真实出片任务。OOM、PodInitializing 或音频边界问题需按各自任务日志单独诊断。
- 腾讯 API 上的旧运行包仍保留，供尚未迁移的 720p/旧任务使用；本次 1080p、2K 新任务不会再从腾讯云下载该包。720p 的迁移需单独验证其不同节点集后再处理。
