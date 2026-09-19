# HQ 环缓冲 NVENC 实验（2026-09-16）

本轮只做实验，不改应用默认、正式 DLL 或发行包。

对照 FFmpeg n7.1.1 `nvenc.c`：P5 + HQ 预设保留 GOP/B 帧，再叠加 VBR CQ19、initialQP=26。
P5 HQ 实测 `frameIntervalP=4`（3 个 B 帧）、`gopLength=250`、lookahead 关闭。按 FFmpeg 公式分配 16 个输入槽。

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\nvenc-hq-ring-20260916'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
tmp\guidance-cuda-env\Scripts\python.exe -B scripts\encode_acceptance_nvenc_ring.py `
  --work tmp\nvenc-hq-ring-20260916 `
  --source 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --frames 24
```

24 帧、360×640、60 fps。素材只读。

## 结果

状态：`measurements_completed`。`promotion_gate`: **reject**。

| 对照 | 24 帧解码 | 与生产 BGR 写入器 |
| --- | --- | --- |
| `FFmpegVideoWriter` GPU（生产） | 通过 | 对照 |
| FFmpeg `format=yuv420p` 后再 `h264_nvenc p5 hq vbr cq19` | 通过 | **逐帧 MD5 相同** |
| FFmpeg `format=nv12` 后再同一编码器 | 通过 | 不同 |
| 原生环缓冲，系统内存 YV12，输入为生产 yuv420p | 通过（24 包） | 24 帧 MD5 全不同 |

`format=nv12` 与 `format=yuv420p` 从 BGR 得到的 4:2:0 **平面也不相同**（24 帧全不一致）。
直连不能把 NVENC 的 ABGR/NV12 转换当成生产 `format=yuv420p`。

环缓冲本身可用：EOS 不再硬崩溃，B 帧槽位按 FFmpeg 公式工作。这只证明会话能跑完，不证明画质合同。

## 判断

1. 生产导出就是 BGR → `yuv420p` → FFmpeg NVENC；CLI 复现与写入器一致。
2. 单靠对齐预设 B 帧、CQ、YV12 系统内存，原生 NVENC 解码画面仍与 FFmpeg 不同。
   剩余差异在 FFmpeg 封装内部（global header、SEI、拷贝路径、multipass 等），本轮不再发散。
3. **不能**把原生 HQ 环缓冲接到默认导出。实验 CONSTQP 搬运结论不变。

未改默认设置。JSON：[ring-hq.json](ring-hq.json)，SHA-256
`65fac264457ee438005709cf11cd138efd4e90d6a44ba3b7144a4052b85739dc`。
