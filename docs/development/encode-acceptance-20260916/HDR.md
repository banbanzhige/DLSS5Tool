# HDR 编码验收（2026-09-16）

素材盘点 8 个视频均为 8-bit SDR，没有原生 PQ/HLG 短片。本轮使用已有合成 fixture
`tmp/hdr-e2e/source-hdr10.mp4` 与 `source-hlg.mp4`，并明确标注 fixture，
不得当作用户相机/发行片源。

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-acceptance-20260916\hdr'
$env:TMP=$env:TEMP; $env:PYTHONDONTWRITEBYTECODE='1'
tmp\guidance-cuda-env\Scripts\python.exe -B scripts\encode_acceptance_hdr.py `
  --work tmp\encode-acceptance-20260916\hdr --frames 2
```

脚本将 10-bit HEVC 解码为 `rgba64le`（避免 8-bit 路径），再把隔离 Torch 候选
`analysis_hdr` / `compose_hdr` 对照生产函数 `analysis_rgba8` / `compose_hdr_frame`。
计算在 CPU 上完成，不占用 GPU 槽，也不调用 DLSS 或 NVENC。

## 结果

两个 fixture 的元数据均为 `hevc / yuv420p10le / bt2020 / tv`，传输函数分别为
`smpte2084` 与 `arib-std-b67`，无音轨。2 帧 `rgba64le` 字节数与期望一致。

| 项目 | PQ fixture | HLG fixture |
| --- | --- | --- |
| 分析 vs 生产 `analysis_rgba8` | 0 通道差异，SHA 相同 | 0 通道差异，SHA 相同 |
| mix=0.7 vs 生产 `compose_hdr_frame` | 最大绝对差 0.000244140625 | 最大绝对差 0.00048828125 |
| 候选设备 | cpu | cpu |
| `promotion_gate` | reject（混合） | reject（混合） |

分析图在 CPU 上与生产实现逐位相同；混合仍有半精度误差，与 9-15 研究对合成输入的拒绝门槛一致。
**不得把分析图通过读成 HDR 导出通过。**

## 仍为 unsupported

- 真实生产 `decode→DLSS→混合→HEVC Main10` GPU/NVENC 未执行。现有 8-bit ABGR H.264 probe 不是 HDR 合同。
- 用户原生 HDR 素材缺口仍在；合成 fixture 不能补这一项。
- 无音轨 fixture 不能证明音频时间线或 HDR 元数据封装。
