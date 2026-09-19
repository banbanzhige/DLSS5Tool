# GPU 编码与 HDR 优化定向复测 · 2026-09-16

## 最终建议

**本版可以保留/合入的新增性能改动：`compose_hdr_frame` 普通输出在 mix=0/1 时提前返回。**
这项改动已在用户工作区，复测没有再次修改生产函数。它不改模型、色彩计算或 GPU 历史，
只是绕过无用 FP16→FP32 整帧转换。已有正式 RAFT→DLSS 显存光流直连继续保留，不计为本轮新增收益。

**DLSS/DLSSG 显存传递仍有工程价值，但本轮不建议打开新的原生 NVENC 默认路径。**
现在阻碍是未完成的生产编码接口/颜色合同，而不是再多跑一遍短测就能消除的问题。
HDR GPU 算术、超分/深度 GPU 改造不跟随接入。没有打包、替换正式 DLL、改默认参数或提交推送。

## 复测范围与证据

本轮由主代理定向复测，不复用其他 AI 的“通过”标签。原报告不覆盖。
修正的实验 harness 为 `scripts/encode_acceptance_fg.py`；新增 `scripts/encode_recheck.py`，
只操作登记目录 `tmp/encode-recheck-20260916`。现有 native DLL/worker 只读复用，无重新构建。

所有报告、小型视频/码流、测试源码快照与 SHA256：
[证据目录](encode-recheck-20260916/README.md)、[逐文件清单](encode-recheck-20260916/MANIFEST.json)。

| 实测 | 对照结果 | 接入建议 |
| --- | --- | --- |
| HDR 端点快路径 | 168 组逐字节一致；1080p mix=0/1 约28ms→0.004ms | 可合入现有生产小改动 |
| BGRA/ARGB vs RGBA/ABGR | 修正后两者原始解码画面、码流相同 | 证明旧颜色对照有缺陷；不等于正式编码已对齐 |
| 原生单槽 HQ vs FFmpeg HQ | 修正后仍有差异，RGB 平均绝对差约1.898/255 | 暂不默认接入 |
| 原生 HQ 环缓冲 vs FFmpeg HQ | 24帧均能解码；平均绝对差约0.736/255 | 暂不默认接入；不得说“全不同就画质很差” |
| NVOFA＋SDR2×完整短片 | 16输入→32编码帧；两路径像素、码流、带音轨MP4一致 | 短片传递正确性通过，生产HQ/HDR/多倍未验收 |
| 现有PQ/HLG HDR导出 | 各24帧×2轮，DLSS→混合→Main10，重复结果一致 | 现有路径可继续使用，不是新显存直连通过 |
| GPU HDR分析/混合 | CUDA真机仍有数值差异 | 不接入默认引导/混合路径 |

## 1. HDR 端点快路径：通过

对照 Git HEAD `compose_hdr_frame` 与工作区版本：FP16/FP32、连续/非连续数组、view 0/1/2、
mix -1/0/.7/1/2/5/6、PQ/HLG，合计168组，输出shape/dtype相同且字节无差异。
完整相关测试另通过24项；端点变化对 view=1/2 不启用早返回，保留差异图/左右对比行为。

1920×1080 RGBA16F、预热、6轮交错CPU算子计时，中位：

- mix=0：旧28.77285ms，新0.00360ms。
- mix=1：旧28.30720ms，新0.00395ms。

计时只包含该函数，输入/输出已在CPU，快路径直接返回原有连续FP16数组，不是完整导出速度。
旧函数本来也在端点返回同一输入，不是新引入数组别名。两次FP32转换的临时数据量：
1080p约63.3MiB、4K约253.1MiB（算术估计，不是测得进程峰值）。其他混合比例不获得这项收益。

证据：[endpoint.json](encode-recheck-20260916/endpoint.json)。

## 2. 颜色与正式编码：纠正错误，但尚未等价

