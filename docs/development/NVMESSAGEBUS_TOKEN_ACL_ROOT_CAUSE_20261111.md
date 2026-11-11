# 原生内存增长深查：令牌权限触发 MessageBus 重连风暴 · 2026-11-11

> 后续按维护者要求，已将最小权限干预整理为限定形态、启动门保护及写后核对的产品兼容处理。
> 见[源头兼容补丁与验收](NVIDIA_TOKEN_COMPAT_FIX_20261111.md)。本文保留发现过程及当时范围，
> 不把原始诊断 helper 直接作为产品实现，也不回改历史原始记录。

## 结论与对先前判断的修正

**本轮定位了当前诊断环境里快速增长的触发条件：沙箱子进程主令牌的 DACL 与 NVIDIA
消息组件的查询权限要求不兼容，触发反复重连及短线程创建；驱动已有的每线程约 184 bytes
保留再将它放大成每分钟几十 MiB 的原生堆增长。**

最强证据不是不同账户之间的比较，而是保持同一个沙箱账户、受限令牌、驱动、二进制及负载，
只对本次诊断子进程独有的主令牌对象临时补充 LocalSystem `READ_CONTROL`，线程风暴即停止。
只补进程查询权限或令牌 `TOKEN_QUERY` 均无效。对象 DACL 随后恢复，前后哈希一致。

普通账户中，相同 GPU／621.14 驱动的 NR、VSR 各完成五分钟首帧后空闲和三十分钟连续处理，
四项均通过既定私有提交增长门槛。因此，**先前把本轮沙箱中的快速增长直接用于描述普通账户
长期使用风险，范围过宽，现予修正。** 184-byte 驱动缺陷仍能在普通账户独立复现；
这与“普通运行一定快速耗尽内存”是不同结论。

本轮没有证明原用户 32 GiB 机器的完整长视频／批量处理／退出后整机异常已解决。
2026-10-20 的 617.42 历史测量没有记录令牌 DACL，不能倒推它们必然具有相同触发条件。
已有更新器生命周期、资源所有权和内存压力保护继续有效，中途续导按用户要求搁置。

## 1. 已观察到的链条与尚属推断的部分

```text
CUDA 初始化 → nvdiagclt64.dll → NvMessageBus.dll
  → \\.\pipe\NvMessageBusBroadcastNVIDIA.Display.Driver
  → 快速反复连接／断开，每轮约两个短线程
  → nvwgf2umx.dll 在线程初始化路径保留约 184 bytes／线程
  → 原生堆持续增长
```

- 客户端观察型 IAT 记录到成功的管道等待和打开，之后出现 `ReadFile` 109（管道已断开）及
  `WriteFile` 232（管道未连接），并再次连接。997 是异步请求挂起，**不计为失败**。
- `trace-factor-none` 约三十秒记录 95,840 次成功管道打开、191,680 次 `CreateThread` 调用，
  比例正好为二；独立线程计数器的观测范围稍有不同，不强求两种快照原子一致。
- `GetNamedPipeServerProcessId` 及独立零载荷探针确认该端点归属 PID 3748、session 0；
  服务查询对应 `NVDisplay.ContainerLocalSystem`，`sc.exe qc` 确认其账户为 LocalSystem。
  两种执行上下文都能打开管道，不能解释为管道不存在。
- 所查同族二进制有“获取连接客户端安全信息”的路径。离线反汇编中，NVIDIA App 的
  `NvMessageBusBroadcast.dll` 函数 RVA `0x65430` 请求 `OpenProcess(..., 0x1000, ...)`，
  随后 `OpenProcessToken(..., 0x20008, ...)`；驱动目录 `NvMessageBus.dll` 的 RVA `0x4c860`
  有同样的两项请求。具体指令、文件 SHA256、工具命令均已归档。

**权限边界的因果对照已成立；服务内部具体失败分支仍属于有证据支持的推断。**
没有注入系统服务或抓取其私有调用栈，客户端记录中没有服务侧 `OpenProcessToken` 的直接失败返回。
服务已加载模块的枚举不可用／返回空，不能据此断言相关模块没有加载，也不能把所查同族文件
直接等同于服务当时实际执行的每条指令。

