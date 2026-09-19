# 编码传递接入：第一阶段交付 · 2026-09-17

## 结论

2026-09-19更新：[真实SDR插帧＋音视频封装已接通候选](GPU_EXPORT_INTEGRATION_20260919.md)。
本页保留前一阶段证据；下方“未完成”描述以最新报告为准，HDR GPU转换仍未完成。

**已完成 SDR/PQ/HLG 整数 YUV 显存编码接口、CFR包时间戳，以及不缩放SDR的RGB→I420
GPU转换和串联验证。HDR的GPU转换、D3D生产者和容器/音频仍未接完；GUI/导出默认未改变。**

以下保留阶段一原始验证，并在后续进展中补充时间戳和SDR转换；不能称整个升级已完成。
现有默认仍为 FFmpegVideoWriter；RAFT、深度、VSR、DLSS reset/帧序和正式 DLL 均未改。
HDR mix=0/1 快路径保持用户工作区原改动，没有换实际混合算法。

## 本轮实现

1. `dlss5tool/encoding_contract.py`：提取正式编码入口的像素格式、缩放/补边、颜色转换和标签，
   `FFmpegVideoWriter` 与对照脚本共同使用。不再用另一条 `format=nv12` 转换冒充正式 `yuv420p`。
2. `scripts/gpu_nvenc_ring.cpp`：显式 Main/Main10、P5 HQ、MP4 global header、初始 I/P/B QP、
   多槽输出延迟、EOS。扩展系统内存 I420/P010 和显存 I420/P010 两条输入。
3. GPU 输入先同步 D2D 拷贝到编码器自己的环形槽，再交给 NVENC；编码完成才 unmap/复用。
   SDR 平面在显存中按 I420→YV12 顺序复制，HDR 保持 P010 整数字节，不做近似颜色算法。
   使用 CUDA 2D copy，不是逐像素/逐行 CPU 回读。输出只有压缩码流回 CPU。
4. `dlss5tool/nvenc_yuv.py`：提供明确选择的编码会话接口，不导入 Torch、不自动选卡。
   检查线程/CUDA context、输入格式/字节数/序号、单 DLL 单会话所有权、EOS 后拒绝提交。
   输入 owner 必须先等生产者完成，并保持来源显存有效直到同步 D2D 返回。
5. 修复部分输入槽分配成功后、后续分配失败时的清理遗漏；增加失败注入与重复关闭检查。

当前接口需要调用者明确提供 native DLL 路径；正常导出没有调用它。
这是待集成的编码核心，不会因为模块存在就静默开启新路径。

## 编码差异的定位过程

本机 FFmpeg 7.1.1 的默认 H264 profile 为 Main；原候选直接保留预设 profile。
补齐 Main、multipass、frame field 和逐图 slice 设置后，24 帧中的前13帧已对齐，尾部仍不同。
输出延迟和独立global header的两轮检查没有消除剩余差异，负结果也保留。

最后对齐了 FFmpeg `set_vbr` 的初始 QP 计算：P=26、I=21、B=34，取代全部26。
该候选的24帧解码与正式FFmpeg全部相同；后续 SDR、PQ、HLG 测试继续通过。
这说明此前不能只看 preset/CQ 名称相同，就声称所有编码参数已经一致。

依据：[FFmpeg n7.1.1 NVENC 实现](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libavcodec/nvenc.c)、
[H264 默认选项](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libavcodec/nvenc_h264.c)、
[通用量化因子默认值](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libavcodec/options_table.h)。
本轮没有将第三方源码复制进产品。复用此前已保留的官方NVENC头文件。

## 验证结果

全部证据：[目录与校验清单](encoder-integration-20260917/README.md)。

| 验证层 | 覆盖 | 结果 |
| --- | --- | --- |
| 系统内存编码 | SDR/H264、PQ/Main10、HLG/Main10；P5 CQ19/CQ23；各24帧×2轮 | 六组均与正式FFmpeg逐帧MD5相同 |
| CUDA整数YUV输入 | 同上；只改输入传递方式 | 六组均相同 |
| Python会话接口＋CUDA输入 | 同上；含EOS重复调用 | 六组均相同，合计288帧对照 |
| 部分分配失败 | 系统内存/显存两模式，第0/3/15槽故障 | 6组拒绝初始化，owned资源计数归零 |
| 取消清理 | SDR/PQ/HLG，提交0/3/17帧后关闭 | 9组计数归零，重复close安全 |
| 取消后重开／槽位复用 | 三种profile各120帧 | 各输出120包、清理计数归零 |
| 相关单测 | 编码合同、会话状态、视频导出、HDR、插帧与缓存 | 73项通过 |

早先测试命令误写不存在的 `tests.test_video_export`，该次报告有1个导入错误；
随后改用实际存在的 `tests.test_video_encoding` 和 `tests.test_video_encoder_selection`，
最终73项通过。不把错误调用计为通过，也没有改测试跳过错误。

