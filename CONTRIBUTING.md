# Contributing

感谢参与 DLSS5Tool。提交改动即表示你有权贡献相应内容，并同意项目按根目录的 MIT
许可证分发该贡献。

## 开发环境

1. 使用 Windows 10/11 和 Python 3.10+。
2. 运行 `setup.bat` 创建 `.venv` 并安装依赖。
3. 运行测试：

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -v
   ```

只有修改或验证原生 NGX 路径时才需要 NVIDIA GPU、SDK 和运行时。普通单元测试必须保持
可在没有 GPU 和专有 DLL 的环境中执行。

## 提交前检查

- 保持改动聚焦，并为行为改动补充或更新测试。
- 运行语法检查和完整单元测试。
- 不提交 DLL、LIB、PDB、日志、用户设置、私人媒体、大型生成物或 `third_party/`。
- 不复制 NVIDIA SDK 文件；对第三方代码保留原始版权与许可证。
- 不在 issue、测试样本或日志中泄露个人路径、媒体、令牌或其他敏感信息。

## Pull request

在 PR 描述中说明问题、方案、测试方法和硬件相关限制。涉及 GPU 输出变化时，可提供经授权
公开的小型截图或哈希，但不要上传无权再分发的模型、运行时或测试媒体。

发现安全问题时不要创建公开 issue，请遵循 `SECURITY.md`。

