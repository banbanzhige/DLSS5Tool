# Contributing

感谢参与 DLSS5Tool。提交改动即表示你有权贡献相应内容，并同意项目按根目录的 MIT
许可证分发该贡献。

## 开发环境

完整的源码运行、SDK 准备与打包步骤见[开发指南](docs/development/BUILDING.md)。

1. 使用 Windows 10/11 和 Python 3.10+。
2. 运行 `setup.bat` 创建 `.venv` 并安装依赖。
3. 运行测试：

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -v
   ```

只有修改或验证原生 NGX 路径时才需要 NVIDIA GPU、SDK 和运行时。普通单元测试必须保持
可在没有 GPU 和专有 DLL 的环境中执行。

## 发行包轻量化约定

开发环境可以保留全组件，但面向用户的发行包必须按功能需要尽量轻量、精简，不能直接
复制整个开发环境或开发机 `mods`。基础包、增强运行组件和模型分开管理；不能以静默
降低画质、破坏功能或要求用户安装 Python/PyTorch/CUDA Toolkit 的方式缩小包体。

打包或修改推理依赖前，先查阅 [打包轻量化记录与索引](docs/release/PACKAGING_INDEX.md)，并按对应
任务编号更新文件清单、压缩/解压体积及验证结论。仅记录的候选不等于已完成裁剪。

## 提交前检查

发布打包必须包含增量阶段：`packaging/update-policy.json` 指定必需旧版基线，
`scripts/package_editions.py` 自动生成 lite/full 差异包并校验上传集合。
`build_release.ps1` 仅完成基础构建，不代表已完成发布交付。
固定命令与禁止跳过项见 [文件更新工作流](docs/release/FILE_UPDATES.md)。

构建、打包和实验前须遵守 [仓库卫生守则](docs/development/REPOSITORY_HYGIENE.md)：
检查磁盘预算，登记临时目录，在任务结束时回收可重建产物；Git 忽略规则不等于自动清理。

目录约定见 [项目目录与文档索引](docs/README.md)。应用模块放入 `dlss5tool/`，
技术文档放入 `docs/`，构建配置放入 `packaging/`，维护脚本放入 `scripts/`；
不要将 DLL、设置或编译中间产物重新输出到根目录。

- 保持改动聚焦，并为行为改动补充或更新测试。
- 运行语法检查和完整单元测试。
- 不提交 DLL、LIB、PDB、日志、用户设置、私人媒体、大型生成物或 `third_party/`。
- 不复制 NVIDIA SDK 文件；对第三方代码保留原始版权与许可证。
- 不在 issue、测试样本或日志中泄露个人路径、媒体、令牌或其他敏感信息。

## Pull request

在 PR 描述中说明问题、方案、测试方法和硬件相关限制。涉及 GPU 输出变化时，可提供经授权
公开的小型截图或哈希，但不要上传无权再分发的模型、运行时或测试媒体。

发现安全问题时不要创建公开 issue，请遵循 `SECURITY.md`。