Microsoft 文档说明 `OpenProcessToken` 将请求权限与**令牌对象自身**的 DACL 比较；
`TOKEN_READ` 包含标准读取权限及 `TOKEN_QUERY`，这里的额外位是 `READ_CONTROL (0x20000)`。
普通令牌信息查询通常只需 `TOKEN_QUERY`，`TokenSource` 例外。
[令牌访问权限](https://learn.microsoft.com/en-us/windows/win32/secauthz/access-rights-for-access-token-objects)
／[OpenProcessToken](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken)
／[GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)。

## 2. 同一沙箱内的权限对照

下面是**计数器观测总线程创建数**，不是首尾检查点之差，也不是当前存活线程数。
各轮配置三十秒空闲；检查点间隔约 29.3 秒。

| 诊断子进程临时允许 LocalSystem 的权限 | MessageBus 观测总数 | 检查点间新增数 | 结果 |
| --- | ---: | ---: | --- |
| 不补权限 | 191,774 | 174,425 | 风暴仍在 |
| 仅进程 `PROCESS_QUERY_LIMITED_INFORMATION (0x1000)` | 194,073 | 176,884 | 无效 |
| 仅主令牌 `TOKEN_READ (0x20008)` | 0 | 0 | 风暴停止 |
| 进程查询＋主令牌 `TOKEN_READ` | 0 | 0 | 风暴停止 |
| 仅主令牌 `TOKEN_QUERY (0x8)` | 217,870 | 200,226 | 无效 |
| 仅主令牌 `READ_CONTROL (0x20000)` | 0 | 0 | 风暴停止 |

前四项是使用完全相同源清单的四格对照，测量期间各轮源清单均不变，精确版本保存在
`measured-factorial/`。后两项进一步区分权限位，不用“换成普通账户”同时改变多个因素来作唯一归因。

所有授权变体均检查诊断子进程 TokenId 与监督器不同，拒绝修改共享令牌；
仅暂改子进程对象 DACL，未增加令牌复制、模拟或分配权限，未更改账户／组／特权内容。
所有成功变体均恢复进程和令牌 DACL，并验证原始／恢复 SHA256 一致。
归档中的 `account_or_token_changed=false` 表示身份／权限内容未换，**不表示没有临时更改令牌对象 DACL**。

该干预用于隔离原因，**不接入产品，不把放宽安全权限作为应用修复**。

## 3. 排除了“受限标志本身必然触发”

| 上下文 | 用户身份 | 受限令牌 | 完整性／会话 | 三十秒风暴 |
| --- | --- | --- | --- | --- |
| 普通入口 | PC 用户，SID 仅保存哈希 | 否 | medium／session 1 | 无 |
| 诊断沙箱 | SID 哈希与 PC 用户不同 | 是 | medium／session 1 | 有 |
| PC 用户派生的受限令牌，保留 13 个启用 SID | 与普通入口相同 | 是 | 同一桌面会话 | 无 |
| 返回原沙箱再测 | 原沙箱用户 | 是 | medium／session 1 | 再次出现 |

普通主令牌有 LocalSystem `0xf01ff` 的显式允许 ACE；沙箱快照没有 LocalSystem 显式 ACE，
Administrators 仅有 `TOKEN_QUERY`。ACE 清单不是完整的有效权限计算，不能只凭“没有显式 ACE”
断言所有主体都无法访问；本轮依赖的是上节保持其他条件一致的权限位干预。

同用户受限对照首次未保留完整启用／登录 SID，加载 `_ctypes` 即失败，未进入 CUDA，
该轮只作为失败记录保存。修正 SID 集合后的有效对照才用于上表。

## 4. 普通账户真实 NR／VSR 内存复测

RTX 4070 SUPER、64 GiB RAM、驱动 621.14、Python 3.13.3；复用现有运行库。
走现有生产 `ProcessLive`／`ProcessSuperResolution`，使用小尺寸合成图，无 `nvidia-smi`／PDH 轮询。
NR 实际处理 640×360；VSR 为 320×180 → 640×360。

每项只跟踪一个处理进程的 PID＋创建时间，排除控制进程及其他应用。处理进程按窗口末私有提交最大者选择，
并与工作进程内部检查点独立交叉核对；内部及外部采样不是原子快照，首帧导入／GC 差异只记录，
不伪造字节级一致性。NR 空闲只有一次内部检查点，**不能据此宣称整段线程计数不变**；
该段私有提交仍有 1,121 个有效外部采样。

算法复用 `scripts/compare_p0_stability.py::window_stats`：排除预热后，比较前后各五分之一采样的
中位数，再按对应中位时间间隔求斜率。空闲排除前十秒，连续处理主结果排除前三十秒。
既定门槛为增长率 ≤0.25 MiB／分钟，且分窗差绝对值 ≤16 MiB。

| 负载 | 配置窗口／帧数 | 起／终分窗中位数 | 分窗差 | MiB／分钟 | 门槛 |
| --- | --- | --- | ---: | ---: | --- |
| NR 首帧后空闲 | 300 秒，最终 32 帧 | 837.453／825.254 MiB | −12.199 MiB | −3.151 | 通过 |
| VSR 首帧后空闲 | 300 秒，最终 32 帧 | 397.898／392.262 MiB | −5.637 MiB | −1.458 | 通过 |
| NR 连续处理 | 1,800 秒，203,845 帧 | 684.090／684.914 MiB | **+0.824 MiB** | **+0.035** | 通过 |
| VSR 连续处理 | 1,800 秒，728,929 帧 | 399.523／401.316 MiB | **+1.793 MiB** | **+0.076** | 通过 |

两项长跑同时进行，**这些数据用于内存观察，不比较性能**。总历时分别 1,807.100、1,804.588 秒；
每项源／运行库清单前后相同，处理和自然退出通过，无安全停止、监督器兜底或任务进程残留。
排除十秒／六十秒的敏感性窗口亦保存，不靠选择唯一有利窗口获得通过。

长跑期间 NR／VSR 分别有 399／1,425 个内部检查点，MessageBus 检查点间新增均为零；
VSR 计数器总共观察到三个启动线程，NR 为零。Python tracemalloc 首尾差分别为 7,979／19,956 bytes。
五分钟分段私有提交中位数（MiB）：

- NR：684.090、684.410、684.410、684.914、684.914、684.914。
- VSR：399.492、399.492、399.977、399.977、399.977、401.316。

长跑由普通执行入口启动，相关上下文由该入口的独立普通账户控制核对；
逐工作进程令牌元数据功能是在长跑结束后才补入，**不冒充这些历史长跑已采集该字段**。
原测量的两份公共脚本保存在 `measured-common/`，哈希与测量清单对应。
原始 `result.json` 仍保留诊断器默认的 `long_term_memory_plateau_verified=false`，
上述范围内的数值门槛由独立汇总 `verification.json` 明确给出，不回改原始结果。

这不是 4K／8K、HDR、完整编解码、整夜挂机、重复批量任务、真实多卡或原用户机器的总验收。
P0 总项仍为部分完成；本机普通入口的上述四个窗口有明确通过证据。

## 5. 184-byte 缺陷确实独立存在

为避免“驱动缺陷也只是沙箱现象”的误读，在普通账户重新运行同一个裸线程程序，
仅加载系统 `version.dll` 或现装 `nvwgf2umx.dll`，逐个正常创建／退出 30,000 个空线程。
程序不调用 CUDA、NGX、D3D，不使用 `TerminateThread`。

| 普通账户条件 | 10,000→30,000 线程默认堆增量 | 平均新增量 |
| --- | ---: | ---: |
| 系统 DLL，无追踪 | 0 bytes | 0 bytes／线程 |
| 系统 DLL，有追踪 | 0 bytes | 0 bytes／线程 |
| NVIDIA 驱动，无追踪 | 3,680,000 bytes | **184 bytes／线程** |
| NVIDIA 驱动，有追踪 | 3,680,304 bytes | 184.0152 bytes／线程 |

四轮处理／自然退出及源码／目标 DLL 不变检查通过。微小追踪开销与无追踪测量分别记录。
上一轮当前驱动的分配栈和无丢失裸线程追踪见
[独立驱动复现](NATIVE_MEMORY_ROOT_CAUSE_20261111.md)，不因本轮修正触发范围而否定该证据。

外部 [ArtistDeveloper 最小复现](https://github.com/ArtistDeveloper/nvwgf2umx-thread-leak) 与
[NVIDIA 论坛原报告／6672628](https://forums.developer.nvidia.com/t/nvwgf2umx-dll-leaks-184-bytes-per-thread-in-its-dll-thread-attach-handler-595-95-580-88/381379)
可作供应商参考；本轮所查公开记录未见已确认的修复版本。
另有 [VS Code #237292](https://github.com/microsoft/vscode/issues/237292) 报告硬件加速时增长、
栈含 `NvMessageBus`。它仅是相关现象，不能用来证明该用户也命中相同令牌权限原因。

此前研究的 NeuralScreen 特征释放、Streamline D3D 设备引用、Visual Enhancer 解码反压各有其对应问题，
没有证据把它们当作本轮令牌边界与重连风暴的现成补丁。详细来源保留在
[前期源码研究](NATIVE_MEMORY_RESEARCH_20261111.md)。

## 6. 建议方案与本次落地

1. **纠正诊断上下文。** 后续性能／稳定性报告必须记录账户 SID 哈希、受限标志、完整性、会话、
   进程与主令牌 DACL 的可读元数据及查询错误；普通入口和沙箱分别标注。已新增只读的
   `scripts/windows_process_context.py`，并接入 `verify_p0_stability.py` 和原生内存检查点，
   同时记录监督器及工作进程 PID＋创建时间，不把父进程上下文直接当作所有工作进程上下文。
2. **产品保留现有保护。** 使用当前所有权、更新器退休、实际 RAM／Commit 余量保护和有限恢复。
   后续优先在普通入口回验实际长视频与重复批次，再决定是否仍需调整处理路径。
   本轮证据不足以支持为了沙箱风暴在产品里改 ACL、注入驱动或强制换后端。
3. **驱动根治交给可核验的供应商修复。** 本项目不能修补闭源驱动内部的线程分配。
   可提交裸线程复现与权限单变量材料，建议供应商审查消息连接认证的最小权限请求、错误降级、
   重试退避及短线程生命周期，并修复驱动线程退出的配对释放。本轮仅准备证据，未对外发送。
4. **用户若仍增长，按所有者继续定位。** 需要普通入口下处理进程私有提交、线程累计创建数、
   系统余量和停止／退出结果；若退出后整机资源仍增长，另外追查外部／内核所有者。
   不将本轮进程内堆证据自动解释为原用户的所有整机症状。

本次仅提交诊断脚本、证据和文档修正，无产品 ACL／驱动／服务／注册表／安装变更。
沙箱和普通入口各十秒纯空闲集成烟测通过，实际子进程身份与上下文匹配，源码清单不变；
四份 Python 文件 AST 检查及 `git diff --check` 通过。没有运行全量回归。

## 7. 证据、复核和失败记录

- [数值与因果对照汇总](nvmessagebus-cause-20261111/verification.json)
- [源码、上下文烟测及进程退休检查](nvmessagebus-cause-20261111/final-checks.json)
- [原始观察及精确测量源码](nvmessagebus-cause-20261111/observations.zip)
- [逐文件长度／SHA256 索引](nvmessagebus-cause-20261111/archive-index.json)
- [归档验证与空间清理记录](nvmessagebus-cause-20261111/archive-verification.json)

归档包括任务内 harness、构建／自检、所有成功／失败变体、公共脚本历史快照及本次最终源码；
只保留自编译诊断二进制，不复制已安装 NVIDIA 文件、运行库环境、模型或用户媒体。
重建时先阅读任务登记及 scope，恢复到登记的 `tmp/` 子目录；普通入口测量必须从普通用户入口运行，
仅修改诊断进程令牌 DACL 的变体不可转作产品功能。普通长跑命令示例（解释器及依赖沿用任务登记）：

```powershell
python -B scripts/verify_p0_stability.py --task tmp/<已登记任务> --case native-nr --label ordinary-long-nr --frames 32 --run-seconds 1800 --timeout 1950 --no-gpu-snapshots
python -B scripts/verify_p0_stability.py --task tmp/<已登记任务> --case native-sr --label ordinary-long-vsr --frames 32 --run-seconds 1800 --timeout 1950 --no-gpu-snapshots
```

其他保留的限制和失败：

- 第一轮 CUDA-only 追踪在 MessageBus 尚未加载时安装，仅覆盖十二个引用；不据此声称无管道调用。
  后续等待模块实际驻留，最终变体覆盖并恢复三十二个引用，无丢弃记录。
- 十二 API 观察器自检核对原始／追踪／恢复的返回值与 LastError、线程自然退出及引用恢复；通过。
  包装器有固定容量，不记录通信载荷、令牌内容或凭证，不修改返回值。
- 首轮受限 PC 令牌失败、早期 `_countof` 编译错误、试图覆盖仍被长跑加载的计数器导致链接失败，
  均保留；没有为构建而终止正在运行的长跑，后续复用已验证计数器。
- 沙箱内 CIM 访问被拒，服务身份以普通入口读查询及 `sc qc` 核对；系统 broker 内部没有观测覆盖。
- 汇总器首次误将 NR 唯一首帧检查点限定在 idle-start 之后而没有交叉匹配点，曾触发断言。
  已改为从全部相邻外部采样核对进程，记录采样时差及候选分离度；未放宽内存平台门槛。
- 公开 GitHub 固定提交的本轮网页重取遇到 cache miss；184-byte 独立结论依赖本机四组对照，
  外部状态仅引用本轮可读取的 NVIDIA 原报告，不以搜索无结果证明不存在其他补丁。

收尾前核对了 75 个已记录的 PID＋创建时间，均已退休。334 个文件、33,357,648 bytes
逐一校验后归档为 1,941,263-byte ZIP；源恢复核对也通过。仅清理本任务可恢复产物，
移除逻辑文件 33,354,638 bytes，F 盘清理前后可用空间增加 34,021,376 bytes（可能包含背景磁盘活动）。
任务目录剩 `TASK.md` 3,010 bytes，保留登记和恢复链接至 2026-11-18 复核。
精确记录见归档验证文件，未清理其他 `tmp/` 内容。