完整仓库单测未重复运行；没有对外部GPU负载作独占控制，因此不报告端到端速度收益。
故障注入覆盖的是可控部分分配失败，不等于驱动重置/设备移除故障认证。

## 输入和范围

- 新SDR测试：用户 `9月1日.mp4` 的24帧缩小为320×180。
- 初始参数排查：`260429广寒宫98s.mp4` 前24帧缩小为360×640。
- PQ/HLG：已有320×180合成HDR fixtures，不冒充用户原生HDR素材。
- P5 CQ19/CQ23是本轮明确验证范围。位速率模式、其他预设/质量档、4K长片尚未认证。
- GPU门槛的YUV源来自**生产FFmpeg颜色转换后上传**，刻意隔离编码和传递。
  不代表RGB纹理已经在显卡上精确转成同样的YUV，更不是端到端零回读。
- Python封装对不支持的色彩/codec和位速率模式明确拒绝，不能偷偷替换用户选择。
- HDR保留PQ/HLG、BT.2020/BT.2020nc、Main10输入合同；mastering-display/content-light
  附加元数据和真实HDR长片不是这组验证的结论。

## 未完成的下一阶段（不能省略或声称已交付）

### 后续进展：不缩放SDR转换已串通

新增融合CUDA内核及 `CudaSdrConverter`：RGB/BGR/RGBA/BGRA→I420在GPU完成，
支持输入行距与奇数尺寸黑边，不需要整帧中间缓存。144组布局/行距比较逐字节相同。
转换输出直接接显存编码器，用户素材6组126帧（含1080p/4K短样本）全部与正式FFmpeg
解码和时间戳一致。85项相关单测通过。
详见[SDR转换证据](encoder-integration-20260917/sdr-conversion/README.md)。
这是候选核心，不是正式导出默认；输入仍由测试上传，D3D共享句柄/fence未接。
HDR、缩放和非默认颜色合同会被入口拒绝；没有用近似结果冒充等价。

### 后续进展：CFR 包时间戳已补齐

候选现为 ABI2：返回 `EncodedYuvPacket`，包含 NVENC 显示时间戳、解码时间戳、
单帧时长、有理数时基及关键帧标记；不再让调用者把包序误当显示顺序。
21组453帧的SDR/PQ/HLG对照全部通过，覆盖极短片、29.97和60/72/96fps。
78项相关单测通过，ABI2生命周期复测通过。首轮HDR参考输入漏标签的负结果也已保留。
详见[时间戳证据](encoder-integration-20260917/packet-timing/README.md)。
这只完成下面第2项的**编码包时间戳部分**；没有完成容器、音频或真实插帧集成。
原阶段一证据保持不变，旧ABI1候选由可重建的ABI2候选替代，正式运行库不变。

1. 不缩放SDR RGBA8→I420及奇数尺寸补边已完成候选验证；尚需D3D12上游接入和
   HDR RGBA16F→生产等价P010 GPU转换、输出缩放、
   原图混合和对比视图。算法结果必须对照生产转换，不能替换为NVENC内建RGB转换。
2. 音频与容器、跨进程句柄/fence；目前接口返回带CFR PTS/DTS的码流包，
   不是已完成的正式muxer。B帧必须保留重排信息，不能简单用包序当显示顺序。
3. 将DLSS与插帧生产者接到上述同一个编码消费者，并处理预览/缓存所需回读。
   HDR插帧、3×/4×、取消恢复和完整输出时间线需要各自验收。
4. 通过同参数实际导出性能对照后，才讨论开启路径与正式打包；不提前更改默认。

## 复现

复用已有环境/SDK，不复制模型或正式运行库。任务目录需TASK.md；不得覆盖已有报告。

```powershell
cmd /c scripts\build_nvenc_ring_probe.bat tmp\encode-integration-20260917
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/encoder_contract_gate.py --work tmp/encode-integration-20260917 --native-root tmp/encode-integration-20260917 --label new-gpu-case --device-input --interface
```

GPU实测在独立Python进程中运行，父进程给180秒超时；驱动级卡死不靠Python线程取消。
本次仅保留一个当前候选DLL；native目录中的obj/lib/exp在归档后清理，正式DLL无变动。
新任务新增文件峰值约3.9MiB，预算64MiB。F盘起始约95.1GiB可用；全tmp盘点因无关目录ACL
受限，升级读取也未完成，不声称拿到了最新总量，不清理这些目录、不修改ACL。

后续时间戳及SDR转换沿用同一任务64MiB上限，未复制依赖。阶段一已清理63项可重建产物，
逻辑3,872,214字节、观察磁盘可用增加4,005,888字节，随后候选迭代仍只有一套。
续轮新增峰值约11.1MiB；归档校验后清理67项重复媒体、报告和编译中间文件，逻辑
11,449,026字节，观察磁盘可用增加11,583,488字节。剩余约150KiB，仅当前编码DLL、
SDR转换PTX与任务登记，保留用于后续接入，2026-09-24复核。清理内容可从归档恢复或重建。
