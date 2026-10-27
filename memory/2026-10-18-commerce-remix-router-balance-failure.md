# 电商复刻失败：提示词服务余额不足（2026-10-18）

## 现象
Hisense 商户的 3 条 720p、15 秒电商复刻任务失败，界面错误写“DeepSeek 分镜洗稿失败”。

## 已核实根因
线上 `equai-api-local` 的提示词设置为 enabled，base URL 是 `https://api.router.one/v1`；运行环境 `H3_PROMPT_MODEL=grok-4.7`。因此这次实际请求走 Router One 的 Grok 4.7。`remix_vision._wash_shots_with_deepseek` 和 `reenact.llm.director` 的错误文案仍硬编码称为 DeepSeek，容易误导。

Router One 返回 HTTP 402 `INSUFFICIENT_BALANCE`，明确写明 available `$0.0000`，且需要至少约 `$0.001647–$0.001955` 的初始预占。失败发生在 `remix_vision.analyse()` 的分镜洗稿调用。视频理解阶段已走到洗稿阶段；洗稿调用失败后流程抛出 `VisionError`，停止提交后续生成。因此这些任务没有进入 GPU 视频生成。

## 受影响任务
- `vid_3ca159af3fba4d0db43900fb77def5b2`：17:35:21 创建，17:35:58 失败；402 minimum reservation `$0.001955`。
- `vid_d1f954976094449ea2a81289f9d83fa7`：17:35:39 创建，17:36:12 失败；402 minimum reservation `$0.001755`。
- `vid_edb1966b9ba742bd8b80f25cd72abcb6`：17:38:56 创建，17:39:24 失败；402 minimum reservation `$0.001647`。

三条记录均 `provider_started=0`、`provider_billing=0`、`billing_status=released`；每条任务 1.5 积分均已释放，未扣费。日志时间为 UTC，任务表为 Asia/Shanghai 时间。

## 没有修改
仅查询线上日志、只读读取任务数据库、查询当前脱敏后的提示词服务设置和本地代码路径；未更改代码、服务配置或余额。
