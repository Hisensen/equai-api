# 数字人 V2 原音频驱动修正（2026-10-02）

## 问题与证据

用户反馈 27.6 秒《童话》唱歌实验从首段开始口型不准。旧实验任务：`vid_05af2c2107c8418b86dea2d443c29648`，共绩效任务 `3327473`。生成成功只说明得到视频，不代表口型合格。

对照 https://github.com/slmonker/selflift-Avatar#audio-driven-h3-workflow：原音频需要经 H3 音频 VAE 编码、设置全零音频 noise mask，并进入 SelfLift 采样 latent。仅将音频作为 MiniMaxH3Easy 参考，再在输出阶段换回原声，不能证明采样时用原声驱动嘴部动作。

本项目之前缺少这条原音频 latent 及遮罩链路。上游 `nodes.py`、`avatar_sampling.py`、`avatar_masks.py`、`selflift.py` 与本地副本核对一致。

修正之前关于“每个分段都收到完整 27.6 秒”的判断：实际 `generate_long_avatar_chunk` 已传入 `lengths[0] / FPS`，不能将其认定为本次故障原因。

## 本次变更

只在 `selflift=True` 的数字人 V2 工作流中添加：

```text
当前段 LoadAudio → VAEEncodeAudio（H3 audio VAE）
                      ↓
SolidMask(0) → SetLatentNoiseMask
                      ↓
当前段 video latent → LTXVConcatAVLatent → SelfLiftAvatarH3Sampler
```

首段和续段的采样器都使用新 AV latent；续段仍从上一段读取 MotionContext 视频/音频上下文。数字人 V1 不增加此链路。

采样参数沿用现状：8 步 Euler，低分辨率前 6 步，空间缩放 0.5，rho=0.6，w_min=w_max=1，cfg=1，upscaler=none，空间 tiling 关闭，SolAttn tau=1.3。口型质量仍需观看实际结果；上游没有完美口型保证。

27.6 秒帧计划为 `[362, 328]`，24 fps，续段包含 22 帧上下文。对应原声窗口起点分别为 0 和 14.166667 秒。第二段先裁掉 18 帧，再在最终拼接中去掉前段的 4 帧，声音与画面时间线一致。

## 部署与验证

- 相关本地测试：32 项通过。覆盖 V2 首段/续段采样器实际连接的音频 latent、全零遮罩、H3 音频 VAE，以及 V1 继续使用原有 latent。
- 上游 mask 归一化代码检查：全一视频遮罩允许生成，全零音频遮罩完整保留；通过。
- 本次未运行上游完整 47 项 ComfyUI 测试（本机没有对应 ComfyUI 环境）。
- 腾讯云运行包：`/data/equai-local/runtime/h3-long-worker.tar.gz`。
- SHA256：`e26d4f8d52c151b88306e64b754827a23e737118c734e675395b6901bf0b08ba`。
- 备份：`h3-long-worker.pre-dh2-audio-mask-20261002-114400.tar.gz`。
- 使用线上真实接口重新提交同一人物图和 27.6 秒音频，额外提示词留空，画幅 9:16。
- 新任务：`vid_80a1ecbe79584214baf05ac46ac307d4`。
- 共绩效任务：`3327943`。
- 实验已完成：两段均成功，最终输出 27.625 秒（24 fps 帧量化），736×1280，H.264；第一音轨为兼容 AAC，第二音轨为原上传 MP3。
- 输出大小：14,301,535 字节。
- 排队约 32.1 秒，API 记录生成耗时 510.1 秒（8 分 30 秒）；共绩效实际计费运行 554 秒（含启动等）。
- 共绩效结束后的最终 GPU 费用：¥0.453060。线上任务表当时仍显示中途计费 ¥0.251700，本记录以共绩效最终费用为准，不将中途读数当最终成本。
- 商户成功扣分：2.000 pt（两段）。
- [本次视频](../output/digital-human2-song-27s-source-audio-20261002.mp4)；[上次视频对照](../output/digital-human2-song-27s-before-20261002.mp4)。
- [任务与费用记录](../output/digital-human2-song-27s-source-audio-20261002.json)。
- 生成链路成功不代表口型质量已验收；用户观看新结果进行效果评价。
