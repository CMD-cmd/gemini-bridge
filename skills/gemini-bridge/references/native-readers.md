# Gemini 原生子任务

v0.13 已实测 reader、builder、reviewer 三角色及 concurrent 槽位依实际 Stop 复用；本次真实任务最大并发1，多槽位边界另有回归。按任务选择1–4个槽位，不因本地能力可用而无条件派遣。

有独立交付收益时，Codex 在新工作区 scope 选择角色与数量，例如：

```json
{"workspace":{"read_roots":["/绝对路径/任务资料"],"subagents":{"max_children":3,"roles":["reader","builder","reviewer"],"capacity_mode":"concurrent"}}}
```

`max_children` 最多 4，需本任务明确授权；省略 roles 保持 reader，旧 scope 不扩权。默认 `capacity_mode="lifetime"` 是任务总派遣额度；明确 `concurrent` 才按并发槽位管理，只有实际观察到 child Stop 才释放槽位。静默、父任务说完成或发送 kill 不释放槽位，也不证明云端已停。

父 Gemini 自行写提示、等待并汇总，走 headless；Codex 选择父模型／effort，子继承精确组合，只开一层。后台核对真实父子边、逐工具身份与目录，不以配置或模型自报代替证据。

| 角色 | 能力 | 交付 |
|---|---|---|
| reader | 读取／搜索授权源文件，向父回传 | 源位置与证据 |
| builder | 读取材料，仅在自己独占输出目录写文件／补丁 | 实际文件及改动 |
| reviewer | 只读检查，向父回传 | 具体问题、位置和依据 |

子任务不执行命令、不用 MCP、不再派遣、不联系其他任务。builder 不能编辑共享 worktree 或另一 child 目录；父任务整合补丁并运行获准命令／预览，reviewer 可检查真实结果。复杂后端、并发、同步恢复、安全与最终整合仍由 Codex 或合格原生执行者承担。

## 验收与后续

查看实际角色、读取／写入／回传与 Stop，核对 `native_children_validation`、源哈希和父产物。invoke 返回、消息发出与正常 Stop 是不同证据。卡片只展示已有本地活动，不推测思考或完成百分比。

取消撤销工具权限并停止本地执行；缺 child 远端停止确认时保留远端未知。v0.11 取消样本仅证明本地停止／撤权。带 child 的会话不使用 continue_job_id；后续用新 scope、新任务及必要的已验收产物。ACP 项目 MCP 与 headless 子任务仍分开授权，不恢复旧 scope。
