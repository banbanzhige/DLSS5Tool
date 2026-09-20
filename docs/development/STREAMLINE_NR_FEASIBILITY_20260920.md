# Streamline NR 接入可行性核验（2026-09-20）

## 结案决定

**状态：已结案（2026-09-20，维护者决定停止投入），不实施 Streamline NR 后端。**
保留现有 NGX 直调后端。保存本报告、四轮日志和三个独立研究脚本作为历史证据；
不继续测试、不启动后台跟踪、不升级或替换生产 DLL。没有已证实的画质、速度或
显存收益，进一步兼容工作的成本目前不值得承担。重新开启需要维护者明确决定。

**实测结论：RTX 4070 SUPER / 616.92 未能启用 SL 2.13 NR。**
同目录修正后，原版模型的 NGX 支持查询明确返回 AdapterUnsupported；项目现用
社区模型在官方 NGX 加载路径出现签名及版本元数据错误，NR 仍被拒绝。
没有进行推理，不能评价画质/性能；也不能推出所有 40 系社区模型都无法通过专项
适配调用插件。签名/元数据错误已证实，但启动前 not implemented 的完整内部原因
尚未隔离。详见实测、后续源码调查及随附日志。

用户提供的 `dist/SL 2.13.rar` 补足了 NR 插件二进制。
其中 `sl.dlss_nr.dll`、`sl.common.dll`、`sl.interposer.dll` 的文件版本均为
2.13.0.0，Authenticode 检查均为 Valid、签名者 NVIDIA Corporation。
已完成隔离加载/支持查询，未进入 NR 求值。仍缺官方 NR 专用头文件和接口合同；
后续找到社区私有 ABI 研究，但未采纳或执行。公开 2.14.1 ZIP 缺件的结论仍成立，
不等于所有渠道都没有插件。

这不等于 NVIDIA 内部或合作伙伴版本不存在 NR 支持；本结论仅覆盖下述公开包、
源码标签、用户提供组件与本机实测。不构成性能、画质或跨显卡认证。
后续章节按调查阶段保留，阶段性的建议/暂停状态不是待执行任务，以本节结案决定为准。

## 已核实证据

### 1. 官方已声明框架支持

- `v2.14.1/changelog.txt` 的 `Release 2.14.0 Entries` 写明新增 `sl.dlss_nr`。
- 同标签 `include/sl_core_types.h` 定义 `kFeatureDLSS_NR = 1004`。
- 同一头文件有 uplift 输入、输出颜色和可选四通道控制蒙版资源类型。
- 编号和资源标签不足以定义可调用的 NR API；不能据此推断蒙版各通道语义、
  必需引导输入、HDR 色彩合同、支持显卡或性能收益。

来源：
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/changelog.txt
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/include/sl_core_types.h

### 2. 正式 x64 ZIP 的目录中没有 NR 交付文件

核验对象：
`https://github.com/NVIDIA-RTX/Streamline/releases/download/v2.14.1/streamline-sdk-v2.14.1.zip`

- GitHub API 报告大小：275,994,000 字节。
- 附件更新时间：2026-09-08T15:27:22Z。
- GitHub API 提供的整包摘要：
  `sha256:92c4d954631a1710da86ca3fa8d5034f2b9503838c95fc4ae977ae149319781b`。
  **未下载整包，因此未独立重算此摘要。**
- 用 HTTP Range 读取字节 `275731856-275993999`，服务器返回 206 和匹配的
  Content-Range；只读取 262,144 字节，在内存用 Python 标准库 `zipfile` 解析目录。
- ZIP 目录共 511 项；存在 SR、RR、FG 插件及对应 NGX DLL。
- 没有 `sl.dlss_nr.dll`、`nvngx_dlssnr.dll`、`sl_dlss_nr.h` 或 NR 专项接入指南。
- 同标签的 GitHub recursive tree 返回 `truncated=false`，也没有 NR 专项文件路径。

目录核验不等于对所有文件内容作完整性或安全认证，也没有验证 ARM 发行包。
这里没有证据支持“将现有 nvngx_dlssnr.dll 改名即可成为 Streamline 插件”；
两者处在不同接口层。

