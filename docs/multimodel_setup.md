# 第二模型与执行边界

本版实现 MatterGen 和 DiffCSP++ 两个适配器、模板解析和统一候选收集；尚未完成第二模型真实计算验收。默认 real_compute_enabled=false，计划准备、模板解析和固定样本测试不加载模型。

DiffCSP++ 采用 AtomisticSkills 的独立包装器，固定提交 6afdd465a35088136e4875d7d2fbaefc633f4adf。上游参考目录保持不变，运行时不把它的所有服务暴露给 Harness，仍使用原九个 MatPilot 工具。

## 模板登记

操作者在配置 templates 中登记一个 ID，对应 path、sha256、source、review（by/date/scope），可设 demo_only=true。LLM 只能选择已登记 ID，不能提交任意文件或凭空编写 Wyckoff 参数。来源和审核人属于操作者提供的记录，程序不能认证其真实性。

插件解析有序 CIF、转为标准常规胞，并从 pymatgen/Spglib 得到空间群、等价位点、元素及 Wyckoff 重数。采用 symprec=0.01 Å、angle_tolerance=5°。执行前，隔离桥接器用 PyXtal 再核对各字母的重数及实际化学计量；不一致即停止，不改写用户要求。

使用例子见 examples/config.multimodel.demo.json 和 examples/request.diffcsp.json。其中 NaCl 结构是人为构造的软件固定样本，采用 CsCl 型的两点立方布局，空间群 221；不是岩盐 NaCl 的实验数据，也不是模型发现结果。模板只能用于 demo。

## 独立计算环境

真实执行适用于 Linux，Windows 使用 WSL。环境配置示例为 examples/config.diffcsp.native.json；提供独立 Python、AtomisticSkills 干净固定源码、DiffCSP-PP 干净固定源码，以及该检查点目录每个文件的 SHA256。

权重目录必须只有一个 .ckpt；清单同时包含 hparams.yaml 和存在的 scaler 文件，防止上游“优先 last、否则最大 epoch”选择不透明。身份校验不代替权重安全、许可或适用性核对。配置不完整只影响真实执行，能力卡不会把接口存在说成真实就绪。

桥接采用列表形式传 Wyckoff 与元素，明确批次目录，记录上游身份、模板、随机种子及实际数量。少于请求数或部分 CIF 无法解析时保留有效产物和错误，任务显示 partial/failed；所有已经启动的尝试仍占预算。Linux/WSL 进程组监督负责正常停止与超时，并在父进程先退出时清理该组的子孙进程。dev6 通过实际 CPU 进程测试；无法确认终止时保持待处理状态，禁止采集活动文件或直接恢复。监督器被 SIGKILL 强杀、宿主崩溃或子进程主动创建新会话不在这一保证范围内；真实 GPU 停止行为仍待以后验收。

当前用户要求禁止真实计算，不能为完成验收把 real_compute_enabled 改为 true。没有实际模型通过的小任务证据前，只称为双适配器软件版本。

官方资料：[DiffCSP++](https://github.com/jiaor17/DiffCSP-PP)、[固定版本 AtomisticSkills 包装器](https://github.com/learningmatter-mit/AtomisticSkills/blob/6afdd465a35088136e4875d7d2fbaefc633f4adf/src/utils/generative_models/diffcsp/diffcsp_wrapper.py)。

## 跨平台源文件身份

0.2.0.dev4 起，DiffCSP 包装器哈希按 UTF-8/LF 文本计算，避免 Windows CRLF 与 Linux LF 检出差异。锁定提交仍必须干净，修改任何源码内容仍拒绝。此规范化哈希已与该提交的 Git 原始文件内容核对；它不是模型权重或真实推理验收。
