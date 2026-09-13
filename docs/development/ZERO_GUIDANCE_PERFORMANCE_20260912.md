# v1.1.0 → 当前零引导性能差异诊断（2026-09-12）

## 结论

原片《260429广寒宫98s.mp4》关闭光流后的导出降速，主要可由宿主提交设置从
`merged + 2 in-flight` 变为 `compatibility + 实际1 in-flight` 解释。
当前代码和当前 DLL 不降画质，仅恢复前者，短窗口编码流水线从58–60 fps恢复到约98 fps。
这与用户旧图95.1 fps、新图56.3 fps（约下降40.8%）高度吻合。

这是当前版本不同设置的因果对照，**不是重新运行v1.1.0正式安装包的整片验收**；
不声称恢复到所有场景100+ fps，不把纯DLSS的140 fps当作导出吞吐。
第一张图的8.7 fps属于另一视频的预览缓存统计，不能与后两张导出读数直接比较。

本次只新增诊断脚本和报告；未修改产品代码、用户设置、原片、DLL、驱动或模型。
工作树原先已有其他任务修改，本次全部保留。

## 历史证据

| 项目 | v1.1.0 | 当前默认 / 当前本地设置 |
| --- | --- | --- |
| 宿主 | auto，优先v2 | auto，优先v2 |
| 提交 | merged | compatibility / compatibility |
| 持久上传/回读 | true | true / true |
| 零引导快路径 | true | false / **true** |
| 请求在途队列 | 2 | 3 / 3，但兼容提交实际只有1 |
| 光流 | 尚无可选模型链路 | 默认1；本地已设0 |
| 导出默认 | H.264 NVENC p5、CQ19、hq | 本次SDR原尺寸高质量p5仍为同组编码参数 |
| 本地结构 / 自动皮肤蒙版 | 默认不勾选 | 默认勾选；不等于性能主因 |
| 预览尺寸策略 | auto | original |

- `0a1dc59`（2026-09-06）调整v1.1.5默认效果、缓存、预览原尺寸和队列3；此时仍是merged/zero-fast。
- `5c4e90a`（2026-09-09）在引导输入优化与激活配置中，把默认提交从merged改为compatibility。
  CHANGELOG和`docs/guidance/GUIDANCE_ACTIVATION.md`说明采用当时的用户实测引导配置，
  并没有按“零引导/带引导”分别保留提交策略。
- `4179dd4`（2026-09-10）把默认guidance_mode从0改1、zero-fast从true改false；兼容提交保持不变。
- 本次读取`var/dlss5_settings.json`实际为guidance_mode=0、zero-fast=true、compatibility、queue=3。
  因而不能把当前实际降速全部归因于zero-fast关闭；截图没有展示该复选框的状态。

代码定位（当前工作树）：

- `dlss5tool/app_settings.py` DEFAULTS：默认值变化。
- `dlss5tool/gui.py` `_collect_host_settings`：关闭光流只影响guidance_mode及快路径可用性，
  不自动把host_submission变回merged。
- `native/host_v2/dlssnr_host_v2.cpp` `ProcessCompatibility`：上传、Evaluate、回读分别同步提交并等待。
- 同文件`AllocateSlots`相关分配逻辑、`dlssnr_capabilities`及`EnqueueFrame`：
  非分块、merged、persistent三者满足时才支持异步队列。
- `dlss5tool/dlss_engine.py` `_refresh_capabilities`：不具备异步能力时强制max_in_flight=1。
- `dlss5tool/gui.py` SDR导出循环：supports_async为真时enqueue/dequeue；否则逐帧process。

因此界面的“GPU队列3”是请求，不代表兼容提交真有3帧并行。
增加跨帧重叠可以隐藏CPU拷贝、进程通信和GPU等待，不是把DLSS时序模型改成乱序计算。

## 实测方法与环境

- 日期：2026-09-12；RTX 4070 SUPER；nvidia-smi驱动616.92；Python现有环境，OpenCV4.13.0、NumPy2.4.1。
- 仓库HEAD：35b59a8，包含开始诊断时已有未提交修改，不能视作干净v2.2.0正式包。
- 原片：用户指定素材目录中的《260429广寒宫98s.mp4》，1080×1920、60 fps。
- 所有对照使用同一当前宿主和同一内置运行时；关闭guidance、同分辨率、同效果参数。
- 每个case独立进程初始化，先预热24帧；两轮逆序运行，GPU测试不并行。
- native/proxy：复用前16帧内存窗口，计时160帧，无解码/编码；另计16帧输出哈希。
- pipeline：4帧解码预读、生产ProcessLive、单后处理/编码线程，顺序读取原片前480帧。
  H.264 NVENC p5/hq/vbr/CQ19、yuv420p，与本次应用的编码参数一致。
  FFmpeg写null输出，不生成视频；包含编码器排空，不含NGX初始化、GUI刷新、MP4落盘/faststart和音轨封装。
- 未关闭或干预用户正在运行的程序；短测仍可能受系统负载影响。

当前二进制SHA-256：

- `runtime/dlssnr_host_v2.dll`：F6302D39F5505C2BC71F54147EB4F0756F9CFAED1D9BA7858D85EA7712CB9996
- `runtime/nvngx_dlssnr.dll`：CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650

### 最接近导出的编码流水线

| 配置 | 实际队列 | 第一轮fps | 第二轮fps |
| --- | ---: | ---: | ---: |
| compatibility，零引导快路径开 | 1 | 58.10 | 59.99 |
| merged，零引导快路径开，请求2 | 2 | 98.08 | 97.73 |
| compatibility，零引导快路径关 | 1 | 54.74 | 53.19 |