复核入口：
- https://api.github.com/repos/NVIDIA-RTX/Streamline/releases/tags/v2.14.1
- https://api.github.com/repos/NVIDIA-RTX/Streamline/git/trees/v2.14.1?recursive=1

### 3. 本机没有现成 NR 插件可供验证

- GPU：NVIDIA GeForce RTX 4070 SUPER；驱动：616.92。
- 只读枚举 `C:/ProgramData/NVIDIA/NGX`，共 200 个文件，访问错误 0。
- 找到 2.12.129.0、2.14.0.0 的 Streamline 组件，没有名称含 `dlss_nr` 或
  `dlssnr` 的文件。
- 未触发 OTA、修改驱动设置或加载缓存 DLL；未检查全盘其他应用的私有副本。

## 对本项目的适配判断

当前 `native/host_v2/dlssnr_host_v2.cpp` 使用自行创建的 D3D12 设备、NGX 参数及
NR 导出函数，并安装调用方兼容钩子。Python 的隔离宿主已经提供失败隔离和可替换运行库。
因此 Streamline 是候选后端工作，而不是一次原位 DLL 升级。

未来若取得完整、可信的 NR 插件与接口，建议先独立验证：

1. **支持与身份**：启动时验证签名、固定组件来源；查询目标 GPU/驱动支持，
   创建设备后再次确认功能初始化成功，并记录实际被选中的插件路径。
   实验先禁用自动 OTA 下载及可选缓存加载，不修改系统 OTA 设置。
2. **输入合同**：确认 NR 专用 Options、参数映射、颜色格式、曝光、光流/深度要求、
   reset 与分块语义；不能把现有 NGX 字符串参数机械改名。
3. **离屏生命周期**：普通 `slEvaluateFeature` 接受命令列表，但不证明 NR 支持
   无窗口批处理。官方文档要求每帧触发 `presentCommon()` 以完成内部回收；当前
   NR 宿主没有交换链/Present。必须取得可用的离屏帧结束方案并验证长视频显存稳定，
   不能假设手工调用内部函数或逐帧释放即可替代。
4. **隔离验证**：沿用独立进程边界，单独测试单张 SDR、连续视频、切镜、尺寸变化、
   SDR/HDR、退出与失败恢复，再与旧后端比较输出、耗时和峰值显存。
5. **上线门槛**：需要证明实际改善或维护收益；确认完整许可与交付条件后才评估
   打包，并继续保留旧后端回退。不能把社区 30/40 系兼容性当作官方支持承诺。

生命周期依据：
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/docs/ProgrammingGuide.md
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/docs/ProgrammingGuideManualHooking.md

## 初始阶段决策（历史记录，已由结案决定取代）

不实施候选后端，不下载整套 SDK，不测猜测的 ABI。原先的下一步前提是取得
官方 NR 插件、匹配的接口定义/示例及离屏使用说明；用户提供的 2.13 包已经补足
插件二进制，接口和离屏合同仍待确认。之后再进行最小验证，
而不是先修改 GUI、参数系统或现有 GPU 传输链路。

## 初始公开包检查的卫生与验证范围

- 开始时 F 盘可用约 135.87 GiB。
- `tmp/` 可读目录统计约 28.725 GiB，超过 20 GiB 预警；有既有目录访问受限，
  因此不是精确总量。最大项包括 guidance-cuda-env（7.16 GiB）及三项约 4.43 GiB
  的环境/备份；均未删除或改动。
- 本次未创建临时目录、未下载整包、未解压、未构建；ZIP 目录在内存核验。
- 本任务残留临时产物为 0；仅新增本报告。未修改程序代码、运行库或用户设置。

## 补充：用户提供 SL 2.13.rar 的静态核验

### 来源与边界

- 用户提供：`F:/project/DLSS5Tool/dist/SL 2.13.rar`，147,629,644 字节。
- 压缩包 SHA-256：`E827B3FB998A44BD8E3CFFAC8F5631D82E6CEC47F6B67780CC637EFD12DC588E`。
- 7-Zip 25.00 列目录：13 个文件和一个目录，RAR5、非固实、无分卷、未加密。
- 包含 ReShade 安装器与 RenoDX addon；没有头文件、源码、接入指南或许可证文件。
  压缩包发布来源尚未提供。个别 DLL 签名有效不等于整个组合包由 NVIDIA 发布，
  也不证明再分发许可或当前硬件兼容性。安装器和 addon 未执行、未核验签名。