只读素材 `260429广寒宫98s.mp4`，前24帧、CPU缩小到360×640、60fps。
FFmpegVideoWriter强制GPU P5/HQ/VBR/CQ19两次，重解码RGB逐字节一致；同一I420输入再经
FFmpeg NVENC，也与生产BGR入口一致，提供了可靠基线。

### 原错误复现

BGRA内存对应NVENC的ARGB枚举（word-order，小端字节为B,G,R,A），不是ABGR。
同时运行正确 BGRA→ARGB、正确 RGBA→ABGR，以及错误 BGRA→ABGR 控制：

- 两条正确布局：压缩H264 SHA256均为
  `aa78ce215e4c203586c53de4d0430715c19e7ab6e82c48463689b363be2ca90a`，解码也完全相同。
- 错误布局 vs 正确：平均绝对差14.813/255，最大127，PSNR约21.69dB。
  这证实原测试有通道解释错误，不应把这一分支差异都归给NVENC色彩转换。

### 修正后的剩余差异

正确RGB原生单槽HQ vs FFmpeg生产：平均绝对差1.898/255，最大21，PSNR约40.09dB。
原生系统内存YV12环缓冲 vs FFmpeg生产：平均绝对差0.736/255，最大16，PSNR约46.03dB。
这些PSNR均在重解码RGB8之间计算，是两个有损结果的差异，不是相对无损源的画质评分。
不能据此断言原生质量更差，也不能仅凭全部帧MD5不同推导故障原因。

环缓冲保留P5 HQ的3个B帧，16槽，lookahead关闭；单槽强制无B帧。相关配置并非全面对齐。
本轮不声称定位了所有差异来源，也未修复环缓冲分配失败清理问题：源码只在所有槽创建完成后
设置`nslots`，中途失败会漏清理已分配槽。此项仍应在正式接入前修复并做失败注入检查。
没有压力或设备移除测试，不能用24帧成功来认证原生组件生命周期。

证据：[contract.json](encode-recheck-20260916/contract.json)，目录中保留各H264与生产MP4样本。

## 3. 插帧：完整输出正确，未证明端到端提速

修正原harness的三处口径：

1. 计时在NVOFA之前开始；单列flow耗时与审计耗时。
2. 实际编码原帧、生成帧、reset保持帧和末尾保持帧，不再只生成16项事件列表却编码7帧。
3. 真正重解码/检查32帧的PTS，并封装源音轨。候选共享缓冲的审计回读、哈希计时单独扣除。

样本：`9月1日.mp4`前16帧1440×1440/30fps，NVOFA grid=1；人为在0和8 reset，
不是自动切镜检测测试。2轮交错CPU-bounce/resident；实验NVENC仍为P5低延迟CONSTQP19。
编码帧率按2×设为60fps，不再使用旧探针固定30fps。

- 每条路径实际32包、32解码帧、16原帧＋14生成帧＋2保持帧。
- 每次原始全帧哈希、validity和编码码流与基线一致，非reset返回无效会立即失败。
- 两个MP4也字节相同，SHA256：
  `b4c599dd92bb6cc148329b8a552740d2e7db438537863d030ec2f0008d0e160d`。
- PTS为0到0.516667秒、间隔1/60；视频0.533333秒，AAC音轨0.533000秒。
  音轨转码/截取到该短片长度，证明此样本封装时间戳/时长，没有做主观听音或长片同步验收。

本次计时：CPU-bounce两轮92.79/105.43ms每输入帧；resident101.91/107.17ms。
平均99.11→104.54ms，**没有测得提速，不能引用旧34.94→31.34为整条流程收益**。
NVOFA平均耗时在44–57ms间波动；开始前nvidia-smi曾见外部GPU负载约24%。
本任务GPU顺序运行，但没有独占整张显卡。审计操作虽从耗时中扣除，仍会改变调度，
因此本组主要支持正确性，不用于宣称候选稳定更快或稳定更慢。

限制：只有0.53秒SDR2×短片、不是生产HQ编码、HDR插帧/3×4×/GUI缓存/长片恢复仍未覆盖。
共享显存只能省去生成画面的回读上传；当前完整harness仍从CPU上传原帧，不能称整个导出零拷贝。

