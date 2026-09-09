# 原生引导上传优化与调度实验 · 2026-09-08

## 结论与范围

用户指出的每帧临时 `vector<uint16_t>` 光流转换和 `vector<float>` 深度复制确实存在，
但它们在 **非零引导上传路径**，不是已经启用的零引导快速路径。
快速路径只在 `InitializeZeroTextures` 初始化零纹理一次，此后跳过逐帧光流／深度上传。
这次不宣称加速了该快速路径。

生产源码已改为：

- 光流：按 D3D12 footprint 偏移和 RowPitch，将原 FloatToHalf 的结果直接写入上传堆。
- 深度：按行直接从输入 float32 memcpy 到上传堆，取消全帧临时数组和中转复制。
- 空指针或对应引导模式未启用时直接清零有效行，不遗留前一帧数据；不写行填充区。
- 保留旧 FP16 转换全部位行为，不用硬件转换替换其舍入／溢出规则。
- 映射上传堆只写不读，光流使用 volatile 半字存储，避免 write-combined 内存上的意外读改写。
- 队列槽及 fence 生命周期不变；没有跨槽复用一块仍被GPU读取的上传数据。

参考微软关于 UPLOAD heap 避免 CPU 读取、持久映射与 fence 的说明：
[ID3D12Resource::Map](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12resource-map)。

1440×1440 时去掉两份共约15.82MiB的逐帧临时数组，以及相应初始化和中转复制。
GPU纹理格式、上传大小和模型计算不变；这不是CUDA↔D3D12零拷贝。
关闭 persistent_buffers 时原生临时 D3D12 staging 仍按旧逻辑创建，本轮只移除CPU临时数组。

## 实测

同机 RTX 4070 SUPER，MSVC x64 /O2，真实 D3D12 映射 UPLOAD 堆，1440×1440。
每项预热4次、计时50次，两轮反向顺序。

| CPU准备步骤 | 原实现 ms | 直写上传堆 ms |
| --- | ---: | ---: |
| 光流转换与准备 | 4.579 | 2.942 |
| 深度复制与准备 | 2.382 | 0.226 |
| 两项合计 | 6.961 | 3.168 |

局部合计节省约3.79ms／帧（约54.5%）。这是CPU准备微基准，不包括GPU纹理复制、模型或DLSS执行，
不能当作整条视频链路快54.5%；在约200多ms的混合引导链路中，只是小幅收益。
原始记录：`output/native-upload-ab/upload-microbenchmark.json`。

原生DLL前后对照，不运行Torch模型，使用同一原片16帧和可控引导图：

- zero_fast、zero_upload、mixed、async（三槽）、odd（641×359）、transient、compatibility 七类全部输出哈希一致。
- 引导模式在3／1／2／0间切换；含空光流、空深度、显式reset、在途槽期间覆写输入。
- 报告 `output/native-upload-ab/report.json`。
- 这些短运行的时间包含哈希和部分首帧开销，仅作辅助观察，不按其百分比承诺性能。
- 另用真实冻结SDPA＋FP16引导组件配新DLL，三种模式的同步／三槽异步结果一致，reset和非活动引导清零通过：
  `output/native-upload-guidance-smoke/report.json`。
- 全量Python测试236项：233通过，3项Torch可选测试跳过；本轮C++测试单独运行通过。

`tests/native_guidance_upload.cpp` 比对独立保留的旧算法：不同宽度、RowPitch填充、512字节偏移、
边界哨兵、空指针、正负零、次正规数、无穷／NaN及一百万随机浮点位模式，全部通过。
现有转换对特殊值的历史行为被保留，不把它描述为IEEE半精度标准舍入实现。

## 构建与部署状态

- 新源文件：`native/host_v2/guidance_upload.h`，`dlssnr_host_v2.cpp` 调用它。
- `native/host_v2/build.bat` 可指定独立输出目录，不再为测试覆盖根目录DLL及对象文件。
- 已验证候选DLL：`tmp/native-upload-20260908/dlssnr_host_v2.dll`。
- **已在用户关闭主程序后部署到根目录 `dlssnr_host_v2.dll`**：部署前确认无GUI／引导进程，
  核对旧版与候选SHA-256，再备份并替换；未强行结束用户进程。
- 原DLL备份：`tmp/native-upload-backup-20260908/dlssnr_host_v2.dll`。
  旧版SHA-256：`78BC86B3841A535F7B3D706EED38C344C88AECA14B36A6E45F08D418FABB2436`。
  新版SHA-256：`48A7752D0DF2238CBC3F91056DE80C9FA55858ACF0A40AADEF6837A563227A8D`。
- 重启开发版 `run.bat` 即使用新宿主，不需要改引导参数；RAFT精简与双Stream仍未部署。
- 部署后使用默认宿主路径复测三种引导模式，同步／三槽异步、reset及非活动引导清零全部通过：
  `output/native-upload-deployed-smoke/report.json`。
- `nvngx_dlssnr.dll`、当前GPU引导组件、模型和用户设置未由本轮修改。
- 打包版EXE旁的 `_internal/dlssnr_host_v2.dll` 也需相应更新；本轮没有重打包发布包。

构建候选：

```powershell
cmd /c native\host_v2\build.bat tmp\native-upload-new
```

C++测试：在VS x64开发终端中运行（目标目录先创建）：

```text
cl /nologo /std:c++17 /O2 /EHsc /MT tests\native_guidance_upload.cpp /Fo:tmp\native-upload-new\upload_test.obj /Fe:tmp\native-upload-new\upload_test.exe /link d3d12.lib dxgi.lib
tmp\native-upload-new\upload_test.exe
```

## RAFT精简与双Stream（独立实验，未部署）

`scripts/guidance_schedule_probe.py` 在独立子进程测试四个变体：现状、RAFT仅输出最后一次上采样、
两个CUDA stream、两者组合。RAFT仍做6次低分辨率更新；深度仍SDPA＋AMP FP16，光流仍FP32。
没有同时调用同一个RAFT实例；两个stream只分别运行不同模型，先提交两者再等待CPU回读。
这不是完整生产并行调度实现，也没有依据此测试修改模型组件。

输入使用上一轮已保存的同一原片窗口，每段前3帧预热、后4帧计时，三段、两轮反向顺序，
每种配置共24个计时帧。仅测引导处理，不含IPC、DLSS或编码。

| 变体 | 平均 ms | 中位数 ms（合并两轮） | Torch峰值分配 MiB |
| --- | ---: | ---: | ---: |
| 当前串行 | 187.59 | 180.87 | 3066.89 |
| RAFT仅最后输出 | 174.04 | 173.27 | 3066.89 |
| 双Stream | 172.50 | 172.33 | 3080.05 |
| 组合 | 167.21 | 167.39 | 3080.64 |

全部抽测光流与深度数组相对基线逐值完全一致，无NaN/Inf。
组合的中位耗时约减少7.5%，平均约减少10.9%；基线有一次约304ms异常慢帧，平均值容易受其影响。
因此报告为“有小幅优化潜力”，不宣称稳定提升12%或并行翻倍。
没有测CUDA kernel时间线，不能把收益全部归因于GPU算子同时占用；CPU后处理与GPU工作重叠也可能贡献收益。
测试显存只增加约14MiB不代表其他输入尺寸都如此。

原始逐帧时间和误差：`output/guidance-schedule-ab/report.json`。
下一步需扩展方向、seek／切镜、模式／尺寸变更、错误恢复以及冻结组件的生命周期测试后再正式接入。
