# 打包轻量化记录与索引

## v2.1.3 本地发行打包 — 2026-09-11

- 源码基点 `f46dd32`，仅更新版本资源、发布说明及打包入口；审查发现的三处问题按用户要求保留，已在 [Release notes](RELEASE_NOTES_v2.1.3.md) 注明使用建议。
- `scripts/build_release.ps1 -SkipInstall` 构建基础程序；422 项单测中 414 项通过、8 项跳过，发行内容隔离通过，EXE 文件／产品版本均为 2.1.3。基础 ZIP CRC 通过，SHA256 `b9d9414abda7c533a2b002931f9611fd73cc722e0b96ee73d8e6fc53c4a8a30e`，230,700,546 B；位于 `dist/DLSS5Tool-v2.1.3-win64.zip`。
- F 盘空间不足，经批准将三形态发行包放在 `D:/DLSS5Tool-Releases/v2.1.3/`。脚本新增显式 `--allow-external-output`，默认仍限制在项目内，并拒绝覆盖已有目录。
- 增强组件使用已验证的新 worker，SHA256 `2b309510ef6f73ae73dc98d11842a8ef3ba2e012c19a7e1cfe38def9a3b9f450`；不裁依赖，仅附原始 RAFT-Large 权重，不附深度权重，不复制用户设置和队列。

| 发行形态 | ZIP 字节数 | ZIP SHA256 |
| --- | ---: | --- |
| 轻量版／更新包 | 229,689,026 | `e26ee11022ef4607aa0de3275881587c4a20b067fe466c90d9bd68661ec5c522` |
| 推理附加包 | 3,053,841,718 | `ed6f9c12da1eab4e6a89ccccb5193440a483af46184234beba295f8a51149ea2` |
| 完整版 | 3,283,528,197 | `917ecef37141dac8a7a5c4bf41f187f3d28a109382a6bc44aa42d472f2fc0c1f` |

- 上传集合：`D:/DLSS5Tool-Releases/v2.1.3/github-assets/`，含标准更新文件名的轻量 ZIP、完整／附加包各两卷、校验文件、合并脚本及简版发布说明。不要同时上传重复的 `-lite.zip`；不要将超过 2 GiB 的未分卷 ZIP 用作 GitHub 附件。
- 三个 ZIP 逐文件 CRC、SHA256 清单和分卷重组验证通过；完整版逐文件等于轻量版叠加附加包。
- `verify_editions.py` 解压安装验证通过：冻结轻量 EXE 普通诊断成功、缺组件明确失败；冻结完整 EXE 与叠加安装输出一致；安装组件 RAFT 连续帧及旧深度模式映射通过。报告：`D:/DLSS5Tool-Releases/v2.1.3-verification/report.json`。
- 完整包 NVOFA 1×1 禁止回退的两帧检查通过（RTX 4070 SUPER），报告 `output/package-v2.1.3-nvofa/report.json`。打包专项单测 9 项通过。
- 本次未上传发布、未打 Git tag，未提交版本改动；本机验证不代表干净环境、跨显卡认证或第三方公开分发授权。

以下为历史记录。

2026-09-10 维护补充：按维护者指定，深度下线与 NVOFA 候选统一归入 v2.1.2 源码版本，
NVOFA 已写入默认 `mods/enhancement`，开发入口为根目录 `run.bat`。见 [集成实测](../experiments/NVOFA_INTEGRATION.md)、
[深度状态](../experiments/DEPTH_REFERENCE_STATUS.md)。尚未生成新的三种完整发行包/分卷清单，
下方体积与 SHA 仍是历史数据，不作本候选的发布数据。

历史记录日期：2026-09-09。当时源码版本：v2.1.1（按维护者指定编号）。下文 v2.2.0 为同日更早的实测基线，保留原始名称、体积与哈希；v2.1.1 三种发行形态见后文 E-01～E-03。

## 已确认原则

### 本地完整版：2026-09-10 / PKG-002 / v2.1.2

