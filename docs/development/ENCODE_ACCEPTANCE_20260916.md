# SDR / HDR / 插帧编码验收补测 · 2026-09-16

状态：**主代理已核对原始报告并填写判定**。三类均覆盖；本轮不是生产接入或打包授权。

## 固定验收门槛

1. **传递正确性**：候选和对照使用同一输入、参数、模型、帧序/reset；原始输出逐帧哈希一致。
   baseline 自身重复性失败时不能判候选合格。音视频/容器校验单列，不能用图像哈希替代。
2. **正式编码契约**：帧率、codec、preset、tune、rate control、混合、RGB/YUV转换、range、
   transfer、primaries、位深、帧序、音频和收尾行为需与正式路径相符。实验低延迟 CONSTQP
   不能冒充正式 HQ CQ/VBR 对照。合同未实现记为 unsupported，而非 test pass。
3. **HDR**：必须保留 10-bit/PQ 或 HLG，不以8-bit SDR转换换速度。解码后的HDR帧、光流分析代理
   和最终导出像素是不同对象。真实素材与合成fixture分别标注。
4. **插帧**：区分生成帧传递、正常原帧+生成帧时间线、HDR和倍率。零光流探针不作为真实光流验收。
5. **生命周期**：报告连续帧数、超时/取消/再启动测试覆盖。短片不等于长视频压力、多卡故障恢复。

## 范围与预算

- 输入目录 `F:/project/test/dlss5/测试素材` 只读。ffprobe盘点8个视频，均8-bit SDR；没有已标记PQ/HLG素材。
- HDR使用已有 `tmp/hdr-e2e/source-hdr10.mp4` 与 `source-hlg.mp4` 合成fixtures。
  用户原生HDR内容的验收缺口必须保留，不将SDR文件改标签后当作HDR实测。
- 每组初测＋至多一轮局部修正，避免重复发散；没有接入大接口的项如实列出。
- F盘起始可用95.11 GiB、tmp约28.7079 GiB。新增磁盘预算256 MiB，三组各64 MiB。
- 所有新产物 `tmp/encode-acceptance-20260916/`；复用CUDA环境、模型和只读运行库。
- 原有应用/GUI/诊断/预览的未提交修改不属于本轮，不覆盖。结果以报告记录的源码状态为准。
- 收尾时任务目录约 4.16 MiB，F 盘仍约 95.10 GiB 可用。未改默认设置、正式 DLL 或发行基线。

## 分组记录

- [SDR](encode-acceptance-20260916/SDR.md) — CPU 生产流式编码通过，码流可重复
- [HDR](encode-acceptance-20260916/HDR.md) — 分析图与生产逐位相同；混合半精度误差，拒绝晋升；GPU 导出 unsupported
- [插帧](encode-acceptance-20260916/FG.md) — 真实 NVOFA、SDR 2× 传输通过（修正 pitch/resident 标志后）
- [生产 NVENC 合同](encode-acceptance-20260916/CONTRACT.md) — 单缓冲原生 HQ VBR/CQ 与 FFmpeg 生产 NVENC 解码 MD5 不一致，拒绝晋升
- [HQ 环缓冲](encode-acceptance-20260916/RING.md) — 对齐 P5 HQ 的 B 帧与系统内存 YV12 后仍与生产解码不一致，拒绝晋升

归档 JSON（与 tmp 源 SHA-256 一致后复制）：
[sdr](encode-acceptance-20260916/sdr-acceptance.json)、
[hdr](encode-acceptance-20260916/hdr-acceptance.json)、
[fg](encode-acceptance-20260916/fg-sdr2x-nvofa.json)、
[contract](encode-acceptance-20260916/nvenc-contract.json)、
[ring](encode-acceptance-20260916/ring-hq.json)。

## 最终判定

| 项 | 判定 | 说明 |
| --- | --- | --- |
| SDR 生产 CPU 编码 | 通过（仅该合同） | 180 帧 360×640，取消清理、帧数、源像素与码流可重复 |
| SDR 生产 GPU / 直连 | **reject / unsupported** | 实验 CONSTQP 传输≠`h264_nvenc p5 hq vbr cq19`；原生 HQ 单缓冲解码画面与 FFmpeg 不一致 |
| HDR 分析候选（CPU） | 分析通过、混合拒绝 | 与 `analysis_rgba8` 逐位相同；`compose_hdr` mix=0.7 最大差 1–2 ulp |
| HDR 导出 / Main10 NVENC | **unsupported** | 无原生 HDR 素材；未跑 decode→DLSS→Main10 |
| 插帧生成帧 GPU 传递 | 通过（实验编码器） | 1440²、NVOFA grid=1、8 帧 2×；画面与码流一致 |
| 插帧生产 HQ/HDR/3×4×/音频 | **unsupported** | 超出本轮 candidate |

**不把任何一项读成生产默认接入。** 9-15 研究里 DLSS/DLSSG→NVENC 的搬运可行性仍然成立；
本轮补上的编码合同证明：要落地必须先做输出环缓冲和与 FFmpeg 相同的 YUV/RC，而不是打开实验编码器。

继续遵守 [GPU 常驻研究](GPU_PIPELINE_STUDY_20260915.md) 的边界：不改默认、保留 CPU 完成等待、
不默认替换 NVOFA、不自动荐参。
