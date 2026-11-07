# 实时控制与观察恢复

v0.13 已验证运行中纠正、真实进展和断线后只读恢复。旧工具目录可用同服务受控客户端或只刷新当前聊天连接。

## 运行中纠正

`bridge_steer(job_id, thread_id, steering_id, text)` 向仍运行的同一 ACP session 发送增量纠正。它属于模型输入，不是零生成查询；不新建 Bridge job，不增加附件、域名、目录、命令或权限。终态任务按[续聊](acp-transport.md)处理。

同 steering_id 与相同文本幂等返回原记录，变更文本使用新 ID。状态区分 queued、sent、responded、rejected、unknown：sent 仅证明写出，responded 仅证明收到该协议请求响应，均不能写成“要求已应用”。是否采用核对最终文件。unknown 不换 ID 自动重发，先读原任务和控制记录。

## 中断后的观察恢复

先用原 job_id 查询 summary／status，短观察连接断开不代表 provider 断线。恢复丢失提交响应保留原 request_id 和完全相同请求。

`bridge_recover(job_id, thread_id, probe=false)` 读取本地已有上下文。仅确认原本地执行器已退出后，probe=true 才可使用原 session/cwd 的 session/load 观察上下文；不发送 session/prompt、不重放原提示、不恢复 scope、不改验收。同一会话或后续任务仍活动时继续观察最新任务；身份／进程证据不足时不强行探测。

返回保留 provider_execution_state=unknown、prompt_replayed=false。读回上下文不证明生成完成、云端空闲或取消成功；部分交付仍需核对。后续生成由 Codex 在有效授权下选续聊、新任务或[有限重试](operations.md#有限重试)，recover 不能自动重做。

## 实时进展

卡片从已有事件、受保护结果和进程记录显示当前动作、最近有效活动、工具调用、子角色和观察到的 Stop。前台活动任务约 2 秒刷新；后台或完成待验收约 15 秒。刷新不发消息、不调用模型，验收完成后停止不必要刷新。

本地 PID、文件时间、静默不是远端心跳；取消后缺 child Stop／远端 ACK 时显示本地取消、远端未知。只列实际活动，不展示思考正文、推测百分比或未发生步骤。
