# 当前模型的能力与分工

核对日期：2026-11-14。本页是选模建议；精确型号、思考档位与资格以实时目录和生效策略为准。新型号不会自动继承旧型号的授权或实测结论。

## 这次调整了什么判断

Carbon 接替高档首选，Hydrogen 接替低档首选。Hydrogen 不再只承担摘录、改格式等简单工作，也适合边界明确的代码实现、调试、测试和界面迭代。Carbon 优先处理需要整体判断的写作、长文综合、视觉方案、规划和复核。上述分工来自公开证据与工作流需要，不是所有领域的绝对排名。

不沿用“Gemini 后端一概弱于当前 Codex”的旧概括：新模型的工程能力已有提升，但目前没有同一环境、同一档位、覆盖本项目的对照，不能据此宣布 Carbon 胜过或落后于某个 Codex 组合。复杂后端、数据库迁移、并发、同步恢复、安全与发布仍由 Codex 负责最终实现判断和验收；Gemini 可以交付隔离的实现或独立意见。

## 给任务选组合

| 维度／工作 | 优先建议 | 交接与验收重点 |
|---|---|---|
| 规则摘录、格式转换、局部定位、重复材料整理 | 低档 Medium；需要交叉核对时 High | 返回位置、完整字段和遗漏；保留原件 |
| 文案、英文表达、沟通和创意写作 | 重要内容用高档 Medium；复杂语气或多重约束用 High；普通短稿可用低档 Medium/High | 提供读者与风格样例；核对事实、承诺和语气，不能以总榜替代个人偏好 |
| 单篇阅读与证据表 | 低档 High 可承担明确字段提取；跨章节矛盾与多篇综合优先高档 Medium/High | 页码／段落、原文与结论分别列出；引用逐条核实 |
| 长上下文与科研推理 | 高档 Medium/High 优先；低档负责子问题和材料整理 | 上下文窗口不代表长文全记住；用关键事实、反例和跨段一致性验收 |
| 截图、图表、PDF／PPT 版式检查 | 高档 Medium；多页关联或细节约束用 High；局部首轮用低档 High | 指定页／位置；区分看到的内容与推断，不把图像理解等同生图 |
| 前端视觉、交互、动效与原型 | 高档 Medium/High 做方案和关键视觉；低档 High 做确定方案的实现、预览和修正 | 检查真实渲染、交互、空状态、响应式与数据行为；漂亮截图不等于功能完成 |
| 范围明确的实现、调试和测试 | 低档 High；多约束子问题可明确选 XHigh；架构与疑难复核用高档 High | 独立 worktree／授权目录，实际 diff、必要测试与失败日志；不因模型升级扩大权限 |
| 复杂后端、安全、并发和整合 | Codex 负责；高档 High 提供第二意见或隔离补丁 | 依据真实复现与检查作决定，不能用厂商安全专项结果代替本地权限或发布授权 |

低档从 Medium 起步，不默认使用 Low；XHigh 是有额外推理收益时的选择，不是“便宜模型一律开满”。高档 Low 可用于很小的语气修改。纯写作与阅读通常直接选单模型，避免无必要的规划／执行交接。

## 证据如何使用

**独立综合观测。** AA v5.0.1 当前页面分别为 Carbon High 60、Hydrogen High 51、Hydrogen XHigh 54；每任务平均耗时约31.1、8.1、14.3分钟。这是同版综合基准，不能当作本机任务耗时或会员扣费。它支持“Carbon 偏整体深度、Hydrogen High 偏执行效率、XHigh 有额外代价”的选择，不能推出中文文风或前端审美排名。Low/Medium 没有相同证据时保留未知，不能借用 High 分数。

来源：[Carbon High](https://artificialanalysis.ai/models/gemini-4-carbon)、[Hydrogen High](https://artificialanalysis.ai/models/gemini-4-hydrogen-high)、[Hydrogen XHigh](https://artificialanalysis.ai/models/gemini-4-hydrogen)。公开 v5.0.1 记录与原生角色冻结的 v4.3.2 上限分开保存，不混算或改写原生代理档位。

**厂商多维评估。** Carbon 的模型卡覆盖工程、知识工作、视觉、长上下文及科学推理；同表 GraphWalks 的 Carbon/Argon 为96.2/84.2，但这是特定图遍历任务，不能据此保证整篇论文阅读正确。Hydrogen 的模型卡把迭代实现、工具执行、视觉知识工作列为适用方向；其中科学终端任务并非各项领先。不同 Terminal-Bench 版本和测试配置不横向拼榜。发布演示中的界面成品也包含 Agent、工具和设计 Skill 的贡献。

来源：[Carbon 模型卡](https://deepmind.google/models/model-cards/gemini-4-carbon/)、[Hydrogen 模型卡](https://deepmind.google/models/model-cards/gemini-4-hydrogen/)、[Carbon 发布演示](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-4-carbon-now-available/)。安全专项计划的结果不默认代表普通会员路线具备相同能力。

**早期使用反馈。** 一位编辑评测作者在同一埋错文本上报告 Carbon 找到88/90处、Hydrogen84/90处；Hydrogen 换成一次性综合提示后为68/90。另一位用户喜欢 Hydrogen 的人物对话与故事连续性，同时认为复杂情节不如 Carbon/Argon。样本少、提示敏感，且都不是中文学术稿验证；仅用于提示“文风值得试、复杂要求要验收”，不作为硬分数或自动资格。

来源：[编辑错误检出测试](https://www.reddit.com/r/WritingWithAI/comments/1xveh4m/google_dropped_gemini_4_hydrogen_and_carbon/)、[创作体验](https://www.reddit.com/r/WritingWithAI/comments/1xv38jc/is_it_just_me_or_is_gemini_hydrogen_actually/)。两款模型刚发布，长期可靠性、中文风格和大型项目表现尚缺成熟独立样本。

**本 Bridge 的实测。** 本轮只完成 Carbon Medium 与 Hydrogen Medium 两条无私人材料的短文本生成，核对实际回报型号、卡片与返回内容。它证明连接与回传，不证明长文、复杂代码、视觉或子代理全链路已经在新型号上验收。后续在真实任务中补记录，不为凑样本重复调用。

## Orchestrator 与子代理

Orchestrator 是单会话的规划／执行协作模式，不等同多子代理。其思考强度固定，不能写成 Carbon High。固定配置、卡片、身份与续聊适配已实现，实际可调用范围以实时策略的运行资格为准。使用要求见 [Orchestrator](orchestrator.md)；已有单层子代理边界见 [原生子任务](native-readers.md)。