### 关键组件身份

| 文件 | 字节数 | 实际版本 | SHA-256 |
| --- | ---: | --- | --- |
| sl.dlss_nr.dll | 401024 | 2.13.0.0 | 9F6672E5E0170DC118A3188D21BDA187E1FC1AA3502895B21AB846D23165C11D |
| sl.common.dll | 830592 | 2.13.0.0 | A4B2B5ACBE49FBC6D44DD432CAC19CD53218F698B2539DC7ED0FB268C72CFC8D |
| sl.interposer.dll | 651392 | 2.13.0.0 | 27B2190057994C0B287C2C5716953BF1586F6499AC12FBBB2092B9AAF8396570 |
| nvngx_dlssnr.dll | 165840496 | 本轮未读取版本资源 | E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E |

前三个文件的 Get-AuthenticodeSignature 均返回 Valid，签名者 NVIDIA Corporation，
产品字段 NVIDIA STREAMLINE PRODUCTION。最后一个模型文件仅通过 7-Zip stdout
流式哈希，没有落盘；哈希与项目记录的 50 系原版 310.8.0.0 完全一致，
不是新的 NR 模型，也不是当前 40 系默认 CEB643… 社区运行库。

### 插件静态检查发现

- dumpbin 导出表仅有 `DllMain`、`slGetPluginFunction`。字符串中出现
  `slDLSSNRSetOptions`，但它不是普通 PE 直接导出。不能据此猜测参数结构调用。
- 内嵌插件 JSON：id=1004，name=sl.dlss_nr，依赖 sl.common，
  rhi=[d3d12,vk]，hooks=[]。这是插件声明，不是两种 API 均已实测的证据。
- 内含 NGX 后端、命令列表求值、按 viewport 创建/销毁运行时、尺寸/preset/
  performanceMode 改变后重建的诊断字符串。
- 包含 DLSSNR.ControlMask、DLSSNR.GlobalToneStrength、UI/UIAlpha、
  BidirectionalDistortionField 等键。当前宿主尚未传入 ControlMask 和
  GlobalToneStrength；这些是值得核验的能力线索，不是已确认可用的新功能。
- 有输入、输出、motion、depth 缺失检查提示，不能假设这些引导资源可省略。
- 未反推 NR Options 二进制布局，未加载插件、调用未知 ABI 或做 GPU 推理。

### 更新后的建议

无需为了版本号继续寻找更“新”的包：2.13 已提供有价值的签名 NR 插件。
下一阶段可在用户批准继续实验后，用隔离进程和通用 Streamline API 检查功能加载、
插件实际路径和显卡支持，优先保持这组三个 2.13 组件成套，不能默认混配 2.14.1。
在 NR 专用 ABI、引导资源和帧结束合同未确认前，不进入正式推理后端实现。
如果后续比较原版模型与现有社区模型，要分别记录支持查询与实际求值，
不能通过关闭检查掩盖 GPU 不受支持。

### 临时占用

本次只选择解压三个小 DLL，共 1,883,008 字节；未复制模型、未安装软件。
使用 `tmp/streamline-nr-inspect-20260920/` 登记任务。核验完成后删除这三个
可从用户原始 RAR 重建的临时副本，仅保留 TASK.md；原 RAR 保持不变。

## 补充：README 源码构建范围复核

用户授权测试后，曾在准备阶段要求暂停，先核对源码是否能完成需要的功能。
当时探针尚未编译或执行，任务目录暂留约 160 MiB 准备材料；此后已恢复测试并
清理这些可重建材料，不再有这项待复核占用。

2026-09-20 核对 main 提交 `2122257e0fce486f91b385aa63b9a09b0a34b363`：

- 完整 README 确实写明除 DLSS-G 插件之外可从源码重建 SL；这是框架及其插件层，
  不是承诺提供 NVIDIA NGX 模型 DLL 的源码。
