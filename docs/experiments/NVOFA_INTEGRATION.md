# NVOFA / 深度下线本地候选 · 2026-09-10

状态：NVOFA 已接入仓库默认组件 `mods/enhancement`。开发只从根目录 `run.bat` 启动；不再使用独立试用入口。
2026-09-10 按维护者指定，原 v2.1.3 候选与深度下线统一归入 v2.1.2 源码提交；尚未生成或上传新的发行包。

## 产品边界

- 现阶段该版本的 DLSS 深度参考失效，默认隐藏深度模式/参数/预览/导出/权重路径。
  旧 mode 2→0、3→1，实际宿主和 worker 请求一致；旧队列快照不改写，启动仍关闭。
- 保留内部深度推理和上传，维护者 env 可复开。新包递归排除 DAV2 checkpoint，不删用户已安装文件。
- NVOFA 可选：新组件、不要求 RAFT 权重、SLOW/grid4/hints off，分析尺寸16对齐，RGB uint8 输入。
  RAFT 默认和512长边/6次/FP32保留；无权重路径仍要完整含 Torch 的组件。
- 初始化失败提示并尝试 RAFT（需权重）；运行中失败停止，不在视频中途换算法，不自动切 CPU。
  strict A/B 设置 guidance_flow_fallback=false，不能用成功回退冒充 NVOFA 成功。
- NVOFA 以成为主力为目标，真实效率和连续画质放行后再决定默认；不把启动光流变成自动开启。

## 本地产物

- 开发入口：仓库根目录 `run.bat`（与 `python gui.py` / `python -m dlss5tool` 同一套界面）。
- 当前组件：`mods/enhancement/`；worker SHA-256：
  `56d36aa26334d4caa8ac70b881717f17d71045e02933cba2dd38c6379928167b`（NVOFA 网格 4/2/1）。
  上一版 4×4 专用备份：`mods/enhancement-backup-nvofa-grid4-20260910/`，SHA-256：
  `d644aac97272fe0ba4fac530adf04edcc0e7e4198abd25cc9bf006d6b32e4618`。
- 设置和队列走仓库 `var/`，不再使用独立试用目录。
- 默认仍 RAFT。在“推理模型”选择仅光流，再选择 NVOFA。
- RAFT 对照权重读取 `mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth`，不修改字节。
- 接入前的 RAFT-only worker 备份在 `mods/enhancement-backup-raft-only-20260910/`，SHA-256：
  `0cc883164818c2f22df7f124790e2719ccda401eeba5028ee0fde2a3678a6a06`。

## 验证

- 基础环境全量测试：377 项，369 通过、8 项因无 Torch 跳过。
- Torch 构建环境专项：32 项全部通过，包括深度注意力、原光流输入、缓存/参数/执行和新 NVOFA tests。
- NVOFA 纯单测：S10.5、网格、ABI布局、strict设备、旧握手、失败回退/no fallback、
  真 Models.process 下帧对方向/reset/cache/resize/故障、半初始化资源释放和幂等 close。
- 真实 GUI 启用：两帧 NVOFA 通过，主线程采样最大间隙116ms；随后关闭成功。
  `output/nvofa-gui-activation-20260910-r1/report.json`。
- 冻结主程序 + 冻结 NVOFA 组件诊断成功：
  `output/nvofa-preview-settings-20260910/diagnostic-result.json`，输出640×360，v2实际宿主。
- 主程序发行隔离检查通过；新组件扫描未含 DAV2 checkpoint 或 nvofapi64.dll。
- 旧已验证发行 worker 与新组件 RAFT：竖屏243帧编码前 SHA 按顺序全部一致。
  `output/nvofa-raft-regression-20260910-r1/report.json` 对照下表新组件 RAFT。

## 实机速度

RTX 4070 SUPER，驱动616.64，非独占 GPU。两路同 mode1、长边512、backward、缓存0、原v2宿主与同一DLL/参数。
运行库 SHA-256：`ceb6432f6fbdf44d886014bcd47241932bf8b67439feef9bbdd0961436662650`；
宿主 SHA-256：`7046b5402ea28edfb7f4d1f771bcfa5edf20b1ec21ffc0a6d81f2104b8fd3931`。
通过真实 ProcessLive→GuidanceSession→冻结 worker，不 monkeypatch、不带深度。
下面 wall 包含解码、首帧组件加载、处理、逐帧哈希和编码，不是纯光流内核、GUI实时播放或音频导出帧率。
P50/P95 是 ProcessLive.process 调用耗时。单次顺序测量存在系统背景/温度波动，不保证固定收益。

| 素材 | 帧数 | RAFT wall | NVOFA wall | wall缩短 | RAFT / NVOFA process P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 竖屏1088×1920 | 243 | 27.34s | 17.29s | 36.8% | 91.40 / 55.72ms |
| 方形1440×1440 | 165 | 24.98s | 13.70s | 45.1% | 108.45 / 60.64ms |

两组四份输出均完整可解码，序列后显式seek/reset调用成功。
单独512×384合成平移探针：mean0.719ms、P950.749ms、正向(8,4)、反向(-8,-4)、ROI EPE0.001064px，有限值。
这是合成光流步骤，不是复杂遮挡精度证明。

原报告及可播放视频：

- [竖屏报告](../../output/nvofa-portrait-production-20260910-r1/report.json)、[RAFT](../../output/nvofa-portrait-production-20260910-r1/raft.mp4)、[NVOFA](../../output/nvofa-portrait-production-20260910-r1/nvofa.mp4)
- [方形报告](../../output/nvofa-square-production-20260910-r1/report.json)、[RAFT](../../output/nvofa-square-production-20260910-r1/raft.mp4)、[NVOFA](../../output/nvofa-square-production-20260910-r1/nvofa.mp4)
- [独立驱动探针](../../output/nvofa-production-abi-20260910.json)

复现使用全新输出目录：

```powershell
.venv\Scripts\python.exe -B scripts/nvofa_integration_probe.py --mods mods --output output/nvofa-new --source "<原片>" --backends raft nvofa --flow-weights mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth
.venv\Scripts\python.exe -B scripts/guidance_activation_probe.py --mods mods --backend nvofa --output output/nvofa-gui-new
```

## 放行前仍需做（未完成）

- [ ] 人工连续播放两组对照，重点看遮挡、细碎运动、切镜、闪烁与拖影；不能由可解码/单帧SHA替代。
- [ ] 实际交互预览/导出按钮/队列取消及重复切换的长时间体验，干净系统及不支持 OF 的机器验证。
- [ ] 决定是否切为主力默认。当前只证明本机效率有收益，不声称画质等价或稳定30fps。
- [ ] 正式完整/附加包清单、CRC、体积/哈希、完整依赖和上游分发复核；尚未生成ZIP或分卷。
- [ ] 通过后更新 package_editions.WORKER_SHA 到批准的新组件（目前仍锁旧已验证值，防止提前发行）。

不做：无 Torch 小包、temporal hints、零拷贝、宿主改造、双缓冲、NVOFA+深度混合。
UI沿用现有控件与主题，按UI/UX错误恢复原则提供回退原因、迭代禁用和失败后可编辑配置，不重做视觉系统。
