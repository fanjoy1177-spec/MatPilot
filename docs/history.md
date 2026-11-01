# 历史资料

当前仓库保留源码、使用文档、回归与复现输入，以及 core dev14 / Harness dev12 安装包。旧版包、生成报告和开发过程记录已从当前文件树移除，完整内容仍可从清理前的固定提交 `9052a358f3f0b7135abd66db546fccb7ad2a8004` 访问；Git 历史未改写。

| 资料 | 固定历史入口 |
|---|---|
| 历次验证 JSON、运行摘要与图片 | [artifacts](https://github.com/fanjoy1177-spec/MatPilot/tree/9052a358f3f0b7135abd66db546fccb7ad2a8004/artifacts) |
| 旧核心、Harness 安装包与研究资料包 | [releases](https://github.com/fanjoy1177-spec/MatPilot/tree/9052a358f3f0b7135abd66db546fccb7ad2a8004/releases) |
| 生成的对比 PDF 与证据清单 | [output/pdf](https://github.com/fanjoy1177-spec/MatPilot/tree/9052a358f3f0b7135abd66db546fccb7ad2a8004/output/pdf) |
| 旧版验收、调研、实施状态和研究提案 | [docs](https://github.com/fanjoy1177-spec/MatPilot/tree/9052a358f3f0b7135abd66db546fccb7ad2a8004/docs) |
| 全 12 题插件作用对照及不利结果 | [study_plugin_effects.md](https://github.com/fanjoy1177-spec/MatPilot/blob/9052a358f3f0b7135abd66db546fccb7ad2a8004/docs/study_plugin_effects.md) |
| 开发消融结果、耗时和费用限制 | [study_ablation_results.md](https://github.com/fanjoy1177-spec/MatPilot/blob/9052a358f3f0b7135abd66db546fccb7ad2a8004/docs/study_ablation_results.md) |

历史资料中的版本、链接和数字只对应当时的实验范围，不代表当前安装方式或普遍效果。目录中的文件可在 GitHub 打开后使用 Raw/Download 获取；完整旧树可检出上述提交。

`benchmarks/deployment/baseline/` 是原部署流程的冻结快照，保留原始字节和旧相对路径。复查其中的旧发行文件时，请使用上表固定历史入口；当前部署以 [deployment.md](deployment.md) 为准。需要复算旧版本比较脚本时，先从历史入口取回指定 wheel，并按原记录核对哈希后传入 `--baseline-wheel`。

最新公开研究数据与标准库重算入口保留在 [benchmarks/ranking100](../benchmarks/ranking100/)，软件与科学证据边界见 [验证摘要](validation_summary_20261101.md)。
