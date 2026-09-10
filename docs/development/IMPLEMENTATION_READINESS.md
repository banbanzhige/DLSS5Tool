# 深度下线 / NVOFA 落地检查 · 2026-09-10

状态：已实施本地候选，记录见 [NVOFA 集成实测](../experiments/NVOFA_INTEGRATION.md)。本文保留开工前核对依据，不进入用户发行包。

用户确认后的边界优先于下方历史建议：NVOFA 以成为主力为目标，通过真实效率/连续画质后再切默认。
初始化失败可提示并回退 RAFT（需权重）；运行中失败停止，不中途换算法；不做无 Torch 小包，启动光流仍关闭。

## 判断与范围

两条路线合理，但不能直接照原清单开写：先完成 [v2.1.2](TODO_v2.1.2.md)，
再独立做 [NVOFA](TODO_NVOFA.md)。保持 RAFT 默认、深度内部代码保留、宿主和共享内存协议不改。
前一轮只补方案。后续已独立构建主程序和组件并测试，未覆盖现有组件、未提交或发布。

深度结论限定为“已测运行库、宿主和输入下，深度输入未改变输出”，足以支持暂时隐藏的产品决定，
不把逐像素相同进一步当作已证明运行库内部完全没有读取深度，更不外推到所有运行库。

## 开工基线

- 当前源码版本：`dlss5tool/app_version.py` 为 `2.1.1`。
- `.\.venv\Scripts\python.exe -B -m unittest discover -v`：363 项，355 通过、8 跳过，45.563 秒。
  跳过项是缺少 Torch 的深度加速、光流输入、缓存和分析尺寸测试；不能据此声称 GPU / 冻结组件通过。
- 工作区已有 README、指南、构建文档、图片和脚本改动；实施只选择本次文件，不 `git add .`，不清理未跟踪文件。
- 本机正在部署的组件与已验证发行组件不是同一份：

| 角色 | 路径 | SHA-256（2026-09-10 实读） |
| --- | --- | --- |
| 当前部署，不能冒充发行基线 | `mods/enhancement/guidance_worker.exe` | `0cc883164818c2f22df7f124790e2719ccda401eeba5028ee0fde2a3678a6a06` |
| v2.1.2 候选复用源，与打包锁定值一致 | `dist/v2.1.1-editions/DLSS5Tool-v2.1.1-win64-addon/mods/enhancement/guidance_worker.exe` | `ae3d29343f669f8d0741e8fe4673afe3bf49ace9135feacfa1f11e6d8d545732` |

哈希匹配仅核实 EXE 身份。打包前还要对照旧发行清单检查整个 `enhancement` 目录，不能拼接不同版本的 `_internal`。

## v2.1.2 必须补齐

### D1 · 折算必须贯穿实际请求，不能只改校验局部变量

证据：`guidance_client.validate()` 返回文件字典，不返回设置；`GuidanceSession` 仍复制原始 settings 发给 worker。
`dlss_engine` 也直接用 mode 设置宿主参数。`ExportJob.from_dict()` 只复制队列 settings，并不调用 `app_settings.validate()`。

实现约定：新增 Torch-free `normalize_public_settings(settings)`，返回副本，统一折算 mode；
在设置加载、宿主创建/更新、会话 contract / preflight / 发包和直接引导导出入口使用。
原生宿主、文件发现、握手、worker 请求必须看到同一个有效 mode。
不要靠修改调用者字典的副作用修复，也不要把 GUI 的“启动强制关闭”应用到队列执行。

验收：旧队列 mode 2 无组件可正常普通导出；mode 3 不要求 DAV2 且发送 mode 1；
调用者原始快照不变；源图、队列参数和输出不被重写。覆盖直接调用、预览、普通导出、队列重试。

### D2 · 环境变量读取时机与残留视图

模块级 `PUBLIC_DEPTH_GUIDANCE = ...` 在导入时锁定；之后在 `setUp` 改 env 不会生效。
采用调用时读取的 `depth_enabled()`，GUI 构建时决定可见性；维护者改环境变量后重启应用，不支持运行中热切换 UI。
测试用 `mock.patch.dict` 并注册 cleanup，避免污染其他用例和子进程。

除 `guidance_compare_target=depth → flow`，还需处理 `guidance_preview_view=depth → flow`。
排查推理页、对比菜单、独立窗口、组件详情和路径编辑器；
`gui._update_module_summary()` 当前硬编码按 mode 3 查模型，默认不得再报深度权重缺失。
隐藏控件仍保留变量/原值，避免收集设置时 KeyError 或覆盖维护者参数。
直接 `export_guidance(..., target='depth')` 默认明确拒绝，不悄悄导出一份标为深度的光流图。

### D3 · 打包不是改 MODELS 就结束

`package_editions.py` 整目录复制 component，又复制整个 licenses 目录。
只删 `MODELS` 条目无法阻止输入组件的 `models/`、`_internal/` 或旧说明夹带 DAV2 权重和旧用户说明。

