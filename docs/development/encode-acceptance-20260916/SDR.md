# SDR 编码验收（2026-09-16）

本报告只覆盖真实 SDR 素材的 CPU 生产编码流式路径；未使用 GPU，不能替代
NVENC 共享显存验收。输入为 `F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4`
（1080×1920、60 fps、只读），逐帧缩放到 360×640 后写入 `FFmpegVideoWriter`，不复制输入。

执行命令（TEMP/TMP 指向任务目录，Python 使用 `-B`）：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-acceptance-20260916\sdr'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/encode_acceptance_sdr.py `
  --work tmp/encode-acceptance-20260916/sdr `
  --source 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --frames 180
```

脚本先取消一组短编码并检查临时文件/目标文件清理，再完整编码两次；报告记录源像素
哈希、压缩码流 SHA-256、重解码帧数及首尾帧 MD5。输出 JSON 为任务目录中的
`sdr-acceptance.json`，两个小型 MP4 仅作可复现实证。

生产 SDR 参数由现有实现生成：`libx264 -preset fast -crf 18`，随后 `yuv420p`。
此前 NVENC probe 的 `H.264 P5 low-latency CONSTQP19`、ABGR CUDA 指针输入是传输实验，
与生产的 RGB/BGR→YUV、CQ/VBR、音频封装和 GUI 时间线合同不同，明确标记为 unsupported，
不得据此宣称正式 HQ CQ/VBR 等价或生产加速。