- 实际使用 `premake.lua`（README 仍写 premake5.lua）。完整脚本有 interposer、
  common、compute、SR、RR、NIS 等目标，没有 sl.dlss_nr 构建目标。
- 完整 recursive tree 未截断，source/plugins 没有 sl.dlss_nr，实现及 NR 专用
  头文件缺失；没有子模块可补足它。
- setup.bat 调用 Packman project.xml。该清单只有 premake、VulkanSDK、imgui、
  slang、PIX、implot、gtest，没有 NR 源码或插件下载依赖。
- 根 CMakeLists.txt 主要是导入/复制预编译组件的集成辅助，未提供 NR 构建目标。

因此源码可帮助调试框架的设备绑定、资源状态/回收、插件生命周期和 NGX 路径；
不能仅运行 setup/build 就生成缺失的 NR 插件或专用 Options ABI。当前可行路线
仍是隔离测试用户提供的签名 2.13 插件；必要时另外研究自编框架，但不能假定
公开 2.14.1 框架与 2.13 NR 插件的内部接口兼容。自行编写新的 Streamline NR
插件也是可能的工程方向，但属于重新封装现有 NGX 后端，不等同编译官方 NR，
也不会自动获得模型升级或消除现有兼容问题。

证据：
- https://github.com/NVIDIA-RTX/Streamline/blob/2122257e0fce486f91b385aa63b9a09b0a34b363/README.md
- https://github.com/NVIDIA-RTX/Streamline/blob/2122257e0fce486f91b385aa63b9a09b0a34b363/premake.lua
- https://github.com/NVIDIA-RTX/Streamline/blob/2122257e0fce486f91b385aa63b9a09b0a34b363/project.xml
- https://github.com/NVIDIA-RTX/Streamline/blob/2122257e0fce486f91b385aa63b9a09b0a34b363/CMakeLists.txt

## 实测：SL 2.13 NR 支持探针

用户随后授权继续测试。使用官方未修改的 v2.12.0 通用头文件（sl.h、sl_struct.h、
sl_consts.h、sl_version.h、sl_result.h、sl_appidentity.h、sl_device_wrappers.h、
sl_core_api.h、sl_core_types.h），只调用稳定通用 API；SDK 声明版本 2.12.0，
已被 2.13 的 slInit 接受。不伪造 NR Options 布局，不调用该未知接口。

### 测试与结果

| 运行 | 布置 | 支持查询（设备创建后） | NR 状态 | 退出 |
| --- | --- | --- | --- | --- |
| original-01 | 原版模型与插件分目录 | 32 / FeatureNotSupported | 未启用 | NGX shutdown 45 秒超时 |
| community-01 | 社区模型与插件分目录 | 32 / FeatureNotSupported | 未启用 | NGX shutdown 45 秒超时 |
| original-colocated-02 | 原版模型与插件同目录 | 6 / NoSupportedAdapterFound | 未启用 | shutdown=0，正常退出 |
| community-colocated-02 | 社区模型与插件同目录 | 32 / FeatureNotSupported | 未启用 | NGX shutdown 45 秒超时 |

所有运行：slInit=0，slSetD3DDevice=0；这仅表示框架/设备初始化成功，**不是 NR
可用**。slIsFeatureLoaded=31（FeatureMissing），loaded=false；函数发现
slDLSSNRSetOptions=31；未分配 NR 特征或提交图像。

探针使用的是任务目录的签名 2.13 interposer/common，记录到的 NGX 为当前驱动的
_nvngx.dll。未自动换用缓存中的 2.14 插件。三个 SL DLL 在每次启动前通过
WinVerifyTrust（仅使用本机证书缓存）校验；文件 SHA-256 保存在每轮 result.json。

首轮分目录结果不能单独用于判断显卡：公开 commonEntry.cpp 的支持查询仅把
pluginPath 传给 NGX FeatureDiscoveryInfo，而设备初始化阶段会使用完整路径列表。
因此先发现路径问题，再做了同目录复测。

同目录原版的关键日志：

```text
NGX_*_GetFeatureRequirements feature: 18 FeatureSupported == AdapterUnsupported
Ignoring plugin 'sl.dlss_nr' since it is not supported on this platform
```

同目录社区模型的关键日志：

