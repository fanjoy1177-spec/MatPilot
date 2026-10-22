# dev5 部署与开发对照验收

版本：Python 0.2.0.dev5；Harness 插件 0.2.0-dev.5。

## 软件与安装

176项Python回归、26项研究控制器及本地进程检查通过。wheel与源码逐文件一致，Harness插件包已通过隔离配置安装。批次上限新增低于上限允许、超限阻塞及上下界语义回归。

## 真实AI开发联调

使用实际DeepSeek Harness 0.2.1-rc.2、deepseek-official/deepseek-flash、reasoning off。科学部分使用条件绑定的合成回放；没有真实MatterGen、DiffCSP++或性质计算。

r1接口预检4次，r2完整开发比较36次，r3修正后36次，旧记录均保留。r3每组相同12个公开开发题、每题1次，36次对话正常结束；这不是36次任务正确完成。自动审计和作用案例见study_development_results.md及study_plugin_effects.md，语义与结论仍待用户审核。

三组统一8192输出tokens、最多16请求、180秒；固定正常上游技能和共同CPU回放协议。子会话局部排除生产MatPilot MCP说明与资源广告；每个请求前核验，通用宿主提示保留。M的领域说明与检查工具作为被评估的插件部分。

## 费用

本次r3为184次请求、保守计量7.415622元。累计475次请求、保守上界18.839824元，含一次历史未结算预留。20元是通知阈值；最后阶段含新请求预占时曾达到20.003012元，已提示用户，结算后回落。金额不是服务商账单实扣。

按官方价格页所列峰时cache-miss输入2元/百万tokens、输出8元/百万tokens保守计量；缓存输入也按miss估算。价格来源：[DeepSeek官方价格说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。

## 结论边界

程序核验可提供组成、版本、空间群和方法可比证据，但LLM自由文本仍可能误述证据。无独立人工评分或用户主动时间数据，不输出完整任务正确率或整体人工提速百分比。两模型真实计算验收、服务器计算环境和正式主实验尚未完成，当前用户要求禁止真实计算并在交付后自行审核。

桌面已实际加载dev5九个工具；MatterGen和DiffCSP两个固定样本流程均完成，各返回2个可解析文件。参数冲突被阻塞；每批上限2时，实际批次1为ready、实际批次3为blocked。验收记录见 artifacts/v05-deployment-validation.json，API调用为0。
