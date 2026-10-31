# MatPilot core 0.2.0.dev10

补齐dev9缺失的自动“优先弛豫”分级。默认auto对exact Fe-Ni-O先执行结构核验和代表去重，按固定启发式标记优先弛豫/人工复核/暂缓，再从优先池做多样性推荐。其它体系回退通用structural_v1；显式structural_v1可重放dev9结果。

安装到独立Python环境：

    python -m pip install matpilot_core-0.2.0.dev10-py3-none-any.whl

在Harness原生插件设置中连接该Python与配置。原生bundle版本和Python core版本分别管理；本机用native dev10连接core dev10。GitHub的native dev11固定部署配方仍为core dev8，本次不覆盖受管环境或配方。本源码包是Python core快照，所附连接源码是本机native dev10，不替代远端dev11部署包。

303项CPU测试无失败（9项条件跳过）。此次没有新的材料生成、弛豫、DFT或模型API调用；只是对原12个新CIF补齐后处理。11结构代表中5个进入优先池，6个转人工复核，推荐4个。相同优先池最小结构基线覆盖3组成、43位点；插件覆盖4组成、46位点，顺序基线与插件集合相同。既保留覆盖收益也保留规模代价和持平结果。

价态、距离、体积和密度阈值是复核启发式。统一价态检查不含混合价态，未通过只表示需要复核，不证明化学不可能。优先弛豫是下一步任务建议，不保证稳定、可合成、物性或弛豫成功。

使用细节：docs/candidate_recommendation.md。
补充复现：python benchmarks/compare_relaxation_triage.py --manifest releases/studies/2026-10-26-mattergen-comparison/manifest.json --output triage-reproduction.json
修订PDF：output/pdf/MatPilot_双场景操作对比.pdf。
