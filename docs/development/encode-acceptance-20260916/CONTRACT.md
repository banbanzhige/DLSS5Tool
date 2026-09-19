# 生产 NVENC 合同对照（2026-09-16）

比较应用正式 `FFmpegVideoWriter` GPU 路径与单缓冲原生 HQ VBR/CQ 探针。
不接入 DLSS、音频、HDR、B 帧或 lookahead，不改默认设置。

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-acceptance-20260916\contract'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
tmp\guidance-cuda-env\Scripts\python.exe -B scripts\encode_acceptance_nvenc_contract.py `
  --work tmp\encode-acceptance-20260916\contract `
  --source 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --frames 24
```

24 帧缩放到 360×640、60 fps。生产 GPU 参数为
`h264_nvenc -preset p5 -tune hq -rc vbr -cq 19 -b:v 0`。
原生探针为同一 CQ 目标，但强制 `frameIntervalP=1`、关闭 lookahead，因为只有一个
已注册 CUDA 指针。

## 结果

状态：`measurements_completed`。`promotion_gate`: **reject**。

| 路径 | 编码器 | 24 帧解码 | 耗时 | 与生产 NVENC 逐帧 MD5 |
| --- | --- | --- | --- | --- |
| FFmpeg 生产 GPU | H.264 NVENC | 通过 | 0.498 s | 对照 |
| FFmpeg 生产 CPU | libx264 | 通过 | 0.192 s | 不同（预期） |
| 原生 HQ NV12（FFmpeg `format=nv12` 后再编码） | 单缓冲 NVENC | 通过 | 0.074 s | 不一致 |
| 原生 HQ ABGR（BGRA 内存） | 单缓冲 NVENC | 通过 | — | 不一致；也与 NV12 路径不一致 |

FFmpeg 确实走了 GPU。原生 EOS+LockBitstream 在此驱动上会硬崩溃，flush 改为空操作；
无 B 帧时每帧已经出包。

## 判断

单缓冲原生 HQ VBR/CQ **不能**替代生产 FFmpeg NVENC。即便 YUV 已由 FFmpeg 转换，
解码画面仍不同，说明缺口不只是 RGB→YUV，还包括 FFmpeg 封装的 B 帧、lookahead
和其他 RC 细节。ABGR 走 NVENC 自带颜色转换，与 `format=yuv420p` 也不同。

要落地生产直连，需要：输出环缓冲以匹配 HQ B 帧/lookahead；GPU 上复现 FFmpeg 的
YUV 转换；HDR 另做 Main10/P010；预览/缓存仍要 CPU 帧；保留 CPU 回退且默认关闭。
当前 CONSTQP 传输实验仍然只证明搬运，不证明画质合同。