- 在上述基础包上使用当前已验证 NVOFA 网格 4/2/1 组件，worker SHA-256 `56d36aa26334d4caa8ac70b881717f17d71045e02933cba2dd38c6379928167b`，同步脚本旧哈希锁定与发行说明；未重建或裁剪组件。仅附原始 RAFT-Large 权重，递归排除 DAV2 权重。
- 输出目录 `dist/v2.1.2-editions/`；ZIP64 / Deflate level 6。完整版 ZIP 为 `DLSS5Tool-v2.1.2-win64-full.zip`：3,283,448,048 B（约 3.06 GiB），解压 5,247,862,043 B（约 4.89 GiB），3631 个文件；SHA-256 `4536c52998bac8224123e1362ec1a3005ccde5ad193e0d0f3473c03da06aed8d`。
- 配套附加包 ZIP 3,053,774,326 B，解压 4,781,518,435 B，2412 个文件；SHA-256 `a4439423525817319c79a180b56c622b9b7fad585d953ba687c5e0ff8a7c7797`。流程另生成含分发说明的轻量包与两卷式完整／附加包，均未上传。
- 逐文件大小／SHA 清单在 `verification/*-files.json`，完整校验报告在 `package-report.json`，归档校验值在 `SHA256SUMS.txt`。所有 ZIP CRC 校验通过，完整版与轻量＋附加包逐文件精确叠加一致。
- 本机 `scripts/verify_editions.py` 通过：冻结轻量版基础诊断成功、缺光流组件时明确失败；冻结完整版与叠加安装诊断输出一致，安装后的 RAFT 连续帧与旧深度模式映射检查通过。报告：`output/verify-v2.1.2-editions/report.json`。
- 另对完整版执行 NVOFA 1×1 禁止回退的两帧就绪检查，确认实际 `flow_backend=nvofa`、`flow_grid=1`、CUDA RTX 4070 SUPER；冻结完整版 NVOFA 单帧诊断通过。专项打包回归 9 项通过。本机验证不是干净环境、跨显卡认证或公开分发许可审核。

### 本地基础包复打：2026-09-10 / PKG-002 / v2.1.2

- 源码基点：`5e88370`；使用 `scripts/build_release.ps1 -SkipInstall -SkipTests`，复用 Python 3.13.3 / PyInstaller 6.22.2；构建前完整回归 409 项，401 项通过、8 项因可选依赖缺失跳过。
- 产物：`dist/DLSS5Tool-v2.1.2-win64.zip`；可运行目录：`dist/DLSS5Tool-v2.1.2/`。仅基础轻量包，文件清单按 `packaging/DLSS5Tool.spec` 与 `scripts/build_release.ps1` 收集，不含增强组件、Torch、模型、用户设置或实验素材；未实施依赖裁剪。
- ZIP 实测 230,687,409 B（约 220.00 MiB），解压逻辑体积 466,319,973 B（约 444.72 MiB），1232 个归档条目；压缩方式为 PowerShell `Compress-Archive -CompressionLevel Optimal`。
- SHA-256：`df7a28a8a2787dee5e4d308c516959370e827ea0bd9e05d54c3556d2a13db82d`。
- 验证：发行内容隔离、ZIP 逐文件 CRC、EXE 文件／产品版本 2.1.2 均通过；冻结程序 v2 后端在关闭光流的合成 640×360 单帧诊断通过，输出 SHA-256 为 `329A1278081892CFD4A09ED79152CEA0FBFC7B13571689D9169440A178FE1D93`。本地诊断记录在 `output/package-v2.1.2-check/`，不进入发行包。
- 当前默认参数已固化；轻量包缺少光流组件时仍经启动检查后关闭并提示。未构建完整包或附加包，未上传发布；本机基础诊断不代替干净环境、完整 GUI 导出及跨显卡验收。

**开发环境可以保留全组件；面向用户的发行包必须按实际功能需要尽量轻量、精简。**
开发便利不等于发行依赖，不能把开发环境或整个开发机 `mods` 直接作为正式发行清单。
轻量化不能以静默降低画质、减少既有功能、破坏兼容性或要求用户额外安装 Python、
PyTorch、CUDA Toolkit 为代价。显卡驱动及已声明的系统运行库要求仍单独说明。

本文件是后续打包工作的维护入口。开发环境保留全组件，基础包和完整模型包分开管理。
2026-09-09 已按用户决定放弃推理运行库裁剪版本，相关实现和实验产物已回滚；
原始组件、模型、基础包和原完整模型包保留。

## 环境与发行边界

