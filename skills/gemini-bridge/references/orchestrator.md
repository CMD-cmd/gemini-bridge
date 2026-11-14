# Orchestrator 适配状态

2026-11-14：账户目录有 `gemini-pro-orchestrator`，显示名 Orchestrator (Carbon)。目前不属于已启用的 Bridge 自动候选；目录可见与生成、工具执行验收分开。

## 它与原生子任务的区别

官方 Orchestrator 在同一会话内由 Carbon 规划／复核、Hydrogen 执行，遇到问题再交回规划方。它不是把整条请求一次性分给另一个模型，也不是多个独立子会话；思考强度固定，`--effort` 不适用。Ultra 提供 Carbon 版本。参考：[官方型号与工作机制](https://antigravity.google/docs/models#orchestrator)、[多种协作方式的区别](https://antigravity.google/docs/dynamic-workflows)。

Bridge 已有另一种原生子任务：Codex 按本任务授权 reader、builder、reviewer 和槽位，Gemini 自主派遣并汇总；只开一层，子任务继承父任务精确模型／强度。当前不允许自行选择不同子模型，也没有启用 Swarm、Teamwork 或任意工作流。详情见 [原生子任务](native-readers.md)。Orchestrator 的模型交接不应被伪装成这些子任务。

## 现有代码需要适配的地方

1. **配置语义。** 现在任务以 model/effort 精确配对。组合执行须单独标记固定思考配置，提交时不传虚构 effort，卡片显示“Carbon 规划 · Hydrogen 执行／系统固定”。其资格由实时目录和显式策略记录，不从名称猜测，也不借用 Carbon High 的分数或成本。
2. **通道选模。** headless 应省略 `--effort`；ACP 当前 `select_model` 同时设置并验证 model 和 `thought_level`，须增加固定配置分支，仍确认实际选择。保留已有单模型通道。
3. **运行身份。** 工作区 hook 目前要求 `modelName` 等于任务精确型号；内部切换可能回报组合名称或实际组件型号，需先观察真实事件，再登记本组合允许的精确身份。目录、会话、任务、命令和撤权检查继续有效，不能去掉模型检查或放行任意 Gemini。
4. **状态与交付。** 保存组合选项与实际可观察的模型／阶段。服务端若未暴露内部交接，就显示“内部阶段未报告”，不能猜测正在由谁思考。一个逻辑任务、一张卡片，继续原有 review、幂等恢复、有限重试和会员路线。
5. **验证顺序。** 独立零生成 ACP 会话验证配置；随后在获准的真实 worktree 上验证实现、测试、一次失败后的修正及交付，再检查取消和续聊。新模式初期不同时开启原生子任务，避免混淆两套身份与额度记录。

本次在独立 ACP 会话中已实际选中 `gemini-pro-orchestrator` 并收到确认，选中后 `thought_level` 选项消失；没有发送 `session/prompt`、启动子任务或扩大工具权限。配置检查通过不等于以上执行链路验收通过。

## 值得优先试的任务

多文件前端改动、常规功能实现、局部重构，以及“先规划→实现→运行检查→修正”的完整交付最适合。纯润色、明确字段提取与快速查询直接选择 Carbon 或 Hydrogen 即可。高风险架构、科研结论和最终发布继续由 Codex 验收。
