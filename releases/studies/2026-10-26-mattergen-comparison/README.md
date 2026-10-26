# 本轮新生成的 MatterGen 对照材料（2026-10-26）

这里仅收录本轮重新生成的12个CIF，未混入历史128候选。直接CLI生成6个，MatPilot生产服务生成6个；生成阶段由脚本调用，并非LLM驱动。CIF均未弛豫、未做DFT，不能作为材料性能证据。

manifest.json记录每个CIF的SHA256、来源组、原始成员名和结构版本；同一批12个候选供所有排序策略使用。D001至D006在先、P001至P006在后，是预先固定的组/批/成员顺序，不是科学排序。

## 复现后处理

在仓库根目录安装Python核心及依赖后运行（仅CPU，无模型API或新生成）：

    python benchmarks/compare_fresh_recommendations.py --manifest releases/studies/2026-10-26-mattergen-comparison/manifest.json --output fresh-reproduction.json

脚本核验CIF字节和事先固定的推荐实现哈希；已有输出不覆盖。所有策略共享资格检查和结构去重，K=4，随机基线1000次、随机种子20261026。候选生成器的本地旧CLI不提供seed，本次生成未设置seed；这不代表当前上游版本没有seed。

## 实测结论

12个候选中11个通过该规则成为代表；D004缺少目标元素。插件与优先小结构基线选中相同集合：D003、D005、P001、D001（顺序可能不同），覆盖4组成、4空间群、2规模档，总输入晶胞原子数40。原序前4个合格代表总原子数51，覆盖同为4/4/2。因此不能声称插件在本轮具有独有覆盖优势，也不能把原子数差异等同真实计算成本下降。

两组生成并非相同随机样本，进程数、轮询和后处理计时也不同。直接侧记录538.486秒，插件侧622.703秒，仅为过程记录，不能算作提速率或插件生成质量优势。

历史128候选的复用分析和此前LLM对照只在PDF参考附录；没有计入上述数字。新Harness真实AI演示是读取这批新候选并调用推荐工具，单独记录，不冒充三臂用户实验。

## 证据文件

- ../../../artifacts/mattergen-usage-comparison-20261026.json（仓库根目录 artifacts 下同名文件为准）
- artifacts/fresh-candidate-recommendation-comparison-20261026.json（仓库根目录）
- artifacts/harness-recommendation-validation-20261026.json（仓库根目录）

所有结果是结构预检查和代表性选择证据；稳定性、能量、合成、催化、耐腐蚀尚未测量。