| 对象 | 允许或需要保留 | 发行约束 |
| --- | --- | --- |
| 开发环境 | 完整 PyTorch/CUDA、模型源码、多个模型、调试和性能分析工具、测试环境 | 可以全组件；不为缩小发行包而删除开发依赖、模型或备份 |
| 基础便携包（默认下载） | GUI、DLSS/VSR、媒体处理所需运行文件与用户说明、许可 | 不带 Torch、增强工作程序、深度/光流模型；引导关闭时独立可用 |
| 可选增强组件 | 经验证的推理程序、必要依赖、协议描述、许可 | 与基础包分开构建；目标是最小可用推理依赖集，不照搬开发环境；不要求用户安装 Python/PyTorch/CUDA Toolkit |
| 可选模型包 | 用户明确选择的兼容权重与来源/许可说明 | 与运行组件分开统计；不默认捆绑所有型号；更换小模型或精度需作为显式方案验证 |
| 完整组合包 | 基础包＋已验证增强组件＋明确列出的模型 | 作为可选组合，不取代轻量默认包；公布压缩和解压体积；许可审查完成前仅供本地测试 |
| 开发与用户数据 | 源码、SDK、实验报告、测试媒体、旧组件、设置、队列、缓存等 | 不混入发行包；必要运行资产和用户文档按明确清单收集 |

## v2.2.0 体积基线

记录口径：2026-09-09 本机实际构建，ZIP 使用 Deflate level 6；MiB = 2^20 字节，
GiB = 2^30 字节。解压体积是文件逻辑长度合计，不是文件系统“占用空间”。
后续比较需记录相同口径、依赖版本、组件及模型构成，不把估算写成实测。

| 编号 | 产物/组成 | ZIP 实测 | 解压实测 | 说明 |
| --- | --- | --- | --- | --- |
| B-01 | v2.2.0 基础包，不带增强组件/模型 | 229,652,010 B（219.01 MiB） | 466,311,878 B（444.71 MiB） | 默认轻量发行形态 |
| B-02 | v2.2.0 完整模型本地测试包 | 4,528,719,516 B（4.22 GiB） | 6,588,262,771 B（6.14 GiB） | 基础包＋现有 CUDA 组件＋RAFT-Large＋DAV2-Large＋本地测试说明 |
| B-03 | 现有 CUDA 增强组件，不含权重 | 未单独压缩测量 | 4,759,447,748 B（4.43 GiB） | 复用现有冻结组件；Torch 2.8.0+cu128、CUDA 12.8 |
| B-04 | Depth Anything V2 Large 权重 | 未单独压缩测量 | 1,341,395,338 B（约 1.25 GiB） | `depth_anything_v2_vitl.pth` |
| B-05 | RAFT-Large 权重 | 未单独压缩测量 | 21,106,607 B（约 20.13 MiB） | `raft_large_C_T_SKHT_V2-ff5fadd5.pth` |

B-03 中 `torch` 目录约 4316.85 MiB，约占组件的 95%；OpenCV 约 111.76 MiB，
增强 EXE 约 31.98 MiB。主要体积来自通用运行库，不是业务代码或权重。
已检查 `torch_cuda.dll` 的直接导入：包括 cuSPARSE、cuFFT、cuSOLVER、cuDNN、
cuBLAS/cuBLASLt 和 `torch_cpu.dll`。不能仅因业务代码没有显式调用某库就删除它；
还需核对延迟加载、动态加载和不同推理配置下的依赖。

产物校验索引（产物本身不提交 Git）：

| 文件 | SHA-256 |
| --- | --- |
| `DLSS5Tool-v2.2.0-win64-no-models.zip` | `170fdab302d4bd0e21d70f770bce0211b51ee605c3154c4592052e0e963eb6a7` |
| `DLSS5Tool-v2.2.0-win64-full-models-local-test.zip` | `6ddd326215a68ba707635d8cc569af7a31a966ca479cb821c1f83ce69b21aa2a` |

本次验证：335 项单元测试中 329 项通过、6 项跳过；两个 ZIP 的逐文件 CRC 校验通过；
基础包诊断、缺组件提示、冻结主程序调用完整组件以及 GPU 双模型连续帧推理通过。
GPU 验证设备为 RTX 4070 SUPER，不代表其他显卡和驱动已通过。
现有完整组件的许可归档尚未补齐，完整包仍是本地测试产物，未公开发布。

## 轻量化任务索引

状态约定：已记录 / 待验证 / 实施中 / 已验证 / 不采用。只有证据与验收齐全才标为已验证。

