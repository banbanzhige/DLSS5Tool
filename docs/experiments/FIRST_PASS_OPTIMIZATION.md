# 首次渲染优化：光流输入准备 · 2026-09-09

## 范围

固定用户参数：光流／深度长边512、RAFT-Large六次更新、Depth Anything V2 Large、
深度SDPA FP16、光流FP32、双Stream、反向光流、compatibility宿主提交。
不降低尺寸、不减迭代、不换模型、不丢帧。原始预测缓存禁用，每轮新会话，
所有帧重新推理（首帧／切镜按原规则清零光流）。不是缓存回放测速。

本轮仅改光流的CPU输入准备，深度forward、RAFT forward和原生宿主均未改。

## 改动

`dlss5tool/guidance_inputs.py`用原始Torch运算和torchvision变换生成每通道256项float32查表，
按uint8 RGB输入生成连续NCHW张量，保留原归一化结果的逐位数值，包括除法舍入。
不把 `(x-.5)/.5` 擅自改写成另一种浮点表达式。

连续帧对A→B、B→C之间只保留B的已准备CPU输入，节省重复转换；并不保存或复用光流预测。
worker每帧产生新的缩放RGB数组，按对象身份确定是否仍是对应的前帧。
跳转、切镜、缓存命中或尺寸变化导致前帧身份不同就重算；只保留一帧，不增长成整片缓存。
失败及关闭时释放输入和查表，GPU上传、stream及fence生命周期不变。
512×512下新增长期CPU输入约3MiB（另加前帧RGB引用与3KiB查表），不引入新权重或依赖。

## 源码诊断：两个完整视频

RTX4070 SUPER；Torch 2.8.0+cu128；Torch线程1、OpenCV线程20；非独占GPU。
表内为process调用的均值，排除前三帧用于观察稳定阶段；报告同时保留首帧和含加载总耗时。
此层将模型与DLSS置于同一个独立诊断进程，未包含生产IPC、编码和GUI，不冒充导出FPS。

| 输入 | 帧数 | 原始光流输入准备 | 新输入准备 | 原始process | 新process |
|---|---:|---:|---:|---:|---:|
| 方形1440×1440 | 165 | 16.54ms | 4.57ms | 152.13ms | 144.08ms |
| 竖屏1088×1920 | 243 | 10.67ms | 2.49ms | 127.15ms | 114.11ms |

两片的全部光流、深度及编码前DLSS输出SHA-256逐帧一致；不是抽帧或有损视频文件哈希。
方形基线复跑158.78ms，输出仍一致。存在背景负载波动，不能承诺固定百分比。
方形模型调用164次光流／165次深度；竖屏240次光流／243次深度，优化前后相同。

4线程试验将输入准备缩短至7.43ms，但process为162.62ms，未证明整链路提速，未采用。
该早期试验期间上一诊断进程在NGX清理中停留，也可能影响资源占用，不能当作严格线程消融。
之后独立诊断采用进程退出回收NGX，避免已有的Torch同进程shutdown挂起。

双Stream下flow_ms／depth_ms是重叠GPU事件区间，不能相加。
`_infer_*_wall_ms`包含CPU提交及可能的等待，`_finish_*_wall_ms`包含回读等待与后处理；
不是纯GPU kernel或纯CPU时长。默认源码诊断下深度GPU区间仍约53ms（方形），
本轮没有消除深度推理瓶颈，也没有达到30fps。

## 冻结组件与真实编码链路

同一方形165帧，冻结组件＋ProcessLive＋NVENC p5／高质量，每轮新进程，无预热回放，
含原视频解码和编码，无GUI／音频复用。编码前哈希逐帧一致，两个输出视频均实际解码165帧。

| 项目 | 已部署旧组件 | 独立新组件 |
|---|---:|---:|
| 稳定阶段process均值（排除前三帧） | 173.84ms | 149.76ms |
| 稳定阶段process P95 | 197.66ms | 166.38ms |
| 首帧process（含模型加载） | 10578.64ms | 10607.14ms |
| 含准备／解码／哈希／编码及编码收尾 | 44.98秒 | 41.38秒 |

本轮process均值约减少13.9%，含准备测试总时间约减少8.0%；单次A/B、非独占GPU，
不能承诺固定收益。深度GPU时间55.84→54.45ms，变化很小，不能宣称深度模型被本轮加速。
报告：`output/first-pass-frozen-baseline-r1-20260909/report.json`、
`output/first-pass-frozen-candidate-20260909/report.json`。

## 验证与状态

- 256个全部输入值、两种方向、非连续RGB视图逐位一致。
- 模拟worker覆盖三种模式、冷计算／缓存命中、seek／切镜、尺寸变化及失败／关闭清理。
- Torch环境输入／缓存／参数／执行／深度回归24项通过；新增worker状态覆盖另行通过。
- 基础环境全量348项：340通过、8项Torch专用测试跳过。
- 冻结候选三种引导模式均通过7帧同步／三槽异步输出一致、reset与未启用输入清零检查；
  使用深度SDPA FP16／混合双Stream，记录在`output/first-pass-frozen-smoke-20260909/report.json`。
- 源码已接入；独立冻结候选位于 `tmp/first-pass-input-20260909/mods/enhancement`。
- **未覆盖根目录正在使用的组件，未重打包公开发行版，也未接入硬件光流。**

旧组件EXE SHA256：`0CC883164818C2F22DF7F124790E2719CCDA401EEBA5028EE0FDE2A3678A6A06`。
候选EXE SHA256：`AE3D29343F669F8D0741E8FE4673AFE3BF49ACE9135FEACFA1F11E6D8D545732`。

所有记录在`output/first-pass-*-20260909/report.json`。
首次冻结基线尝试因测试脚本误用writer.close而失败，不计入结果；已修正为finish／abort。
源文件、用户设置、正式模型权重和旧组件未修改，未删除测试失败记录。

## 复现

`scripts/first_pass_probe.py`要求全新输出目录；读取用户参数并保存本轮快照。
参考对照要求源文件哈希、帧数和处理参数一致（允许组件位置不同）。

```powershell
tmp/guidance-cuda-env/Scripts/python.exe scripts/first_pass_probe.py --source "原片.mp4" --output output/first-pass-new-baseline --frames 165
tmp/guidance-cuda-env/Scripts/python.exe scripts/first_pass_probe.py --source "原片.mp4" --output output/first-pass-new-candidate --frames 165 --numpy-flow-input --reference output/first-pass-new-baseline/report.json
.venv/Scripts/python.exe scripts/first_pass_probe.py --backend frozen --source "原片.mp4" --output output/first-pass-frozen-new --frames 165 --encode
```

源码对照保留原算法在`scripts/flow_input_candidate.py`，即使生产源码已优化，
不加`--numpy-flow-input`仍明确测试原实现。冻结模式不允许用源码patch冒充已部署组件。
