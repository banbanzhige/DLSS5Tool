# SDR 编码验收（2026-09-16）

本报告只覆盖真实 SDR 素材的 CPU 生产编码流式路径；未使用 GPU，不能替代
NVENC 共享显存验收。输入为 `F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4`
（1080×1920、60 fps、只读），逐帧缩放到 360×640 后写入 `FFmpegVideoWriter`，不复制输入。

执行命令（TEMP/TMP 指向任务目录，Python 使用 `-B`）：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-acceptance-20260916\sdr'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
.venv\Scripts\python.exe -B scripts\encode_acceptance_sdr.py `
  --work tmp\encode-acceptance-20260916\sdr `
  --source 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --frames 180
```

脚本先取消一组短编码并检查临时文件/目标文件清理，再完整编码两次；报告记录源像素
哈希、压缩码流 SHA-256、重解码帧数及首尾帧 MD5。输出 JSON 为任务目录中的
`sdr-acceptance.json`，两个小型 MP4 仅作可复现实证。

生产 SDR 参数由现有实现生成：`libx264 -preset fast -crf 18`，随后 `yuv420p`。
此前 NVENC probe 的 `H.264 P5 low-latency CONSTQP19`、ABGR CUDA 指针输入是传输实验，
与生产的 RGB/BGR→YUV、CQ/VBR、音频封装和 GUI 时间线合同不同，明确标记为 unsupported，
不得据此宣称正式 HQ CQ/VBR 等价或生产加速。

## 结果

状态：`measurements_completed`。源 SHA-256
`1f8f3f7a3dc64e815cda5e9f64783d8828b0e7d2085c74a7ababb8079158f364`。

| 门槛 | 结果 |
| --- | --- |
| 取消后无输出文件、无残留临时文件 | 通过（写 8 帧后 abort） |
| 两次完整编码均为 180 帧 | 通过 |
| 源像素 SHA 可重复 | 通过 |
| 压缩码流 SHA 可重复 | 通过 `6b087663bc211c568850f486f9e146c5ae7f8e67a5d378eff07270783e58d939` |
| 重解码首尾 MD5 | `1ea977adbca668ea3eb4e32c65171e0c` / `adb623a52911928e8133a451a7f506c7` |

两次编码约 0.85–0.95 s。这只证明生产 CPU 流式编码器在代理分辨率下可重复，
不构成 GPU 直连或 HQ NVENC 通过。生产 GPU 对照见合同组。