| 编号 | 优先级 | 工作项 | 状态 | 验证或完成依据 |
| --- | --- | --- | --- | --- |
| PKG-001 | P0 | 开发全组件与发行最小依赖分离 | 已记录 | 本文边界；后续修改构建流程按此执行 |
| PKG-002 | P0 | 建立 v2.2.0 体积和功能基线 | 已验证 | B-01～B-05、归档哈希及上述本机验证；不代表轻量化完成 |
| PKG-003 | P0 | 审计增强组件收集清单和 DLL 依赖 | 不采用 | 本轮裁剪收益不足，用户决定放弃该版本；不继续推进 DLL 裁剪 |
| PKG-004 | P0 | 保持现有模型/精度/功能的保守裁剪 | 不采用 | 仅节省约 331 MiB 运行库，用户决定回滚；完整模型包保留原推理组件 |
| PKG-005 | P0 | 将发行文件清单、体积报告和回归接入构建 | 待验证 | 本轮精简构建入口已撤回；后续如需通用发行体积记录另行实施，不自动重启裁剪 |
| PKG-006 | P1 | 评估专用推理后端及模型转换 | 待验证 | 仅在保守裁剪不足时评估 ONNX Runtime 等；核查深度/光流、可调迭代和输入尺寸、设备行为、速度及数值一致性，不承诺目标体积 |
| PKG-007 | P1 | 评估权重轻量化选项 | 待验证 | 小模型、FP16 或量化作为独立显式选项；另测画质/时序/性能，不冒充运行库裁剪、不静默替换默认模型 |
| PKG-008 | P0 | 增强组件及权重的发行许可归档 | 待验证 | v2.1.1 完整版／附加包已归档上游许可原文、依赖 NOTICE 和模型来源；Large 为 CC-BY-NC-4.0。本机打包通过不等于公开分发授权完成 |

PKG-003/004 已放弃，不再按原裁剪路线推进；PKG-008 仍是公开分发前置条件。
PKG-006、PKG-007 属于另行评估的改造，不是本轮记录已授权或已实施的变更。

## 裁剪版验收与后续记录

- 在新输出目录构建；保留开发环境和未裁剪基线，不覆盖原模型、组件及已有产物。
- 基础包继续通过发行隔离检查；不因增强依赖进入开发环境而被隐式收集。
- 验证启动、诊断、DLSS/VSR、预览和图片/视频导出等本次裁剪可能影响的功能。
- 增强包验证仅光流、仅深度、混合、连续帧/重置，以及承诺支持的精度、迭代、尺寸、缓存和执行模式。
- 在没有 Python/PyTorch/CUDA Toolkit 的干净环境验证；本机仅清理 PATH 的测试不能替代干净环境和支持范围内的显卡/驱动测试。
- 比较同素材同设置下的输出、时序、速度和显存；涉及数值变化先明确允许误差，不能仅以“启动成功”验收。
- 保留缺组件、错权重、不支持设备及显存不足时的明确错误；不静默降精度、换模型或退回 CPU。
- 完成归档 CRC、SHA-256、许可检查；同时报告下载和解压体积。更换压缩格式只算下载优化，不算安装体积减少。
- 每次实验按任务编号追加：日期、源码版本、依赖/构建参数、删改清单、前后字节数、验证环境、结果与产物/报告位置。未通过的候选记录原因，不进入发行清单。

## 复打记录：2026-09-09 / PKG-002

当时按 v2.2.0 工作区源码执行 `scripts/build_release.ps1 -SkipInstall`，复用现有构建依赖，未实施
PKG-003/004 的依赖裁剪。产物为 `dist/DLSS5Tool-v2.2.0-win64.zip`，可运行目录为
`dist/DLSS5Tool-v2.2.0/`；不含模型、Torch、增强组件或本索引。

| 项目 | 本次实测 | 相对 B-01 |
| --- | --- | --- |
| ZIP | 230,674,849 B（219.99 MiB） | +1,022,839 B |
| 解压后 | 466,312,918 B（444.71 MiB） | +1,040 B |
| SHA-256 | `8a52bd38401787ddd6425b5176ab73879fa80de8a53155d158af970ca29e8b62` | 新产物校验值 |

本次 ZIP 使用构建脚本的 PowerShell `Compress-Archive -CompressionLevel Optimal`，
与 B-01 的 Python ZIP 压缩实现不同，压缩体积变化不能视为依赖裁剪结果。
验证：335 项测试中 329 项通过、6 项跳过；发行内容隔离及 ZIP 逐文件 CRC 通过；
冻结主程序的 v2 后端基础单帧诊断成功，输出哈希与上一基础包诊断一致。
这是基础包复打，不是增强组件裁剪版，也没有重打完整模型包。

## 复打记录：2026-09-09 / v2.1.1 三种发行形态

按用户决定放弃运行库裁剪后，从已验证的 v2.1.1 基础包、优化推理组件和原始 Large 权重生成三种包，不下载、不替换源码部署组件、不裁剪 Torch。完整版文件清单等于轻量版叠加附加包。ZIP 为 Python ZIP_DEFLATED level 6、ZIP64。

