# Security policy

## Supported versions

当前只维护默认分支的最新版本。实验性硬件路径不承诺向旧版本回移安全修复。

## Reporting a vulnerability

请优先使用 GitHub 仓库 Security 页面中的 private vulnerability reporting 功能，提供：

- 受影响的版本或提交；
- 可复现步骤和影响；
- 最小化的日志或样本；
- 已尝试的缓解方式。

如果私密报告入口尚未启用，请只创建一个不含漏洞细节的公开 issue，请求维护者提供私密
联系方式。不要在公开 issue 中附加漏洞利用代码、私人媒体、完整本机路径、令牌或专有 DLL。

本项目会加载本地 DLL 并调用外部 FFmpeg。请只使用可信来源的二进制，并在发布报告前确认
问题能在干净环境中复现。