在新 staging 中明确排除深度 checkpoint，并在完整包和附加包的最终 inventory 中递归断言
无 `depth_anything_v2_*.pth`，检查嵌套 fixture，而非仅检查常量。
保留实际携带的 DAV2 架构代码许可，区分“未附深度权重”和“无任何深度代码”。
同步组件 README、安装说明、模型 NOTICE、报告 `license_review` 和上传说明的过时文字。
不改源组件或删除用户已安装的权重；覆盖解压旧安装不会自动减小用户磁盘占用，建议新目录安装。

`package_editions.py` 当前两次从根目录取 `RELEASE_NOTES_{APP_VERSION}.md`，
必须改为 `docs/release/` 并增加源路径/存在性测试，否则归档末尾才失败。
轻量主程序需要重新构建，但 worker 不重编。保留更新器要求的 `DLSS5Tool-vX.Y.Z-win64.zip` 文件名。

### D4 · 两个验证脚本也要改

`scripts/verify_editions.py` 默认验证 1/2/3 并要求深度非零；
`scripts/guidance_activation_probe.py` 固定遍历 1/2/3/0。
新增公开模式验收及旧 mode 的折算断言；完整深度测试独立 opt-in，外部提供权重，不能要求发行包有 DAV2。
两脚本都加入对应无 GPU 测试。验证发行包时继续禁止回退到开发目录找到模型或组件。

## NVOFA 必须补齐

### N1 · 光流启用条件与输入接口（首要代码风险）

`Models._serial()`、缓存 key、共享输出清零和 metrics 都用 `self.flow is not None` 判断启用；
如果 NVOFA 分支仅设 `_nvof=None` 而不创建 RAFT，就会一路跳过推理并返回零光流。
采用独立 `has_flow`/后端状态，更新所有判断，不用 `self.flow=True` 的实验哨兵冒充模型。

`prepare_flow()` 是 RAFT 专用变换，依赖 `self.transforms`，产生归一化 NCHW Tensor。
NVOFA 必须在 `_flow_input()` 也分支，接收连续 `uint8 H×W×3 RGB` 数组；
backward 传 `(current, previous)`，forward_negated 传 `(previous, current)`，只在 `_finish_flow` 取负一次。
用 fake 引擎驱动真实 `Models.process` 测第二帧非零、reset 零值、缓存命中、seek/cut、尺寸变化及故障失效。

### N2 · 能力探测、尺寸与坐标合同

“对齐到 16”是本轮可选择的实现策略，不是全部 GPU 的支持证明。
初始化查询 `NvOFGetCaps` 的宽高上下限和 grid 支持；128×128 预检仍需实际设备成功。
ABI 函数表槽、结构尺寸/字段偏移、空函数指针增加无 DLL 测试，记录所用 API 2.0 头文件版本。

对齐策略明确为：先将分析尺寸向上对齐到 16，再从原 RGB resize 到实际 NVOFA 尺寸；
`prev`、缓存输入、metrics 和 `_finish_flow(w/fw,h/fh)` 必须统一使用这一尺寸。
不在 `_infer_flow()` 内悄悄 resize 后继续拿旧 fw/fh 缩放。
测试 136→144、横竖非等比例缩放、正负 XY 位移、边界和两个方向；网格插值不乘 4。

### N3 · 从探针升级为可安全退出的驱动模块

探针 retain primary context 却无 release，构造中途失败无统一清理；close 的某个 free 失败会跳过后续释放。
生产实现需要 retain/release 配对、push/pop 的 try/finally、半初始化清理、幂等 close、
清理失败不遮盖原始异常、worker shutdown/失败时调用 close，并用故障注入覆盖每个阶段。

不要硬编码 CUDA device 0 与 Torch 当前设备永远一致：从 Torch 当前逻辑设备解析一致的物理设备身份
（可核对 UUID/PCI bus ID），验证重映射环境；本轮不扩展成 GPU 选择 UI。
驱动 DLL 使用系统可信加载位置/搜索策略，不从用户 mods 或当前目录优先加载同名 DLL。
缺 DLL、无 OF、输入不支持、设备丢失均返回可读错误并失效会话，不静默回退。

### N4 · 设置、握手和诊断闭环

`app_settings.validate()` 目前没有 strict 参数。保持加载时宽松；另设共享后端校验，
GUI 激活、直接 API 和队列执行前严格检查，不能先宽松纠正拼写再宣称 strict 已拒绝。
设备/NVOFA 能力检查只在实际启用光流时触发；保存“关闭 + nvofa + cpu”不能妨碍普通增强。

