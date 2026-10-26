# MatPilot core 0.2.0.dev9

新增生产工具 recommend_candidates（第10个MCP工具），对已生成任务或注册候选集合执行身份检查、元素完整/满占位筛查、结构去重和组成/空间群/规模类别覆盖推荐。查看 docs/candidate_recommendation.md。

安装到独立Python环境并连接现有Harness原生插件：

    python -m pip install matpilot_core-0.2.0.dev9-py3-none-any.whl

已有Harness dev10原生连接包已在本机与此core实测联通。GitHub的dev11环境部署包使用固定core dev8配方，本次未修改该配方；需要推荐功能时，另行建立dev9环境并在插件配置中连接，保留原环境供回退。源码归档为Python core源代码快照，其所附原生连接源码为本机dev10版本，不替代远端dev11安装包。

289项CPU测试：280通过、9项条件跳过、0失败；轮子中Python源码和独立安装目录逐字节一致。实际Harness确认10工具及新集合推荐。热重连首次能力调用曾超时，重试通过。

本轮对照使用全新生成12个候选（直接CLI6、插件生产服务6）。11个通过结构预检查；插件推荐与优先小结构基线选中相同集合，不能宣称独有质量收益。1场真实DeepSeek推荐演示完成，3次API保守费用0.041854元；AI文字含一处距离阈值误述，原文保留在证据中。所有输出均未弛豫、未DFT，未验证材料性能。

复现数据：../studies/2026-10-26-mattergen-comparison/。
对比PDF：仓库 output/pdf/MatPilot_插件能力与MatterGen对比报告.pdf。
SHA256SUMS.txt 用于核对发行文件。
