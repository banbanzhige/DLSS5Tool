# 本地打包与发布操作规程

本规程是本地构建、三形态打包、增量更新验证与发布上传准备的当前执行清单。
文件级更新机制、受管文件校验规则、安装事务恢复及历史版本记录见 [FILE_UPDATES.md](FILE_UPDATES.md)；
磁盘预算与清理边界见 [REPOSITORY_HYGIENE.md](../development/REPOSITORY_HYGIENE.md)；
打包轻量化清单与体积台账见 [PACKAGING_INDEX.md](PACKAGING_INDEX.md)。

## 1. 输出目录与路径安全

- 默认本地发布输出由 `packaging/release-output.json` 管理，统一放在共享根 `E:\dist` 下的本仓库子目录：`E:\dist\DLSS5Tool\<版本号>\`（例如 `E:\dist\DLSS5Tool\v3.1.6\`）；本机换盘或切换输出根可通过 gitignored 的 `packaging/release-output.local.json` 或环境变量 `DLSS5_RELEASE_ROOT` 覆盖，无需改写共享配置。
- 打包临时工作目录放在 `E:\dist\DLSS5Tool\tmp\release-<版本号>-<日期>\`，并按卫生守则登记用途、预计峰值与保留期限。
- 不将附件散落在 `E:\dist` 共享根，也不写入其他项目子目录；历史 D/F 盘发行目录与更新策略仍引用的基线保持原样，迁移或删除须另获明确授权。
- 复制、移动、替换或清理前先解析绝对路径并拒绝重解析点（符号链接/目录联接）；禁止清理共享根和其他项目目录。
- 已验证的旧包须保留至替代品验证通过；不因输出目录调整改动用户已安装程序或无关文件。

## 2. 完整打包与增量更新流程

执行打包或发版前，先定稿 `docs/release/RELEASE_NOTES_vX.Y.Z.md`（见 [RELEASE_NOTES_TEMPLATE.md](RELEASE_NOTES_TEMPLATE.md)），并确认目标盘空间满足卫生守则预算。

1. **只读基线预检**
   ```powershell
   .\.venv\Scripts\python.exe -B scripts/release_updates.py --preflight-version vX.Y.Z
   ```
   - `packaging/update-policy.json` 是共享支持范围与默认基线清单（使用 `{release_root}` 占位路径），当前策略为 `previous-release`（仅为上一个正式版本生成 `lite` / `full` 两种 `.dlssupdate`，更早版本走整包升级，不累计历史版本增量，不恢复已停用旧基线）。本机基线目录不同时，通过 gitignored 的 `packaging/update-policy.local.json` 或命令行 `--update-baseline` 覆盖路径，避免换盘时误改共享策略文件。
   - 基线缺失或文件被改时必须如实报告并停止，不得通过删减策略条目、清空列表、跳过 `full` 或改为只打整包来绕过。
   - 仅首次更新器基线 `v2.2.0` 允许使用 `--initial-update-baseline`，后续版本一律自动生成上一正式版到目标版的 `lite` / `full` 增量。

2. **核验上一正式版远端与本地基线一致性**
   - 读取上一正式 GitHub Release 的附件 SHA-256，核对本地 `lite` ZIP、`full` 分卷与线上正式附件一致，并逐文件校验归档、清单与解压基线（使用 `scripts/verify_release_baseline.py` 保存证据）。
   - 历史旧增量包、同名实验包或用户当前安装目录均不得作为正式基线。

3. **构建基础包并执行三形态完整打包**
   - 先运行 `scripts/build_release.ps1` 构建新基础包，再运行 `scripts/package_editions.py` 完成 `lite` / `full` / `addon` 三形态与增量包构建。
   - **`scripts/build_release.ps1` 仅为基础构建，不是完整发布交付**；完整交付必须经由 `scripts/package_editions.py` 生成并校验增量包。
   - `scripts/package_editions.py` 将 `RELEASE_NOTES_vX.Y.Z.md` 复制到 `editions/` 与 `github-assets/` 后，须确认源文档与两份副本完全一致。纯文案修正无需重压未包含该文件的三种大 ZIP。

4. **上传集合校验**
   ```powershell
   .\.venv\Scripts\python.exe -B scripts/release_updates.py --check-upload "<发行目录>/github-assets"
   ```
   - 交付报告必须明确列出增量包支持的源版本、`lite` / `full` 两种形态、校验结果与文件路径；不能仅交付整包却宣称增量已发布。

5. **真实增量升级验收**
   - 仅能打开 ZIP 或 `--check-upload` 通过不能替代升级验收。
   - 必须验证新 `lite` / `full` 增量可准确还原目标版本；必要时在临时安装副本中使用旧版冻结更新助手执行真实事务（`scripts/verify_release_upgrade.py`，详见 [FILE_UPDATES.md](FILE_UPDATES.md) 验证入口）。

6. **验收后登记新基线**
   - 新版本完成冻结程序、组件与升级验收后，将其正式 `lite` / `full` 解压目录与清单登记到 `packaging/update-policy.json`，供下一次发版自动选取。
   - 保留策略仍引用的基线，不自动删除旧版本目录（清理须另获授权）。

## 3. GitHub Release 授权与上传清单

- **禁止自动发布**：不得自动 `git push`、创建 GitHub Release 或上传附件，必须先获得用户明确授权。
- **默认上传附件**：获准上传后，默认仅上传安装整包、`full` / `addon` 分卷，以及必需的 `lite` / `full` `.dlssupdate`。
- **本地保留文件**：`Join-ReleaseArchive.ps1`、`RELEASE_NOTES_vX.Y.Z.md`、`SHA256SUMS.txt` 及打包报告默认保留在本地，不作为 Release 附件自动上传；SHA-256 校验与上传集合检查仍照常执行，Release Notes 内容直接用于 Release 正文。
- 仅在用户明确要求时额外上传辅助文件；不因此自动删除历史 Release 中已有的附件。
