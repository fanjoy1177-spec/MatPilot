# 材策 MatPilot Harness 插件

本包向 DeepSeek Harness 注册 MatPilot 的九个材料研究工具，使用 Harness 自带界面。

先安装随版本发行的 `matpilot-core` Python 包，并解压同版本源码包取得 examples 配置与 CIF 模板；wheel 与本 tgz 均不包含这些配置材料。将 `MATPILOT_PYTHON` 设置为该环境的 Python 绝对路径，将 `MATPILOT_CONFIG` 设置为后端配置文件绝对路径，再将本包安装到 Harness profile。

默认是固定样本示例模式，实际材料生成须连接 MatterGen 环境。DeepSeek API 密钥在 Harness 中配置，不写入本插件包。

本 tgz 用于命令行/Web profile。桌面版的 desktop profile 由应用管理，应按项目 docs/desktop_setup.md 配置内置 MCP 客户端，不能用命令行强行安装到 desktop profile。

完整安装说明、测试记录和科学结果边界见项目根目录 README。本地发行包不代表已在 npm 发布。