```text
getNGXFeatureRequirements: ngxResult not implemented
nvLoadSignedLibraryW() failed ... 没有验证对象的数字签名。
NGXLoadMetaDataViaGetFileVersionInfo: swscanf_s() failed
error: unable to load DLL metadata via FileVersionInfo
```

不能把“直接 NGX 后端可运行的社区 DLL”推断成“官方 Streamline/NGX 安全加载也
接受它”。未关闭签名校验、伪造硬件支持或 patch 库。也未把模型签名失败与显卡
支持失败混为一谈。该结论只覆盖这台 4070 SUPER、616.92 和这两份模型；没有
测试 RTX 50 或 SF-v2，不能推广到所有驱动/显卡。

要求查询还返回 uplift 输入、输出、motion、depth 四种标签（70、71、1、0）。
这是未启用插件保留的要求信息，不证明实际求值可用，也不说明必须使用真实深度。

### OTA 与隔离发现

- 初始设置 flags=133，没有 eAllowOTA/eLoadDownloadedPlugins，原意是不触发更新。
  但 2.13 仍尝试调用 nvngx_update.exe bootstrap；NGX 本身也尝试模型 bootstrap。
- 查官方源码后确认：这些 flags 控制的是可选更新；不能作为完整离线保证。
  首两轮不能声称“完全没有 OTA 调用”。没有取得更新成功证据，也未保存缓存前后
  快照，因此不声称系统缓存完全未变化。
- 修正探针：在加载 DLL 前设置当前进程 ProcessChildProcessPolicy，禁止创建
  子进程；失败则终止测试。后两轮日志确认更新器创建失败。此限制仅作用于探针，
  未改系统注册表、全局驱动设置或安全校验。
- 第二轮起把子进程 TEMP/TMP 指向对应任务日志目录，避免 2.13 common 使用
  系统临时目录作为 NGX application data path。首轮路径仍为系统 TEMP。
- 每轮 Python subprocess 超时 45 秒，超时即终止并回收探针进程。没有进行图像
  求值，因此长视频回收、HDR、控制蒙版和性能均未测试。

### 项目决策

现阶段不替换当前 NR 后端。阻碍已经从“只有接口材料缺失”增加为本机实测的
硬件支持/社区模型官方加载兼容性问题。自编 common/interposer 可以提供诊断能力，
但不能自动修复原版模型硬件支持或获得缺失的 NR ABI。下一阶段若继续，需要
明确选择 RTX 50 验证或专项社区兼容研究，不能把本次结果包装成接入成功。

### 复现与证据

- `scripts/streamline_nr_probe.cpp`：通用 API 查询、签名检查、模块路径、禁止子进程；
  不调用 NR Options 或求值。
- `scripts/build_streamline_nr_probe.bat <官方通用头文件目录> <已存在构建目录>`。
- `scripts/run_streamline_nr_probe.py <exe> <plugins> <model-dir> <new-log-dir>`。
  实际同目录复测时 plugins 与 model-dir 指向同一任务目录；每次只放一种模型。
- 证据目录：`docs/development/streamline-nr-probe-20260920/`，保存四轮 console.log、
  sl.log、result.json。初始两轮使用尚未加禁止子进程策略的探针；exe 哈希不同，
  当前保留的源代码为修正后的版本。脚本处理超时时返回 124，正常执行完查询
  返回 0 不等于 NR 支持（必须检查 supported/loaded/find-options）。
- MSVC 14.44 编译通过；Python AST 语法检查与 git diff --check 通过。
- runtime/nvngx_dlssnr.dll 前后 SHA-256 均为 CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650。
  没有修改生产代码或现有运行库。任务模型副本/头文件/构建产物在证据归档后清理。

收尾：证据归档共 12 文件、414,497 字节，逐文件 SHA-256 与原日志一致。
已删除本任务可重建的 plugins/original/include/build/logs 子目录；F 盘可用空间
实测增加 168,607,744 字节（约 160.80 MiB），tmp 本任务仅剩小型 TASK.md。
原始 RAR 与项目现有运行库均保留；其他任务、持久环境和备份没有清理。

## 后续源码与模型静态调查（仅诊断，未新增 GPU 测试）

### 两条调用路径并不相同

