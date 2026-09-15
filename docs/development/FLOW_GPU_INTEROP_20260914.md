# 光流 CUDA → D3D12 直连实验（2026-09-14）

## 最终结果（用户要求继续后的修正版）

**独立实验通过：GPU 直连可行，已测两个片段光流与最终 DLSS 均逐位一致，正常退出。尚未接入 GUI/正式跨进程通路。**

在两个原片的第 80～103 帧分别做 24 帧质量对比、4 轮交叉计时（每路径每阶段 96 帧），
含 RAFT 的平均结果如下；所有 CPU 重复控制、GPU motion 位比较和 DLSS RGB 比较均为 0 差异。

| 输入 / 光流尺寸 | 旧路径 ms/帧 | GPU 直连 ms/帧 | 吞吐提升 |
| --- | ---: | ---: | ---: |
| 1440×1440 / 512×512 | 37.24 | 30.75 | 21.1% |
| 1080×1920 / 408×720 | 39.88 | 34.59 | 15.3% |

单独测“光流后处理＋传递＋DLSS”，分别从 15.31→8.73 ms、15.18→8.75 ms，
每帧减少约 6.4～6.6 ms。含 RAFT 阶段排除 reset 帧后的平均耗时分别为
39.28→32.78 ms、42.19→36.91 ms。不能把单独这一段约 74% 的吞吐提升当作完整导出收益。
时间随其他负载变化，以同一轮配对结果为准，不能拿下面早期实验与最终实验跨轮相除。

最终证据：[方形片段](flow-interop-20260914/accepted-square.json)、
[竖屏片段](flow-interop-20260914/accepted-portrait.json)；均记录 `status=completed`，进程退出码 0。
保留的源码 hash 与这两份报告一致；候选 DLL 不部署。

解决的数值问题：

1. 采样坐标必须按 CPU/OpenCV 的 double→float 顺序初始化，不能在 shader 中直接用 float 重算。
2. **运动尺度比例也需在初始化时按 CPU 语义转为 FP32 常量。**shader 的浮点除法与 CPU
   比例转换会产生微小误差，后续 half 舍入可放大其影响。该项修正后竖屏 24 帧纹理也逐位一致。
3. HLSL `mad` 变体未保留；最终使用分离乘加、原有 half 位转换和预计算固定参数。

关闭问题对照：不创建共享资源但加载 RAFT、不加载 Torch 的候选宿主、**不加载 Torch 的正式
runtime/dlssnr_host_v2.dll** 都出现关闭超时。参见 `shutdown-no-interop.json`、
`shutdown-no-torch.json`、`shutdown-production-host.json`。源码
`dlss5tool/dlss_host_process.py:173` 已说明此已知行为，正式宿主本来就不在 Evaluate 后调用
NGX Shutdown，而是退出隔离进程。因此本实验默认沿用相同生命周期：显式释放 CUDA 映射、
共享资源、模型后退出一次性进程，余下 NGX 状态由系统回收。没有修补第三方 DLL，
也不声称底层 Shutdown 接口已修好。`--shutdown-diagnostic` 保留该问题的复现入口。

验证：新增 3 项探针 ABI/输入校验/资源释放顺序单测通过；合并相关回归共 49 项，48 通过、1 跳过。
下一步才是接入真实引导 worker 与 DLSS 宿主的跨进程共享句柄协议，并验证 reset、超时、缓存、
可视化、尺寸变化、多卡选择与长视频。不能用当前单进程实验宣称这些场景已验证。

## 以下为早期实验记录（不是当前验收状态）

### 初期结论

**同卡直连可行，存在可测收益；目前是实验，不满足正式接入条件。**

新增独立脚本 `scripts/flow_interop_host.cpp`、`scripts/flow_interop_probe.py`。
没有修改正式 native 宿主、runtime DLL、引导组件、GUI 或用户设置。没有打包/发布。

实验路径为 CUDA RAFT FP32 张量 → GPU 内复制到 D3D12 共享 DEFAULT buffer →
D3D12 compute shader 放大、尺度调整及原有半精度转换 → motion texture → 原有 DLSS Evaluate。
光流不回读 CPU；仍有 GPU 内复制，不应称为完全零拷贝。固定坐标表只在初始化计算一次。

这是同进程、同物理 GPU、单帧串行探针：CUDA stream 完成后 CPU 才提交 D3D12，
D3D12 fence 完成后才允许下一次覆写。没有实现跨进程句柄交接、外部 fence 异步流水线。
输入仍在 CPU，颜色上传和最终 DLSS 画面回读保留。

### 初期本机 A/B

RTX 4070 SUPER 12 GiB，驱动 616.92。复用现有 CUDA Torch 环境与 RAFT-Large 权重，
FP32、6 次迭代、无预测缓存。每段取前 24 帧，包含第 0/12 帧 reset；每路径预热一个窗口，
四轮交替 CPU→GPU / GPU→CPU。每条路径每阶段计时 96 帧。

| 输入 / 光流尺寸 | 旧路径：光流后处理＋DLSS | GPU 路径 | 含 RAFT：旧→新 | 含 RAFT 吞吐提升 |
| --- | ---: | ---: | ---: | ---: |
| 1440×1440 / 512×512 | 21.81 ms | 12.31 ms | 56.30→44.34 ms | 27.0% |
| 1080×1920 / 408×720 | 22.89 ms | 12.15 ms | 62.49→51.35 ms | 21.7% |

