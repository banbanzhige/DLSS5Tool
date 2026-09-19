# GPU 导出接入进展 · 2026-09-19

## 当前结论

后续已完成HDR和普通增强链路及应用选择：[本地升级验收](GPU_EXPORT_UPGRADE_20260919.md)。
本页保留当时阶段记录，当前状态以新报告为准。

**真实SDR插帧已通过主 `export_video` 循环接到GPU转换、NVENC、MP4/MKV/MOV和原音轨。
不是只喂高帧率测试帧。仍为显式候选路径，没有更改GUI默认，没有完成HDR GPU转换或发版。**

新入口：`export_video(..., gpu_export={worker, native_library, sdr_ptx, device_ordinal})`，
以及 `python -m dlss5tool.frame_generation` 的三个 `--gpu-*` 文件参数。
三者须同时提供；不提供保持原流程。HDR、对比视图、缓存输入等未支持组合会明确拒绝。
当前只验收P5/high或balanced、SDR/H264；不会静默降HDR、换codec或改码率设置。

## 本次落地

- `NativeStream.process_device`：真实DLSSG GPU协议；每张生成帧先等D3D12 fence，
  主导出线程调用GPU消费者，完成转换与编码器D2D拷贝后才ACK。3×/4×的各子帧不会互相覆盖。
- `SharedCudaBuffer`：复制跨进程句柄、检查物理GPU身份、映射CUDA；在CUDA上下文释放前回收。
- `NativeGpuVideoWriter`：复用已验证SDR融合内核和整数YUV编码器。生成帧驻留显存；
  **原帧、解码、NVOFA输入和已有CPU预处理仍走原逻辑**，不称全链路零回读。
- `PacketVideoWriter`：PyAV按编码包PTS/DTS封装，不重新编码视频；复用原FFmpeg音轨策略。
  首包解析获取容器参数；不调用逐帧软件解码。音频先写独立临时文件再发布，防止竞争覆盖目标。
- 生命周期：回调失败杀死等待ACK的工作进程；取消清理共享资源、编码会话与临时文件。
  GPU驱动硬挂起仍需要外层进程超时；当前GUI默认未接此候选，不能当作硬故障认证。
- 可选依赖 `requirements-gpu-export.txt` 固定PyAV 18.1.0。只安装于现有`.venv`，
  27.6MB轮子，无Torch/CUDA环境副本；发布包依赖/许可证收集未接，未运行打包。

## 已跑验收

证据见 [归档与哈希清单](gpu-export-integration-20260919/README.md)。

| 范围 | 实测 | 结果 |
| --- | --- | --- |
| 容器 | SDR/PQ/HLG整数YUV × MP4/MKV/MOV，各24帧 | 9组画面与同容器时间戳一致 |
| 音频 | 上述9组分别带短AAC、PCM、长AAC | 各9组画面/时间戳/音频包一致；复用原兼容转换策略 |
| 真实2× | 用户素材12源帧→24帧，MP4，短AAC | 画面、时间戳、音轨、分类帧数一致 |
| 真实3× | 12→36帧，MKV，短AAC | 同上 |
| 真实4× | 12→48帧，MOV，短AAC | 同上 |
| 硬切4× | 黑→白12源帧→48帧，MP4 | 同上；一次切换产生3张保持帧 |
| 取消/重开 | 4×处理中及结束封装前取消，再重开同DLL | 无发布/临时输出残留、cleanup_errors为空，重开各3帧可解码 |
| 单测 | GPU协议、封装边界、CUDA/编码合同、HDR、插帧与缓存 | 94项通过；diff whitespace通过 |

所有GPU验收用独立进程和180/240秒父进程超时，串行运行，不争抢GPU。
不报告稳定提速百分比，没有做独占GPU长片性能测量；完整仓库测试没有重跑。
用户素材仅生成320×180短fixture；3×/4×继承原固定运行库和RTX4070 SUPER实验限制，
**逐帧一致证明传输不改结果，不证明原插帧算法已经消除时间位置/画质问题。**

## 保留的负结果

- `mux.json`：首次将decoder模板的codec time_base设值，PyAV明确拒绝；随后仅设置stream时基。
- `stream-timebase.json`：MP4通过；对MKV的测试工具错误地假定每个包都有DTS，出现KeyError。
  更正后同时比较双方缺失字段，未伪造DTS；9组容器对照通过。
- NVIDIA技能目录已查询，没有强匹配Windows DLSSG/CUDA/NVENC专项技能；未安装技能。

## 还没交付的部分

1. HDR RGBA16F→生产等价P010 GPU转换。PQ/HLG已过的是整数YUV编码/封装，不是HDR显存图像入口。
2. 非插帧DLSS增强生产者的D3D12驻留输出、比较视图、GPU缩放/混合与缓存/预览策略。
3. GUI生产选择、独立导出宿主的硬超时/设备故障控制、长片性能和打包依赖/许可证。

这些没有被标记为完成，也没有用CPU回读伪装成HDR显存直连。

## 复现

复用前一阶段编码DLL/PTX，不覆盖正式运行库：

```powershell
cmd /c scripts\build_gpu_export_worker.bat tmp\export-integration-20260917
.venv/Scripts/python.exe -B scripts/gpu_export_fg_gate.py --work tmp/export-integration-20260917 --label fresh-fg2 --multiplier 2
```

脚本内记录素材、源文件/候选哈希、帧MD5、容器时间戳和音频包哈希。复现也应使用父进程超时。
临时预算沿用256MiB、实际远低于64MiB；保留一个当前候选worker，旧正式worker不动。

## 归档和清理

164个证据/快照文件共7,158,614字节已逐项校验。清理本任务152项重复产物，逻辑
7,626,459字节；观察F盘可用空间增加7,942,144字节。剩余约387KiB，仅当前worker、
取消复测仍引用的45KiB源片段与任务登记，2026-09-24复核。证据可从归档恢复，编译中间文件
可重建。没有删除旧任务、模型、依赖或正式运行库。全tmp最新总量仍受此前无关目录ACL限制。
