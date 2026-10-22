# U+ 局部修复

基于 AtomisticSkills 提交 6afdd465a35088136e4875d7d2fbaefc633f4adf；四份上游文件原文见 upstream_snapshot，MIT 许可证见 LICENSE.AtomisticSkills。补丁不修改只读研究参考副本。

在完整、干净的相同提交副本中执行 git apply atomisticskills-uplus.patch。source-lock.json 记录原始字节哈希与补丁后的 UTF-8/LF 内容哈希；Windows 换行可能不同。tests/test_baseline_patch.py 在临时目录应用公开补丁，逐文件核验，再用替身验证四项具体行为；不加载模型、不发数据库请求。

修复范围：条件无静默覆盖、精确采样预算、唯一候选选择、查询失败未知状态。凸包数值和参照方法一致性等其余科学问题不因此自动解决。四项替身测试通过不等于完整上游驱动通过，不构成 M 优势证据。

首次 GitHub Linux CI 暴露快照 CRLF 与补丁 LF 不一致。测试先核对保留的原始字节哈希，再明确转成 UTF-8/LF 并核对可移植哈希，以 core.autocrlf=false 应用补丁。上游只读原件未修改；公开快照通过 .gitattributes 保留原始字节。真实上游副本应使用 LF 检出。
