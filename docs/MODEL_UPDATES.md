# 模型映射更新

## 2026-11-14：Carbon 与 Hydrogen

当前配置将 Carbon 设为高档首选、Hydrogen 设为低档首选，分别接替此前的推理模型和快速模型。旧映射保留，供用户明确指定、会话续聊及受控回退；不会把已有会话改成另一个模型。

| 使用档位 | 本次已登记的型号族 | 思考强度 | 默认使用方式 |
|---|---|---|---|
| 高档 | Gemini 4 Carbon | Low、Medium、High | Medium 处理一般复杂任务，High 用于多重约束与重要综合 |
| 低档 | Gemini 4 Hydrogen | Low、Medium、High、XHigh | Medium 起步，核对与约束较多时 High，XHigh 明确按需选择 |

分发从实时 CLI 目录与策略交集中选择，完整模型标识位于 `routing/policy.example.json`。业务逻辑与 Skill 不以型号名称猜测资格。高低档和思考强度相互独立，Hydrogen XHigh 仍是低档。

### 等待与权限

普通低档保持最多10分钟的观察保护；低档 XHigh 默认15分钟、最多30分钟；高档维持最多30分钟。真实新活动可以延续同一请求，静默不等同于失败。上述工程上限不是 Google 的额度承诺。

本次修复了旧代码把所有“模型能力例外”都当成高档的问题。新低档模型必须有精确的 `allowed_usage_tiers` 映射，不能仅改标签把高档模型伪装成低档。历史 scope 不会因此复活或扩权。当前聊天许可、会员认证、材料范围、无 API 回退与最终验收保持。

公开 AA v5.0.1 观测单独保存，不与原生角色冻结的 v4.3.2 分数混算；无同版分数或每任务成本时保留未知，外部精确模型例外不改变主 Codex、原生七角色或 editor 上限。

### 实测范围

在官方 Antigravity CLI1.5.8 与维护者现有账户上，自动分流分别选择 Carbon Medium 和 Hydrogen Medium；两条合成短文本的生成、模型身份回传、原生卡片与内容验收均通过。其余精确变体已检查目录和策略，没有在本次逐一调用。

Hydrogen High 也被配置为生图任务的编排候选，继续调用现有 `generate_image` 工具；图像后端仍由服务商选择。本次没有新生成图片，不能把文本验收当作新型号的生图全链路验收。

Orchestrator (Carbon) 已加入工作区任务的自动选模候选。它在同一会话中由 Carbon 规划／复核、Hydrogen 执行，使用 `effort="fixed"` 表示系统固定配置；Bridge 不向 CLI 传入虚构的思考档位。自动建议要求获准工作区与命令，且不混用原生子代理；普通文字任务继续按需使用单模型。

真实离线报告实现完成了规划、最初8项测试失败、实现与修正、14项测试通过；同一供应方会话追加功能后17项测试通过，原测试与历史交付保持不变。运行中的实际命令取消也收到远端ACK并完成进程回收。目录／配置回执与实际后端逐步模型追踪分别说明，后者目前不可见。接入及验证边界见 [Orchestrator 适配](../skills/gemini-bridge/references/orchestrator.md)。

### 能力与分工

多维能力评估也已更新：Hydrogen 可承担边界明确的实现、调试和界面迭代；Carbon 优先整体判断、关键表达、长文综合与复核。写作风格、长文可靠性、视觉理解、前后端与科研推理分别评估，不以一个总榜代替领域验收。详见 [证据与任务建议](../skills/gemini-bridge/references/model-capabilities.md)，其中区分 AA 同版观测、厂商评估、早期使用反馈和本地实测。

已有安装者需先备份并审阅合并自己的 `routing/policy.json`；公开示例的会员/模型能力例外默认关闭，仅在明确采用后启用，不覆盖私人策略。

参考：[Carbon 发布说明](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-4-carbon-now-available/)、[Hydrogen 发布说明](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-hydrogen/)、[CLI 更新记录](https://antigravity.google/docs/changelog?tab=cli)。
