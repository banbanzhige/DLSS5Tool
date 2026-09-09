# 引导数据搬运优化 · 2026-09-08

## 范围

本轮只优化 DLSS 宿主与引导组件之间的 CPU 侧搬运，不修改
`nvngx_dlssnr.dll`、模型权重、FP32、引导长边或 RAFT 更新次数。
主程序与 DLSS 宿主原本已经使用共享内存。

- 新组件在协议 v1 握手时协商 `shared_memory_v1`；旧组件没有确认时继续使用原管道。
- 单个固定尺寸共享块：RGBA8 输入、float32 双通道光流、float32 深度。
- 输入只复制一次进入共享块；管道不再承载整帧及引导数组，仅传控制与完成消息。
- OpenCV 放大结果直接写共享输出，省去临时全尺寸输出数组及其复制；非活动引导和 reset 光流清零。
- 原生引擎借用输出视图，不再接收／反序列化／复制整张引导图。
- 每帧带递增序号，客户端收到匹配的完成确认后才读取；一次只允许一个引导请求在途。
- v2 的 enqueue 返回前，C++ 已把引导复制到对应原生上传槽，因此下一次引导覆写共享块不会改坏在途帧。
- `GuidanceSession.process()` 默认仍返回独立数组；只有内部明确指定 `copy_outputs=False` 的调用借用视图。
  借用视图有效期到下一次 process 或 close，不能保留后继续读取。
- 关闭时释放共享块；模型失败、进程退出、超时和错序均有测试。

仍保留 CPU 预处理、模型输入上传、模型结果 GPU→CPU 回读，以及原生 D3D12 上传。
**这不是 CUDA↔D3D12 全 GPU 零拷贝。**

## 原片 A/B

输入：用户原片 `9月1日.mp4`，1440×1440；同一批解码帧、引导长边720、
RAFT-Large 6次更新、Depth Anything V2 Large、FP32、RTX 4070 SUPER。
每轮先处理3帧预热，再计时24帧，含一次显式中途 reset。
两轮顺序为旧→新、新→旧；每种链路共48个计时样本。

| 项目（两轮平均） | 旧管道组件 | 新共享内存组件 |
| --- | ---: | ---: |
| 引导往返时间减去组件内部处理时间 | 37.75ms | 4.29ms |
| 整帧 process 调用（含引导、宿主 IPC、DLSS） | 473.41ms | 436.50ms |
| process 吞吐（总计时帧数／总处理时间） | 2.11fps | 2.29fps |

非推理往返开销下降约88.6%；整帧处理耗时下降约7.8%，吞吐提高约8.5%。
两轮单独吞吐提升约10.9%和5.9%，说明模型耗时／后台 GPU 状态仍会波动。
“非推理往返开销”包含输入复制、控制消息、调度、输出传输和客户端有限值检查，
不是纯内存总线耗时。

计时不含源视频解码、首次加载、哈希计算、GUI 显示、音频或编码；不等同于完整导出或 GUI 帧率。
DLSS 采用同步 process、host_in_flight=1；三槽异步正确性另测，不把其吞吐混入上述数字。
这是短样本，不承诺整个视频、其他显卡或后台负载下固定提升。

原始报告：`output/guidance-shm-real-ab/report.json`，含逐帧时间和 SHA-256。
报告中旧组件记录的是测试时的 `mods/enhancement`；部署后旧组件已移至下述备份目录。

## 一致性与验证

- 原片两轮 A/B 的每个计时帧 DLSS 输出哈希一致。
- 独立的原片引导 A/B（预热3帧＋计时4帧，含中途 reset）中，光流、深度、reset 合并哈希一致：
  `output/guidance-shm-raw-equality/report.json`。此短测试只用于原始引导一致性，不作为主要性能数字。
- CUDA 三种模式分别验证非活动引导为零、显式 reset 光流归零。
- 每种模式7帧，三槽异步与同步 DLSS 输出逐像素相同，包含在途帧期间的 reset：
  `output/guidance-shm-native-smoke/report.json`。
- 全量220项测试通过；共享内存／旧协议／错误回收无需 Torch 的测试在 `tests/test_guidance_transport.py`。
- 新基础冻结 EXE 加已部署 CUDA 组件，强制共享内存的混合模式原生诊断通过：
  `output/guidance-shm-frozen-result.json`。

## 本机部署与使用

新组件已部署到 `mods/enhancement`，原组件完整保留于
`tmp/guidance-pipe-backup-20260908`，外置 `mods/models`、保存参数和 DLL 未改。
保留原组件的 `LOCAL-TEST-ONLY.md`；仍是本地验证组件，不是完成许可审查的公开发行包。

重启开发版 `run.bat` 即使用新主程序代码和新组件；不需要改引导模式或精度。
旧版主程序搭配新组件仍走旧管道，因此只替换组件、继续使用旧 EXE 不会获得共享内存收益。
新基础冻结包在 `tmp/shm-base-20260908/DLSS5Tool-v2.0.1`，仍不内置组件／权重。

维护者复测（必须使用新的输出目录；默认比较同一个新组件的两种传输方式）：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q
.venv\Scripts\python.exe scripts\guidance_transport_benchmark.py --component mods\enhancement --source "原片路径.mp4" --output output\transport-new --frames 24 --rounds 2 --modes 3 --native
.venv\Scripts\python.exe scripts\guidance_transport_smoke.py --component mods\enhancement --output output\transport-smoke-new
```

注意：`--component` / `--legacy-component` 指向固定名称为 `enhancement` 的组件目录。
备份目录本身更名后不能直接作为 `--legacy-component` 使用；复测时将其完整复制到一个新目录下的
`enhancement` 子目录，再用 `--legacy-component` 传该路径（不要改原始备份）。
