# 第三方组件与来源

MatPilot 的 MIT 许可证只覆盖本仓库原创代码与文档。模型权重、第三方代码和各依赖受各自许可证约束。

| 组件 | 用途 | 来源 |
|---|---|---|
| DeepSeek Harness | 宿主界面、语言模型接入和工具循环 | https://github.com/deepseek-ai/deepseek-harness |
| MatterGen | 候选无机晶体生成，独立安装，不随插件分发权重 | https://github.com/microsoft/mattergen |
| MCP Python SDK | 标准输入输出工具协议 | https://github.com/modelcontextprotocol/python-sdk |
| pymatgen | CIF 解析、晶体组成与基础结构计算 | https://github.com/materialsproject/pymatgen |
| MG-Flow | 既有项目中的条件字典、批次执行与结果溯源经验 | 用户提供的 MG-Flow 项目 |

本项目没有重新训练 MatterGen，也没有训练或压缩 DeepSeek。未将模型性能、模型架构或 Harness 界面列为本项目原创成果。发行包不包含第三方权重、GPU 运行环境或用户密钥。

## DiffCSP++ and AtomisticSkills baseline snapshot

DiffCSP++ is an upstream generative model, not a MatPilot model. The bridge imports the operator-installed AtomisticSkills wrapper at commit 6afdd465a35088136e4875d7d2fbaefc633f4adf after identity checks. No checkpoint is redistributed. Verify the selected checkpoint terms separately.

The four-file AtomisticSkills source snapshot and U+ modification patch in benchmarks/baseline are included for reproducible comparison. Copyright and MIT terms are preserved in benchmarks/baseline/LICENSE.AtomisticSkills. Changes cover condition preservation, exact sampling budgets, unique candidate selection and query-failure status. No affiliation or upstream endorsement is implied.

The research replay driver also includes a 16-file source and normal-skill snapshot under benchmarks/study_drivers/upstream_snapshot from the same pinned commit. Its original MIT LICENSE and portable source hashes are retained in that directory and upstream-source.json. Scientific engine substitutions are documented explicitly; no trained weights are included. PyYAML is an additional research-driver dependency under its own MIT terms.