输入：基础包 `dist/DLSS5Tool-v2.1.1/`（隔离检查通过）；组件 `guidance_worker.exe` SHA-256 `ae3d29343f669f8d0741e8fe4673afe3bf49ace9135feacfa1f11e6d8d545732`；`raft_large_C_T_SKHT_V2-ff5fadd5.pth` / `depth_anything_v2_vitl.pth` 未改字节。

| 编号 | 产物 | ZIP 实测 | 解压实测 | 说明 |
| --- | --- | ---: | ---: | --- |
| E-01 | 轻量版 `DLSS5Tool-v2.1.1-win64-lite.zip` | 229,675,288 B（219.04 MiB） | 466,374,724 B（444.77 MiB） | 1220 个文件；GitHub 默认附件名 `DLSS5Tool-v2.1.1-win64.zip`，哈希相同 |
| E-02 | 附加包 `DLSS5Tool-v2.1.1-win64-addon.zip` | 4,299,307,579 B（4.00 GiB） | 6,122,992,988 B（5.70 GiB） | 2413 个文件，仅 `mods/`；分三卷约 1900 MiB |
| E-03 | 完整版 `DLSS5Tool-v2.1.1-win64-full.zip` | 4,528,978,230 B（4.22 GiB） | 6,589,358,904 B（6.14 GiB） | 3632 个文件；轻量＋附加包精确叠加 |

校验值（ZIP SHA-256）：

| 文件 | SHA-256 |
| --- | --- |
| `DLSS5Tool-v2.1.1-win64.zip` / `-lite.zip` | `23a8154307e46340ff6cbcaecf6c6e3fa09e87ee1609bfcb7429fa1566453f5d` |
| `DLSS5Tool-v2.1.1-win64-addon.zip` | `b1a6c340963e954ea9936255bbe11fcee80c7143823d64cd69068e2a12867392` |
| `DLSS5Tool-v2.1.1-win64-full.zip` | `c32c7d38390d30105ecb30a205cb3eae06c51d62b6ee7a4fc1fc4ad972e5f4ef` |

分卷与本机报告位于 `dist/v2.1.1-editions/`（产物不提交 Git）。2026-09-09 本机验证：轻量 ZIP＋附加包解压后文件清单与完整版逐文件一致；冻结轻量版在关闭推理时诊断通过、缺少组件时拒绝启用；完整版与「轻量＋附加包」诊断输出哈希均为 `E5664B85EDD2AA3AA809BADD70943228571EDAC7B73F940EB3B146A0BE654D17`；附加包在 RTX 4070 SUPER 上仅光流／仅深度／混合三种模式均能加载并产出预期非零图。这不是干净环境或全部显卡认证。公开上传前须复核 NVIDIA／CUDA／FFmpeg 条款及 Depth Anything V2 Large 的 CC-BY-NC-4.0。

## 相关文件索引

| 入口 | 用途 |
| --- | --- |
| [CONTRIBUTING.md](../../CONTRIBUTING.md) | 开发与提交约定 |
| [scripts/build_release.ps1](../../scripts/build_release.ps1)、[packaging/DLSS5Tool.spec](../../packaging/DLSS5Tool.spec) | 基础包构建与依赖收集 |
| [scripts/check_release_contents.py](../../scripts/check_release_contents.py)、[tests/test_release_packaging.py](../../tests/test_release_packaging.py) | 基础发行内容隔离及打包契约测试 |
| [scripts/package_editions.py](../../scripts/package_editions.py)、[scripts/verify_editions.py](../../scripts/verify_editions.py)、[scripts/Join-ReleaseArchive.ps1](../../scripts/Join-ReleaseArchive.ps1) | 轻量／完整／附加包、分卷、本机叠加验证与可选分卷合并 |
| [scripts/build_enhancement.py](../../scripts/build_enhancement.py)、[packaging/GuidanceWorker.spec](../../packaging/GuidanceWorker.spec) | 独立增强组件构建，保留完整推理依赖 |
| [mods/README.md](../../mods/README.md) | 用户组件布局、自动检测和模型替换说明 |
| [docs/guidance/ENHANCEMENT_PACK.md](../guidance/ENHANCEMENT_PACK.md)、[docs/guidance/GPU_GUIDANCE.md](../guidance/GPU_GUIDANCE.md) | 历史组件构建与 GPU 验证记录，注意文中日期与适用范围 |
| [THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md) | 第三方许可与分发边界 |

本索引是开发维护文档，不加入用户发行包；基础包现有根目录开发文档隔离检查应继续拒绝它。