前两项平均59.04 vs 97.91 fps；兼容模式约慢39.7%，恢复合并双帧约提升65.8%。
与用户截图的比例接近，但不能用这次短测替代5900余帧的整片导出验收。
流水线全部case的另计16帧输出哈希一致；特别是仅切换兼容/合并与队列，没有观察到画质代价。

### 分层定位

| 配置 | native两轮fps | ProcessLive两轮fps |
| --- | --- | --- |
| compat + fast，请求3实际1 | 109.36 / 110.10 | 86.13 / 67.87 |
| merged + fast，1帧 | 113.01 / 112.38 | 86.17 / 87.49 |
| merged + fast，2帧 | 140.09 / 139.77 | 139.28 / 141.31 |
| merged + fast，3帧 | 138.02 / 136.97 | 139.06 / 140.57 |
| compat，无fast | 91.35 / 89.14 | 79.22 / 76.84 |
| merged + fast，2帧，结构0/皮肤蒙版关 | 134.32 / 142.30 | 135.81 / 139.72 |

单独把提交改merged但队列仍为1，收益很小；双帧重叠是关键。第三帧在此窗口没有明显额外收益。
本地结构/皮肤开关未显示明显吞吐差距，不能靠关画质选项解释此前约40%的降速。
ProcessLive的共享内存与隔离子进程在v1.1.0已经存在，不应误认作本版才新增的开销。

限制/异常：native的无fast第一轮哈希不同、第二轮一致；原始结果保留，
不对“快路径开关所有场景逐像素等价”作保证。核心compat-fast与merged-fast对照在三层测试均一致。
本次没有启动光流模型，也没有测试HDR、超分、分块、不同驱动/显卡。

## 预览8.7 fps的独立解释边界

- 第一图为1312×2304、另一原片；后两图为1080×1920导出。前者像素量约为后者1.46倍。
- `_update_preview_timeline_and_status`显示的是累计缓存处理帧数除以从首次缓存入库起的墙钟时间，
  不是GPU逐帧执行时间；等待、停顿与调度影响该读数。
- 当前`_preview_decode_tick`在UI线程逐次读取并投递一帧，之后由定时回调继续，
  与导出的后台解码预读流水线不同。UI回调/缓存扫描/供帧可能限制预览，但本次未测其时间线。
- 旧auto策略仅对长边>2560的素材缩小；第一图长边2304，**不能**把它的8.7直接归因于
  “旧版auto缩小、新版original原尺寸”。这段素材两种策略都会保持原尺寸。
- 因而本次定位了导出主因，尚未独立确认第一图预览8.7 fps的全部成因。

## 建议后续动作（未执行）

在关闭光流的测试中，设置：合并提交（快速）、GPU队列2、持久上传/回读开、零引导快路径开。
继续保留当前画质参数与NVENC p5；日志应显示GPU队列2帧。只调队列而保留兼容提交无效。
再做同片完整导出，比较完成日志的平均fps，不只看早期进度瞬时读数。

产品修正候选：为零引导和启用光流分别保存/选择提交策略；明确显示请求队列与实际队列及降级原因；
预览吞吐和纯处理时间分开统计。需维护者授权后再实现，不全局强制merged覆盖光流/HDR兼容性选择。

## 复现与产物卫生

脚本：`scripts/zero_guidance_perf_probe.py`。

```powershell
python -B scripts/zero_guidance_perf_probe.py --video 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --work-dir tmp/perf-regression-20260912
python -B scripts/zero_guidance_perf_probe.py --video 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --work-dir tmp/perf-regression-20260912 --proxy
python -B scripts/zero_guidance_perf_probe.py --video 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --work-dir tmp/perf-regression-20260912 --pipeline --frames 480
```

原始JSON与小型日志：`tmp/perf-regression-20260912/`；预计总计不足0.1MiB，作为诊断证据保留至9月19日复核。
首次pipeline执行遇到诊断脚本变量遮蔽queue模块，已修正后完整运行两轮；遗留小日志保留用于失败审计。
没有生成视频、复制依赖或构建DLL；没有删除用户既有文件，也没有清理其他任务的tmp目录。

## 后续：关闭光流默认开启快路径（2026-09-12）

根据维护者后续要求，已在源码实现（不代表原有EXE已重新打包）：

- GUI从启用引导切回关闭时，自动勾选`host_zero_fast_path`并进入既有保存/宿主重载流程。
- 开启引导时取消该勾选，维持禁用状态；底层仍强制保护真实光流不被全零运动纹理替代。
- 引导激活失败退回关闭时，恢复快路径。
- 不含该字段的新零引导设置默认开启；显式保存的关闭值保留，方便兼容诊断。
  已关闭光流后手动取消快路径，不会因无关设置编辑被再次勾上。
- 本轮没有改变提交方式、队列、画质参数、DLL或worker。

仅光流模式不能直接打开现有零引导快路径：`SetEvaluationParameters`会将MVec替换为
`g_zero_motion`。可复用的优化思想是保留真实运动向量、只跳过未启用的深度通道。
工作区已有该项独立源码改动及验证，详见`DEPTH_CLEANUP_20260912.md`；当前runtime与mods
二进制未在此任务更新，不承诺该优化已生效或有固定提速百分比。

验证：设置默认/显式覆盖、模式切换、激活失败、忙碌保护及真实Tk控件勾选/禁用/持久化回归通过；
相关组合测试104次通过（包含重复导入的9项既有用例；随后移除重复导入）。
UI/UX技能用于确保禁用状态与真实执行状态一致，不改布局。
本轮测试临时配置自动回收，`tmp/zero-fast-default-20260912/`仅保留796字节TASK登记，9月19日复核。
