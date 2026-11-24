# DLSS5Tool Agent 指南

## 项目速览与测试

- 目录：`dlss5tool/`（应用主包，统一 `dlss5tool.*` 导入）、`native/`（原生 C++ 宿主）、`scripts/`（构建与维护脚本）、`packaging/`（打包与更新策略）、`tests/`（单测）、`docs/`（文档）。`gui.py` 仅为启动入口。
- 定向单测：`.\.venv\Scripts\python.exe -B -m unittest <测试模块或路径>`。普通单测必须可在无 GPU / 专有 DLL 环境下运行。
- **测试范围**：优先按改动范围运行对应模块及关联测试，禁止无故跑全量回归；仅在影响范围无法局部覆盖或有明确验收要求时运行全量测试。

## 代码结构、提交与产物

- **GUI 架构**：`dlss5tool/gui.py` 中已有的 `App` Mixin 暂不做大规模重构；后续新增独立界面流程或业务协调模块时，不再继续叠加 Mixin，改为独立组合式控制器（显式传入所需接口，并在初始化阶段显式声明状态字段）。
- **提交拆分**：不同主题的功能、修复与设计文档拆分为独立 commit，避免无关改动混在同一提交。
- **证据归档**：大体积全量清单 JSON、`.zip` 等观测/验收产物统一归档至 `E:\dist\DLSS5Tool\...\evidence\` 或外部任务目录；Git 仅保留精简 Markdown 结论与可复现脚本（已入库历史不回溯改写）。

## 安全红线（所有任务生效）

- **禁止自动发布**：不得自动执行 `git push`、创建 GitHub Release 或上传附件，必须先获得用户明确授权。
- **禁止破坏性清理**：禁止 `git clean -fdx`、通配符批量删备份或清空整个 `tmp/`（内含 `guidance-cuda-env`、`dlss5standaloneV2` 等持久依赖）。删除文件前核实绝对路径，使用原生 PowerShell `-LiteralPath`，拒绝沿符号链接或目录联接递归。

## 按需查阅的操作规程（执行对应任务前必读）

| 任务场景 | 必读文档 | 关键约束摘要 |
| --- | --- | --- |
| 构建、大文件实验、下载或清理 `tmp/` | [REPOSITORY_HYGIENE.md](docs/development/REPOSITORY_HYGIENE.md) | 临时任务放 `tmp/<任务>-<日期>/`，目标盘低于 15 GiB 暂停大产物生成，交付前清理可重建产物并报告占用 |
| 本地打包、发版或验证增量更新 | [RELEASE_PLAYBOOK.md](docs/release/RELEASE_PLAYBOOK.md) | 统一输出至 `E:\dist\DLSS5Tool\<版本>\`；必须运行 `scripts/package_editions.py` 并通过预检、基线核验与升级验收，仅 `build_release.ps1` 不算完成交付 |
| 编写 `RELEASE_NOTES_vX.Y.Z.md` | [RELEASE_NOTES_TEMPLATE.md](docs/release/RELEASE_NOTES_TEMPLATE.md) | 仅写相对上一正式版的用户可感知变化，逐字沿用模板中的四项「下载选择」 |