对应原始记录：`flow-interop-20260914/raft-final.json`、`raft-720.json`。
第一列测的是同一批预先推理得到的 GPU 张量，不能当成 RAFT 完整吞吐。
含 RAFT 阶段重新执行模型，包含 CPU→GPU 模型输入上传，但输入缩放/归一化预先完成。
不含组件 IPC、原片解码、编码、音频、GUI、超分或插帧，**不是视频导出速度**。
重置帧不执行 RAFT；短窗口重置比例高于常见长视频，不能外推固定百分比。
测试环境存在其他图形活动，未做独占 GPU 性能认证。

### 初期一致性：部分通过，不能推广

- 1440×1440 的坐标表版本：24 帧光流半精度纹理逐位一致，DLSS RGB 逐像素一致；
  CPU 重复控制也完全一致。
- 1080×1920：24 帧中共有 6200 个光流分量的半精度位不同；最终 RGB 共
  28,905,557 / 149,299,200 个通道值不同（约 19.36%），平均绝对差 0.2083/255，
  单通道最大差 12/255。不能把光流差异比例小等同于最终画面逐像素一致。
- 尝试 HLSL `mad` 匹配 CPU SIMD 融合乘加：8 帧短测最终 RGB 一致，但 24 帧长测仍不一致，
  平均绝对差 0.1067/255、最大 12/255；方形素材该变体也出现差异，故已撤回。
  证据：`raft-fma-final.json`、`raft-square-current.json`；`current` 仅是当时实验标签，不代表保留的最终 shader。
- 初版直接在 shader 用 float 算采样坐标，在第一段原片有更大差异；改为与 OpenCV
  一致的 double→float 坐标表后解决了该段差异，但尚未覆盖其他尺寸/舍入条件。
- 保留原有 `FloatToHalf` 位运算语义，包括非标准溢出/舍入行为；不能直接换硬件 half
  转换并声称相同。剩余差异与浮点计算顺序有关是待继续验证的解释，不是已证明的唯一原因。

### 初期退出失败与定位过程

共享 CUDA 映射释放、共享 D3D12 资源释放、模型释放、WaitAll、ReleaseFeature 均完成，
随后卡在 **NVSDK_NGX_D3D12_Shutdown1**。20 秒 watchdog 仅终止本实验进程，报告标记
`cleanup_failed`。没有将这种退出当作正常验收通过，也没有结束用户其他进程。

初次 smoke 的 Python 指针清零写成 None，触发 TypeError，已改为 0；第一次 RAFT 和
第二次短测尚无 watchdog，由终端中断结束。有效报告以带明确 cleanup 状态的长测为准。
是否为 Torch/NGX 同进程共存、运行库自身或互操作生命周期导致，尚未完成对照定位。
正式方案应继续保留进程隔离，并验证共享句柄 / 外部 fence / 超时和 GPU 移除回收。

## 复现与后续范围

先遵守仓库卫生要求，检查磁盘、tmp 占用，并在任务目录创建 TASK.md。复用原环境：

```powershell
$env:TEMP='F:\project\DLSS5Tool\tmp\flow-interop-20260914'
$env:TMP=$env:TEMP
cmd /c scripts\build_isolated_host.bat scripts\flow_interop_host.cpp tmp\flow-interop-20260914
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/flow_interop_probe.py --work tmp/flow-interop-20260914 --source 'F:/project/test/dlss5/测试素材/9月1日.mp4' --start-frame 80 --frames 24 --edge 512 --rounds 4 --label fresh-square
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/flow_interop_probe.py --work tmp/flow-interop-20260914 --source 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4' --start-frame 80 --frames 24 --edge 720 --rounds 4 --label fresh-portrait
```

不同报告 label 不覆盖旧结果。保存的 DLL hash 区分历史候选；本轮不保留历史 DLL 副本。
脚本读已有媒体，不落盘逐帧大图或大视频。主机只支持此探针的固定尺寸、无深度、非分块模式。
46 项现有引导/宿主测试运行：45 通过、1 跳过；这不是 GPU 直连通过正式回归的声明。

数值差异与关闭方式的后续定位见文首最终结果；跨进程、显卡切换、缓存、可视化和长视频尚未验证。
NVOFA / HDR / 多卡 / GPU 直通编码本轮未验证。

## 参考

- [NVIDIA CUDA/D3D12 外部资源互操作](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/graphics-interop.html)
- [NVIDIA 外部资源 Driver API](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__EXTRES__INTEROP.html)
- [OpenCV resize 实现](https://github.com/opencv/opencv/blob/4.x/modules/imgproc/src/resize.cpp)

## 仓库卫生

开始时 F 盘可用 48.94 GiB、tmp 28.54 GiB。没有下载/复制依赖或模型。
本任务文件峰值不足 1 MiB；小型证据归档本目录。已核实无链接、无运行中的本任务进程且文件
可独占打开，删除本任务的 35 个编译/重复报告/日志文件，逻辑体积 983,879 B；
清理前后 F 盘可用空间增加 1,052,672 B（同时可能有其他任务写盘，不能把盘总变化都归属本任务）。
仅保留 TASK.md，约 1.5 KiB，2026-09-21 复核；其余 tmp 持久依赖与用户备份未动。
过程中其他任务占用变化使 F 盘余量降至约 24.75 GiB；本任务未产生该规模的数据。
