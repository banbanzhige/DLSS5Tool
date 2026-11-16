# Velopack 验证与实验接入

日期：2026-11-16。状态：固定版本验证、可选更新通道与稳定数据目录适配完成；**实验接入，尚未切换正式发行**。
本轮没有推送、上传或修改任何已安装程序/正式发行包，现有 `previous-release` 与 lite/full 增量流程不变。

## 结论

Velopack 1.2.161 能驱动本项目的 Python 3.13/PyInstaller onedir 便携程序跨版本更新，
支持差分链与整包回退。已经通过原生 SDK、Update.exe 和本次封装层实际执行验证，非仅调用 mock。
没有安装到全局环境；现有 .NET 8 运行打包工具，SDK/工具仅解压在任务目录。

必须先分离程序和用户数据。原有“用户文件与真正 EXE 同目录”直接接入时，Velopack 替换 `current/`，
五类合成用户文件均消失；放在其父目录的同样五类文件全部保留。
这是本轮实际反向对照的结果，不是历史正式包发生了数据丢失。

采用明确标记的可选布局：

```text
DLSS5Tool/                    # 稳定便携根目录
  Update.exe                 # 原生更新引擎
  .portable
  dlss5_settings.json
  dlss5_queue.json
  mods/                      # 用户与 full 外置组件/模型
  packages/                  # 更新缓存
  .dlss5-managed/             # 可信目标清单副本与就绪证明
  current/                   # 框架管理的程序目录
    DLSS5Tool.exe
    sq.version
    _internal/
      portable-layout.json   # 显式启用标记、包 ID、形态与通道
```

`paths.app_root()` 继续指向真正 EXE 目录，内置运行库仍取 `_internal`；
新增 `portable_root()` 让设置、队列和 mods 使用稳定根目录。
普通源码/便携包没有启用标记时保持原有路径。标记存在但包身份/形态、路径或元数据不符时拒绝，
不能静默回落到 `current/` 新建一套设置。

## 已实现代码

- `portable_layout.py`：验证布局、包身份、lite/full 通道，拒绝链接/重解析点/硬链接及异常元数据。
- `managed_update.py`：可选 SDK 适配层。固定 Release、目标版本/形态、官方附件 SHA256、完整目标文件清单；
  SDK 重建缓存后逐文件验证，保存 ready 证明，安装前再次核对。普通便携包不导入或要求 SDK。
- `managed_update_ui.py`：异步准备下载、进度、后台隐藏、取消安装准备、稍后安装、失败提示和网页兜底。
  用户确认后只请求关窗；GPU/worker 清理完成后，在后台验证并调度原生更新，退出后安装/重启。
  启动时关闭 SDK 的隐式安装。下载完成不会自行中断导出或队列。
- ready 证明持久化；在固定且可信的同一 Release 元数据下，新 Session 可恢复并验证待装缓存。
  不声称无需网络重新核验的离线信任恢复已完成。
- full 使用独立通道，外置组件协议不兼容时明确拒绝，不降成 lite、不覆盖模型。
- `DLSS5Tool.spec` 只有同时提供 `DLSS5_MANAGED_LAYOUT` 与隔离 SDK 路径时才收集 SDK 及许可。
  标记须名为 `portable-layout.json`；普通构建继续使用现有更新器。
- `package_managed_core.py` 提供实验 core/manifest 生成器，不替代三形态发版与必需增量。
  它从显式 managed 冻结构建生成新目录，禁止把 mods 打进会替换的 `current/`。

当前沿用项目的官方 GitHub HTTPS/API 附件 SHA256 信任模型，清单本身也由 API 摘要固定。
尚未完成独立于 GitHub 仓库的发布签名/密钥生命周期；不能宣称已具备 Cockpit 的完整签名交付流程。

## 原生验证结果

固定 SDK 和打包工具均为 1.2.161，下载来源、大小和官方 SHA256 见
[sources-and-results.json](velopack-validation-20261116/sources-and-results.json)。Python wheel 的运行时
`__version__` 返回 `0.0.0`，因此记录这一现象，不用它识别 SDK 版本；以已核验资产及版本锁为准。

合成版本为 1.0.0、1.0.1、1.0.2，不复制真实 Torch/CUDA 或运行环境。