不仅检查新 `flow_backend`，还核对实际 grid=4、quality=slow、temporal_hints=false；
旧组件缺字段只容许 RAFT，错误后端的就绪包必须拒绝。
更新 `GuidanceSession.info` 字段白名单、`_reply()` 错误白名单和 metrics；
NVOFA 不报告 RAFT 6 次迭代，Torch 显存统计也不冒充驱动原生分配总显存。
NVOFA 改迭代值不应重建会话；当前 contract 包含全部 ANALYSIS_KEYS，需同步处理，不能只改握手字典。

维护者 env 深度模式与 NVOFA 的交叉：本轮推荐明确拒绝 mode 3 + NVOFA；
mode 2 不运行光流，backend 只保存；mode 1 不激活双 Stream。
需要混合模式时另做线程/执行协议验证，避免旧 `raft_streams` 默认把新路径带入未验收并行执行。

GUI 后端控件纳入 `_update_host_control_states()`、busy 锁定、失败可编辑和重新预检；
NVOFA 的模型文件状态显示“不需要权重”，不能显示“缺 RAFT”。切换失败测试覆盖原先处于 RAFT 的场景。
运行时错误除关闭 worker 外，也要检查 UI 状态是否关闭；队列任务应失败留错，不生成冒充成功的输出。

### N5 · 构建、分发与回滚

`GuidanceWorker.spec` 写 manifest 后，`build_enhancement.py` 会再覆盖一次；两条生成路径必须一致。
除了声明 `flow_backends`，还要证明冻结 EXE 内确实包含 `nvofa.py`，并检查产物不含 `nvofapi64.dll`。
将改编头文件的完整许可放入独立组件 licenses；只改仓库 THIRD_PARTY_NOTICES 不覆盖附加包独立分发。
基础包继续不导入 Torch/NVOFA、不加载驱动 OF DLL。

`package_editions.WORKER_SHA` 仍锁旧组件；NVOFA 实测通过后，在下一版更新已批准组件身份和全目录清单，
不删除哈希校验、不提前改成任意新构建。回滚记录要包含整目录及配套 manifest，不只记录 EXE。

### N6 · 原验收命令不能作为生产验收

激活脚本目前没有 argparse，传 `--source` / `--output` 会被忽略，也不能选择 backend 或新组件目录；
先增加并测试 CLI：模式、backend、mods 目录、独立输出目录，输出 ready/第二帧 metrics/组件身份。

`realtime_routes_probe.py` 当前混合深度模式，`large512` 与 `small384_nvof` 同时改变深度型号和尺寸，
其默认方向还是 forward_negated，而应用默认 backward。历史数字是路线实验，不是仅光流后端 A/B。

新增生产路径对照入口（不能用 monkeypatch 冒充冻结 worker）：同一素材、mode 1、同一长边 512、方向、
宿主/DLL、缓存、导出参数，仅改变 RAFT/NVOFA。384 档另成一对；原历史脚本保留复现实验用途。
方形/竖屏整段、遮挡/细节/切镜、seek、反复切后端、取消、退出、无权重目录、失败重试都要覆盖。
同时看冷启动/首遍/热缓存、端到端 P50/P95、完整解码帧数和连续播放；记录 GPU/驱动、素材及 DLL/组件哈希。
无新增可见抖动/拖影是人工放行门槛，自动校验仅能证明可解码、有限值和状态合同。

## 实施顺序与放行门槛

| 阶段 | 内容 | 完成后才进入下一步 |
| --- | --- | --- |
| A1 | v2.1.2 公开配置边界 + 旧配置/队列/直调回归 | worker 实际请求只有 0/1；env 用例独立恢复 |
| A2 | 隐藏 UI / 视图 / 文件摘要 + 中英文档 | 默认无深度入口；env 可复开；不变更内部推理 |
| A3 | 打包入口、验证脚本、递归清单测试、版本/发行说明 | 新基础包 + 原已验证组件；无 DAV2 权重；worker SHA 不变 |
| B1 | NVOFA 纯函数/ABI/资源类，后端设置和握手 | fake 引擎贯穿 Models.process；故障清理和旧组件兼容通过 |
| B2 | UI、生产探针、独立冻结组件和许可 | 明确新目录，加载实际 NVOFA，切回 RAFT 无变化 |
| B3 | 同参数全片 A/B、打包/干净环境与回滚核对 | 人工连续播放放行后才决定下一版发行；默认仍 RAFT |

推荐现在从 A1 开始，不将 NVOFA 新键顺手放进 v2.1.2；每阶段独立提交范围，尚未执行 git 提交。
GPU/驱动兼容性、干净环境和人工画质验收是后续放行项，不阻塞纯 Python 边界实现。

## 官方资料核对

能力查询与设备初始化参考 [NVIDIA NVOFA 编程指南](https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvofa-programming-guide/index.html)。
API 2.0 的能力枚举、S10.5 和许可原文见 [NVIDIA 官方头文件](https://github.com/NVIDIA/NVIDIAOpticalFlowSDK/blob/master/nvOpticalFlowCommon.h)。
仅这些 ABI 声明的许可不能推导整个 SDK 或所有驱动 DLL 的再分发权。