证据：[fg-full-16.json](encode-recheck-20260916/fg-full-16.json)、两份`fg-full-16-*.mp4`。

## 4. HDR：补了真实GPU执行，不冒充原生HDR素材

用户目录盘点没有带PQ/HLG的原生HDR素材；使用已有320×180合成PQ/HLG样本，各24帧。
这次调用生产FFmpegHDRVideoReader的颜色转换，而非仅`format=rgba64le`；
ProcessLive执行RGBA16F DLSS、CPU线性光混合mix=.7、FFmpegVideoWriter强制NVENC HEVC Main10。
每个profile复跑一次同样序列，第一帧reset。

- PQ与HLG的两次DLSS像素/混合像素哈希和最终MP4均一致。
- 两类输出24帧、24fps、1秒；Main10/yuv420p10le/bt2020/bt2020nc/tv，PQ或HLG transfer标签正确。
- 未对mastering-display/content-light全部HDR附加元数据做认证；fixtures无音轨，也非相机/发行原片。
- 独立将GPU `analysis_hdr` 与生产分析比较：PQ的6帧共9个通道不同，HLG的2帧共2个通道不同，
  最大1/255；先前两帧CPU测试“分析相同”不能扩展为CUDA相同。
- GPU混合在两类的24帧都存在差异：PQ累计1135通道、最大0.000244140625；
  HLG累计2693通道、最大0.00048828125。没有把这些GPU结果喂给生产导出来掩盖差异。

初版报告通用PSNR误用了255峰值处理HDR浮点，未据此验收；该版完整保留在`hdr.json`。
修正比较器为HDR不输出PSNR后重新实测，最终用
[hdr-final.json](encode-recheck-20260916/hdr-final.json)，不覆盖初版或旧编码样本。

## 建议的正式改动范围

1. **现在保留HDR端点快路径**。不改mix=.7等实际混合、预览对比、模型或精度。
2. **保留正式RAFT光流直连和现有FFmpeg/NVENC编码**，不把研究中的原生编码器替换进去。
3. 若继续投入，只开一个工程目标：使共享显存帧接入与现有FFmpeg一致的编码/色彩链路，
   或明确另设用户选择的编码策略；不能偷换当前画质合同。先做接口实现，再针对差异验收，
   不无限重复同一组短测。
4. VSR/深度/HDR算术迁移保持隔离；本轮未重测VSR与深度，沿用已有“不通过”的结论，
   不因其他模块测试通过而连带放行。GPU解码及RAFT输入常驻无新增收益证据，仍低优先级。

## 验证与收尾

相关24项单测通过，`git diff --check`通过。额外测试用例锁定HDR不使用8bit PSNR与16→32帧序。
完整仓库测试未重复执行；本轮只有harness修改和证据文件，没有新增生产代码改动。
正式DLSS host SHA256仍为`c8ad631f8f78b2dedec6aec418c7a570d6fc5bf1b9ffa9f3514bcb0edd58fc13`。

F盘可用约95GiB；两次尝试读取全tmp统计均被工具审批服务429拒绝（第二次已有用户明确批准），
没有绕过目录访问限制。本轮复用已安装依赖和二进制，自己任务目录峰值约5.1MiB，不生成大型产物。
旧临时目录、其他AI产物和任何备份不在清理范围。自己的证据归档、哈希与清理明细见证据README。

复现入口：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\encode-recheck-20260916'
$env:TMP=$env:TEMP
$env:PYTHONDONTWRITEBYTECODE='1'
.venv/Scripts/python.exe -B scripts/encode_recheck.py --work tmp/encode-recheck-20260916 --case endpoint --label fresh-endpoint
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/encode_recheck.py --work tmp/encode-recheck-20260916 --case hdr --label fresh-hdr
```

编码合同复测需使用新的登记目录（其样本文件名固定，禁止覆盖证据）；插帧用新label。
原始执行参数见每个JSON的argv或scope/frames/native_root字段；源码快照保留实际计时版本。
