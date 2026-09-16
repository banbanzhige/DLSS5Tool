# SDR / HDR / 插帧编码验收补测 · 2026-09-16

状态：执行中。用户明确要求三类均覆盖，使用指定素材，低开销代理执行、主代理验收。
本轮不是生产接入或打包授权。三位 GPT-5.6 Luna 工作者各持有独立测试脚本，单卡串行实测。

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

## 分组记录

- [SDR](encode-acceptance-20260916/SDR.md)
- [HDR](encode-acceptance-20260916/HDR.md)
- [插帧](encode-acceptance-20260916/FG.md)

最终判定待主代理核对原始报告后填写。