| 用例 | 结果 |
| --- | --- |
| lite/full，1.0.0 直接到 1.0.2，无本地基包缓存 | 整包安装成功 |
| lite/full，有本地基包、两段差分 | 两段补丁重建目标后一次安装成功 |
| feed 缺中间版本条目 | 本合成输入仍可正确重建目标；不扩大为所有缺段均能成功的证明 |
| full 使用 lite feed | 拒绝，仍为旧版，用户/组件文件保留 |
| 完整包损坏 | 拒绝，仍为旧版 |
| 损坏差分包，使用本次封装层 | SDK 报 SHA256 不符，完整包回退后逐文件核对通过，再安装目标 |
| 下载完成再启动旧程序 | 仍为旧版，没有隐式安装 |
| 退出后安装 | 原生更新结束后为目标版，五类用户标记均保留 |
| full 外置组件与模型 | 额外三份合成文件逐字节保留 |

共完成 7 个直接 SDK 场景、5 个本次封装层冻结场景。另有初始未适配布局的反向对照。
四个封装层成功场景和损坏差分回退场景均恢复 ready 证明后再调度原生安装。

差分重建会重新压缩 NUPKG，整个归档摘要/大小可能与直接整包不同。
本轮逐项核对 71 个包内文件，字节完全相同；对直接下载整包仍验证整体摘要。
不能仅因压缩容器不同判断升级失败，也不能仅以 ZIP 可打开判断成功。

原生更新写入版本标记时还可能未结束回调。探针修正为同时等待目标版本和原生更新锁释放，
再做独立启动检查；早期失败与修正证据均保留，没有把失败算通过。

## 定向源码检查

- 新通道、布局、GUI 更新契约、原有更新器/助手及国际化：**112 项、87 个子测试通过**。
- 路径与模型发现定向子集：27 项、15 个子测试通过，与上组部分重叠，不相加。
- 初次扩大到 `test_layout.py` 时有两条既有失败：历史开发文档链接断裂、打包夹具缺失
  `runtime/vsr-cuda/manifest.json`。使用 HEAD 旧版路径函数重新执行同样两条，均复现。
  [基线失败记录](velopack-validation-20261116/baseline-failures.txt) 保留，不顺带修改其他任务。
- 没有运行全仓回归。可选生产 GPU 包、真实用户安装、所有 UI 交互和原生异常中断回滚尚未验收。

## 可复现入口

先核对空间、创建仓库 `tmp/<task>/TASK.md`，将固定 wheel/工具校验并解压到任务目录。
不安装进现有 .venv 或全局环境：

```powershell
.venv/Scripts/python.exe -B scripts/probe_velopack.py --output tmp/<task>/fixture --sdk tmp/<task>/sdk --vpk tmp/<task>/tool/tools/net8.0/any/vpk.dll --stage
.venv/Scripts/python.exe -B scripts/probe_velopack.py --output tmp/<task>/fixture --case lite-delta --wrapped
```

每次仅一份 installed 候选；归档对应 evidence 后，按卫生规则检查精确任务目录和重解析点，
清理该候选再运行下一用例。探针不自行删除。`--stage` 只构建很小的合成 Python 程序。

实验核心打包接受 `--base`、`--vpk`、`--version`、新 `--output`；正式交付位置仅允许
`E:/dist/DLSS5Tool/<version>/`。登记过的 tmp 可显式用 `--experimental-task` 做合成验证。
不覆盖已有产物，也不会自行上传。后续发行的可选资产仍需纳入既有打包/上传门禁后才可交付。

## 正式启用前的剩余工作

1. 现有 ZIP → managed 布局的独立迁移入口与真实历史安装验证，包括旧版设置迁移和回滚。
2. full 组件首次分发、组件自身升级与协议不兼容时的完整处理；本轮只验证兼容外置组件保留。
3. 新 core/manifest 资产、历史差分发现和签名策略正式纳入发布门禁；保留旧版 required lite/full 增量。
4. 新布局下的真实应用 GPU/worker、多进程与异常中断恢复验收，再决定正式切换。

因此，本轮结论是框架可用且实验接入已可运行，不是“所有已安装旧版本已经获得通用升级”。
本轮没有触碰任何正式安装或发行附件。

## 清理

工作目录为已登记 `tmp/velopack-validation-20261116/`，预算新增峰值 < 1.5 GiB；
观测清理前约 1.04 GiB。证据归档后只清理此任务创建的下载、工具、冻结测试包和安装副本。
剩余占用与磁盘实测释放见 [cleanup.json](velopack-validation-20261116/cleanup.json)；
保留 TASK.md，2026-11-23 复核。其他任务、全局 SDK、.venv 和正式包不变。
