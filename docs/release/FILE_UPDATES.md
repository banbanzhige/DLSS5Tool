# 文件级更新（schema 1）

状态：v2.2.0 首次引入；本地打包不等于已上传发布。旧便携包不具备此功能，须先手动安装 v2.2.0。不得用同一版本号重发不同内容并指望版本比较触发更新。

## 分发方案

采用**明确源版本 → 目标版本、明确 lite/full 的变化文件包**，不是二进制差分，也不是任意本地状态的逐文件 CDN 同步。相同依赖无需重新传输；全部变化文件集中成单个 ZIP 容器，扩展名为 `.dlssupdate`，避免旧更新器将它误选成完整 ZIP。跨多版本升级需发布直达目标版本的包；没有匹配包就回退手动整包升级，不自动串联升级链。

既有逐文件 SHA-256 清单理念被复用，生成器从两套已验证、只读的正式解压目录重新取清单，确保所用字节与实际包一致。完整包等于轻量包叠加同版本附加组件，故轻量＋附加包也匹配 full；不需要凭目录名推测发行版。

两个端点都必须含 `DLSS5Update.exe`；没有助手的旧包只能先手动安装新发行包。组件协议、依赖兼容性仍须通过发行测试，不能只凭程序版本号推断兼容。禁止从用户正在使用的安装目录生成基线。

## 构建与上传（不自动发布）

先阅读 `docs/development/REPOSITORY_HYGIENE.md`，检查磁盘和 tmp 容量，登记任务目录与峰值。复用已有 `.venv`。基础构建入口 `scripts/build_release.ps1` 会调用 `scripts/build_update_helper.ps1` 将独立 one-file 助手放到主 EXE 旁；不复制主程序的 `_internal` 给助手。助手不依赖系统 Python、Torch 或系统安装脚本。

完成两个端点的冻结程序/组件验证后，分别针对 lite、full 运行以下形式的命令（路径和版本为占位符，替换为真实已验证输入）：

```powershell
.venv/Scripts/python.exe -B scripts/build_file_update.py `
  --before "已验证旧版轻量目录" --after "已验证新版轻量目录" `
  --from-version vX.Y.Z --to-version vX.Y.N --edition lite `
  --output "tmp/任务-日期/update-lite"
```

full 使用两套对应的完整目录、`--edition full` 和新的输出目录。生成器拒绝覆盖输出或将输出放进输入目录；它只读取输入，直接写变化文件 ZIP，不复制运行环境。生成前打印估算体积和剩余空间，至少保留预计峰值＋15 GiB 余量；发布者仍须核查仓库 tmp 总量。超出客户端 2 GiB 单附件支持范围时停止，保留整包方案，不发布失败候选。

输出：

- `DLSS5Tool-vX.Y.Z-to-vX.Y.N-win64-lite.dlssupdate`（或 full）；
- `update-report.json`：实际 SHA-256、大小、变化/移除文件数量、未发布标记。

只将通过验收的 `.dlssupdate` 加到**目标版本** GitHub Release，保留现有轻量 ZIP、完整分卷、附加包作为新安装和失败兜底。发布清单与旧版直达更新包由维护者维护，不自动上传。检查 GitHub Release API 返回此附件的 `digest=sha256:...` 与本地报告一致；若没有合法 SHA-256、没有精确版本/形态名称或 URL 不属于本仓库此 tag，客户端不自动安装，转整包。此首版以官方仓库 HTTPS/API 附件摘要为信任根，不宣称具备独立签名、抵御仓库账号被攻陷的能力。

## 校验与受管范围

`manifest.json` 含 `schema/from/to/edition/before/after`；两个完整文件清单均记录 `{size, sha256}`。`payload/` 只能包含 after 中新增/变化的文件，before 独有文件视作待移除的官方旧文件。

客户端先校验整个下载的官方 SHA-256，再验证清单、严格路径、Windows 大小写冲突、设备名、ADS、越界、归档条目、解压体积与每个文件 SHA-256。仅根目录明确文件白名单、`_internal/`、`licenses/`、`mods/README.md` 可受管；full 额外管理 `mods/enhancement/`、`mods/models/`、附加包说明。设置/队列/输出、`mods/nvngx_dlssnr.dll` 等不在受管范围。未知文件不主动删除；自定义文件与新官方路径冲突时拒绝覆盖。目录联接、符号链接、硬链接均拒绝参与更新。

下载后及安装前检查整个旧版受管基线，包括未变化的依赖；任何缺失或修改均停止，不静默保留不兼容 DLL。运行库自定义请优先放 `mods`，而非覆盖官方 `_internal`。自定义外置组件目录需人工升级；新安装/移除本地组件导致形态变化也会停止。

## 安装事务与恢复

用户确认下载后只写 `.dlss5-update`。安装前第二次确认；父进程完全退出且取得安装互斥锁后，助手重新校验，复制**变化文件的旧字节**到备份并刷盘，逐次记录写入意图，再使用同盘临时文件替换。安装结束检查整个目标清单，成功显示提示并由用户手动重启，不自动运行下载的主程序。

状态：`failed`（准备未完成/未替换）→ `ready` → `waiting` → `applying` → `complete`；出错进入 `rolled_back` 或 `recovery_needed`。助手文件来自已安装旧版，不执行归档中的脚本。安装前 120 秒未等到父进程退出则恢复 ready；同安装目录通过 Windows 命名互斥锁防止并发 GUI/助手，事务目录另有进程锁。

普通 I/O 错误会回滚已经尝试替换的文件；不可恢复错误保留日志和备份。进程异常退出后的 `applying` 状态会阻止 GUI 启动。关闭主程序和其他助手后，按真实绝对路径执行：

```powershell
& "安装目录/.dlss5-update/DLSS5Update.exe" --root "安装目录" --recover
```

中断在 waiting 时只恢复为 ready，不修改程序；中断在 applying 时按已刷盘的 `journal.json` 回滚。恢复可重试，备份校验失败或路径变成链接时拒绝恢复并报告。不保证在硬盘损坏/掉电破坏文件系统时总能自动恢复，也不替代旧完整包保留策略。

成功备份不自动删除；用户下次更新时明确确认后清理。仅 `complete/rolled_back/ready/failed` 可清理；正在安装或待恢复时禁止清理。未确认不创建第二套候选。取消下载可能保留暂存，同样确认后清理。请勿删除整个程序目录或用户 mods。

## 验证入口

```powershell
.venv/Scripts/python.exe -B -m unittest tests.test_delta_update tests.test_update_helper tests.test_gui_updates tests.test_updater tests.test_release_packaging tests.test_i18n
.venv/Scripts/python.exe -B scripts/probe_file_update.py --helper "助手 EXE" --output "全新任务内探针目录"
```

单测覆盖版本/形态、摘要、额外归档条目、非法路径、冲突、自定义数据、全量基线、本地/暂存修改、取消、磁盘不足、替换失败回滚、进程中断恢复和 Windows 互斥/父进程等待；GUI 契约测试确认双重确认、忙碌不退出、助手就绪后才退出。冻结助手探针只使用合成字节，不能代替正式发行 GUI/GPU、真实版本差异包及干净机器验证。