本项目 `native/host_v2/dlssnr_host_v2.cpp` 自行 LoadLibrary/GetProcAddress 加载模型，
使用调用方兼容钩子后直接调用模型导出。Streamline 的 common 则先经驱动 NGX
进行 FeatureRequirements 查询，之后由驱动发现/加载模型。计算内核可以在
40 系运行，不保证后者的签名、元数据、架构声明和发现流程都能通过。

社区版日志先报告 sl.dlss_nr 已映射、adapter mask=0x1，随后又以平台不支持
排除它。公开 v2.14.1 pluginManager.cpp 中，最终 supported 还取决于插件的
feature.supported，而不是只看非零 adapter mask；这解释了两个日志不矛盾。
使用公开 2.14.1 源码辅助解释 2.13 行为，不声称掌握未公开的全部 2.13 NR 实现。

### 本地原版与社区版的静态比较

原版通过 7-Zip stdout 解压到内存，与现用 CEB643… DLL 比较，没有落盘或执行。
两者 NVSDK_NGX_GetGPUArchitecture 入口均为 `mov eax,0x1B0; ret`；本机日志中的
显卡架构是 0x190。官方 NVAPI 枚举分别对应 GB200 与 AD100。这只能证明架构
查询声明保留原样，不能证明社区版所有内部能力检查都没改。

PE 节比较：.rdata、.rsrc、.pdata、_RDATA、.reloc 完全一致；.text 有 6 字节差异，
.data 有大量差异。CreateFeature/GetFeatureRequirements 两个入口前 64 字节相同，
不代表整个函数/调用链完全相同，也不能将 .data 差异全当作模型权重改变。
现用社区文件 Authenticode 为 HashMismatch；版本资源仍能由 Windows 读取，
不代表 NGX 的特定元数据解析能够接受它。未尝试修复签名或修改架构返回值。

### 社区实现与私有接口线索

- Dagherbou/OptiScaler_DLSSNR 的默认 NR 实现走自己的转发层直调模型；其
  FORWARDER_INVESTIGATION.md 另记载驱动核心 proxy 路径的特征创建失败。
  因此“该项目能做 NR”不能等同“官方 sl.dlss_nr 插件能原样启用”。
- SamG-Coder/ChromiumRTX 存在 2.13 NR 私有 Options（72 字节、version 3）研究及
  进程内驱动映射实验。其公开验证主要是 RTX 5080 上普通 DLAA，不作为 40 系
  NR 成功的证据；其 NR 离屏回收也未完整验证。没有复制其代码或执行其补丁。
- kayle2203/dlssnr-signature-repair 实际用已验证的原版替换损坏/修改文件，
  并不是让 40 系修改版重新获得 NVIDIA 签名的工具，不适合作为当前问题的修复。

### 收益评估

同模型、同输入和参数下，换接入层不等于换模型，没有已证实画质或速度收益。
控制蒙版、全局色调是可探索参数线索，不是本次确认可用的功能。模型工作分辨率
及缩放后合成可以在现有后端研究，不依赖官方 SL NR；社区分支已有类似外部处理。
插件可能提供资源和参数管理便利，但会新增离屏生命周期、签名加载、私有 ABI
及版本组合的维护成本。结合维护者决定，本次到此结案，不继续上述探索。

来源：
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/source/core/sl.plugin-manager/pluginManager.cpp
- https://github.com/NVIDIA-RTX/Streamline/blob/v2.14.1/source/plugins/sl.common/commonEntry.cpp
- https://github.com/NVIDIA/nvapi/blob/main/nvapi.h
- https://github.com/Dagherbou/OptiScaler_DLSSNR/blob/973761621353b99bee3dc7d4bb27b117fef2644f/OptiScaler/dlssnr/FORWARDER_INVESTIGATION.md
- https://github.com/Dagherbou/OptiScaler_DLSSNR/blob/973761621353b99bee3dc7d4bb27b117fef2644f/OptiScaler/shaders/dlssnr/DlssNr_Dx12.cpp
- https://github.com/SamG-Coder/ChromiumRTX/tree/3db80cba7dde4a2e0337edf706f085d2d55c6d2a
- https://github.com/kayle2203/dlssnr-signature-repair
