# 插帧初测（2026-09-16）

## 范围

`scripts/encode_acceptance_fg.py` 是独立验收 harness：复用 `gpu_dlssg_probe.py` 的
shared-D3D12→CUDA/NVENC 传输与句柄/LUID/fence 协议，输入使用真实解码帧，并沿用
`frame_generation.export_video` 的 NVOFA grid=1、边界 padding、reset/cut 语义。
它同时记录原帧、生成帧和末帧 hold 的完整时间线，比较 CPU-bounce 与 resident 两路径的
原始 hash、有效性、时间线及码流一致性。每帧有超时，输出仅为小 JSON/log。

## 准确边界

- 仅实验 candidate；不修改默认设置、正式 worker/runtime 或生产协议。
- 目标为 SDR、2×、1440×1440 以内、最多 16 帧；最多两轮交叉 A/B。
- HDR：当前 inventory 没有原生 HDR 短片；本 harness 不将 uint8 SDR 当 HDR，故本轮
  unsupported。3×/4×：本轮 candidate 仅 SDR 2×，unsupported。
- 输出编码沿用 probe 的实验 H.264 策略；不是生产质量/音频/容器验收，也不宣称完整
  export_video 的 NVENC HQ、音频、长片或多 GPU 支持。
- 真实运动向量只验证 NVOFA→DLSSG 输入契约与两传输路径一致性，不构成插帧画质认证。

## 复现

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-acceptance-20260916\fg'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
tmp\guidance-cuda-env\Scripts\python.exe -B scripts\encode_acceptance_fg.py `
  --work tmp\encode-acceptance-20260916\fg --label fg-sdr2x-motion `
  --source 'F:\project\test\dlss5\测试素材\9月1日.mp4' --frames 8 --rounds 1
```
