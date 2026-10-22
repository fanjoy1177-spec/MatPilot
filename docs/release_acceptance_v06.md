# dev6 部署与修复验收

版本：Python 0.2.0.dev6 / Harness 包 0.2.0-dev.6。更新日期：2026-10-23。

本版针对生产流程修复和正式评估归档，不追加真实 LLM 测试，也不运行 MatterGen、DiffCSP++、GPU 或 DFT。现有开发对照及消融保持原始版本与结果。

## 修复的实际作用

- 保留短返回、坏 CIF 混合批次中可解析的有效文件；非零退出或已确认中止后，仅采集完整可读取归档。
- 已启动的采样尝试继续计入预算；不会因保留部分结果而称任务全部成功，也不会自动超额续跑。
- Linux/WSL 监督本任务进程组，父进程先退出也会清理子孙；无法确认结束时阻止恢复和采集。
- 修改建议列出需求及条件差异，保留数值目标、预算和硬结果检查，需要另行审阅新计划。
- 正式评估导入核对运行包、事件、轨迹与账本；失败请求不会被无依据写成零费用。

## 验证记录

- Windows完整Python回归227项：218通过，9项Linux专属测试按平台跳过；失败和错误均0。记录见 artifacts/v06-unit-validation.json。
- JavaScript控制器与本地进程协议27项通过，见 artifacts/v06-js-validation.json。
- 进程监督11项在WSL通过；部分产物/异常收尾11项在Windows和WSL均通过。相关边界见 artifacts/v06-process-validation.json 和测试文件。
- dev5→dev6同夹具对照：短返回0→1、好坏CIF混合0→1、非零退出完整归档0→2；每例仍扣2次采样额度，不允许耗尽后恢复。详见 artifacts/v06-partial-output-comparison.json。

Python wheel与源码逐文件核对通过，Harness tgz在隔离配置安装成功。实际桌面已加载dev6的九个工具，MatterGen和DiffCSP两条固定样本路径各返回2个可解析文件、去重后各1个；条件冲突、批次上限及具体修复建议检查通过。验收模块随后移除，只保留常规MatPilot接入。公共记录见 [桌面验收JSON](../artifacts/v06-deployment-validation.json)。旧dev5发行包保持原字节。正式研究正确率、人工用时和真实模型验收不在这些软件检查的证明范围内。

## 比较与费用

| CPU固定场景 | 冻结dev5保留数 | dev6保留数 | dev6状态 | 两版采样扣额 |
|---|---:|---:|---|---:|
| 请求2，实际返回1 | 0 | 1 | partial | 2 |
| 2个CIF中1个损坏 | 0 | 1 | partial | 2 |
| 非零退出但有完整2文件归档 | 0 | 2 | partial | 2 |

这些是生产worker的版本修复效果，不是AI正确率或材料性能对照。归档中的两个文件可能等价，保留数量不表示两个独立材料。复算脚本见 [compare_partial_outputs.py](../benchmarks/compare_partial_outputs.py)。

本轮API新增0次、0元；累计保守上界仍为22.273968元，包含历史保留预占2.113536元。20元提醒已发送。研究假设没有因本次软件修复而自动成立；正式题库/评分、用户主动时间与真实模型验收仍待完成。

发行包位于 [dev6目录](../releases/0.2.0.dev6/)，下载后核对SHA256SUMS.txt。wheel不包含配置和模板，需同时解压同版本源码包。开发对照原始记录、dev5旧包及旧桌面固定安装目录均保留。
