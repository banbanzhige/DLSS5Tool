# 文件级更新（schema 1）

状态：v2.2.0 首次引入；本地打包不等于已上传发布。旧便携包不具备此功能，须先手动安装 v2.2.0。不得用同一版本号重发不同内容并指望版本比较触发更新。

## 分发方案

采用**明确源版本 → 目标版本、明确 lite/full 的变化文件包**，不是二进制差分，也不是任意本地状态的逐文件 CDN 同步。相同依赖无需重新传输；全部变化文件集中成单个 ZIP 容器，扩展名为 `.dlssupdate`，避免旧更新器将它误选成完整 ZIP。跨多版本升级需发布直达目标版本的包；没有匹配包就回退手动整包升级，不自动串联升级链。

既有逐文件 SHA-256 清单理念被复用，生成器从两套已验证、只读的正式解压目录重新取清单，确保所用字节与实际包一致。完整包等于轻量包叠加同版本附加组件，故轻量＋附加包也匹配 full；不需要凭目录名推测发行版。

两个端点都必须含 `DLSS5Update.exe`；没有助手的旧包只能先手动安装新发行包。组件协议、依赖兼容性仍须通过发行测试，不能只凭程序版本号推断兼容。禁止从用户正在使用的安装目录生成基线。

## 构建与上传（不自动发布）

### 固定打包工作流（防遗漏）

`packaging/update-policy.json` 是必须支持的旧版本集合。当前登记已验证的 v2.2.0 基线目录
`dist/v2.2.0-release-20260911/editions`；它是正式新更新器基线，不是历史同名实验 ZIP。
后续版本不允许缺少增量包却完成打包。此工作流在**本地三形态打包入口**执行，GitHub Actions 的测试发现机制会运行工作流单测，但不会自动发布。

1. 更新目标版本资源和 Release notes，阅读卫生守则、检查容量、登记任务目录。
2. 主程序构建前先做只读基线检查，例如准备 v2.2.1 时：

   ```powershell
   .venv/Scripts/python.exe -B scripts/release_updates.py --preflight-version v2.2.1
   ```

3. 通过 `scripts/build_release.ps1` 构建新基础包（复用 `.venv`，选择全新输出），然后使用
   `scripts/package_editions.py --base ... --component ... --models ... --licenses ... --output ...`
   完成三形态打包。**无需额外手动调用差异包脚本**：入口默认读取策略，先核验全部旧版实际文件，
   再构建完整包及每个源版本到当前 `APP_VERSION` 的 lite/full `.dlssupdate`。
4. 增量包自动进入 `github-assets`、`SHA256SUMS.txt`，同时写入
   `package-report.json` 的 `incremental_updates`（源版本、形态、大小、SHA）及 `README-UPLOAD.txt` 必传列表。
   任一形态生成或校验失败，命令失败，不打印 `DONE`。失败目录不能上传，不能删策略条目来绕过失败。
5. 打包入口自动执行最后一道检查；手动上传前再次运行：

   ```powershell
   .venv/Scripts/python.exe -B scripts/release_updates.py --check-upload "新发行目录/github-assets"
   ```

   检查器核对策略与报告覆盖的旧版本集合，以及每个版本的 lite/full 附件、SHA、大小、清单版本/形态和 SHA256SUMS。
   上传仍需维护者确认，上传后核对 GitHub API 附件 digest。
6. 新版完成冻结程序、组件与升级验收后，将其版本和正式 `editions` 路径加入策略 `baselines`，
   使下一次发版覆盖该版用户；保留原条目，直到维护者明确决定停止支持。不自动将尚未验证的候选登记为基线。

路径迁移时可重复传 `--update-baseline "旧版editions新位置"`（预检和三形态打包均支持）。
参数只能重定位已登记版本，不会删除其他必需版本，也不能拿另一版本覆盖。多源版本先显式登记策略。

**首次例外**：重建 v2.2.0 时必须显式传 `--initial-update-baseline`（预检和三形态打包都传）。
仅策略中的首个更新器版本允许此例外，后续版本使用会报错。已有 v2.2.0 发行包保持原样；
它早于此工作流，没有 `incremental_updates` 字段，不应用新检查器为其补造历史成功记录。
历史命令记录保留用于追溯；如重新构建首次基线，须补此参数并使用全新输出。

### 底层单包工具（维护与测试用）

先阅读 `docs/development/REPOSITORY_HYGIENE.md`，检查磁盘和 tmp 容量，登记任务目录与峰值。复用已有 `.venv`。基础构建入口 `scripts/build_release.ps1` 会调用 `scripts/build_update_helper.ps1` 将独立 one-file 助手放到主 EXE 旁；不复制主程序的 `_internal` 给助手。助手不依赖系统 Python、Torch 或系统安装脚本。

以下工具由上述工作流自动调用；仅在独立维护/测试时手动使用，不能代替三形态打包和上传集合检查。完成两个端点的冻结程序/组件验证后，可针对单个形态运行（路径和版本为占位符）：

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

只将通过验收的 `.dlssupdate` 加到**目标版本** GitHub Release，保留现有轻量 ZIP、完整分卷、附加包作为新安装和失败兜底。必需源版本由策略维护，差异生成和上传目录收集已自动化，但不自动上传。检查 GitHub Release API 返回此附件的 `digest=sha256:...` 与本地报告一致；若没有合法 SHA-256、没有精确版本/形态名称或 URL 不属于本仓库此 tag，客户端不自动安装，转整包。此首版以官方仓库 HTTPS/API 附件摘要为信任根，不宣称具备独立签名、抵御仓库账号被攻陷的能力。

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
