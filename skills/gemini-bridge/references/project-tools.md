# 任务专属 MCP

v0.13 已实测文件指纹、CSV统计、Crossref检索、公开全文获取、本地PDF与长文本读取。PubMed／Europe PMC本轮返回500／503，不能据接口存在声称当时检索成功；根据实际来源可用性选择工具。

新 scope 只选必要工具；联网工具另需精确 network_hosts，例如：

```json
{"workspace":{"read_roots":["/绝对路径/任务资料"],"project_tools":["literature_search","public_page","document_extract"],"network_hosts":["api.crossref.org","pmc.ncbi.nlm.nih.gov"]}}
```

提交用 workspace={}，由 auto 选可用 ACP，或明确 transport="acp"。只注入当前 session，不填写任意服务器、凭据、用户登录态或全局 MCP。旧 scope 不增加工具／域名，提交子集不能扩权，无网络任务省略 hosts。

| 工具 | 参数与结果 | 边界 |
|---|---|---|
| file_manifest | path；字节数、SHA-256 | 授权普通文件 |
| table_profile | path、可选 delimiter；记录／列数、缺失、有限数值统计 | UTF-8 CSV；表头不计行，NaN/Inf 不计有限值，缺失不补零 |
| literature_search | query、可选 limit（1–10）、source=crossref | Crossref 期刊文章元数据；链接不证明正文已取得 |
| pubmed_search | query、可选 limit（1–10）、source=pubmed 或 europe_pmc | 对应授权 eutils.ncbi.nlm.nih.gov 或 www.ebi.ac.uk；保留实际来源 |
| public_page | url；公开 HTTPS 页面文本、来源和哈希 | 请求／重定向逐次校验授权公开域名；不访问内网、登录态或任意下载 |
| document_extract | path、可选 start、limit、max_chars | 本地 PDF 页／DOCX 段落／PPTX 页／TXT、MD 行；最多 500 单元、60000 字符，按 truncated／next_start 续读 |

文档接口仅提取现有文本，不做 OCR、不执行宏、不加载外部资源。扫描 PDF、图片、排版和公式不能据文本提取声称完整检查。需要视觉证据时由 Codex 安排获准渲染与读图；引用文件版本、页码／段落／幻灯片位置，说明缺失范围。

## 文献任务与验收

区分检索元数据、摘要、实际读取全文和推断，记录查询、来源 URL／DOI、取得时间、阅读范围、证据位置与适用限制，再产出证据表和稿件。引文逐条对照实际来源，不据标题推断全文支持。公开全文失败时保留缺口，5xx／限额不盲目重复请求，不切 API-key 或付费访问路线。

核对 project_tool_validation 的真实调用、源／结果哈希、root conversation 与当前 session，以及 workspace_guard_verification 的审计匹配；模型解释另查。未调用或失败时不以口算／自报替代。结果冻结回收，结束／取消撤令牌并回收执行器；取消 ACK 与本地清理分开记。

headless 子任务与 ACP 项目 MCP 是不同路径，需要串联时交接必要的已验收产物，使用有效新 scope。v0.12 内部启动配置修复没有扩大源文件读取范围。

## 长返回与公开页面受限

有项目工具时，额外提供同任务的 `result_read(result_id, offset)`。长返回先完整保存为受保护回执，工具只返回短说明和游标；Gemini 按 next_offset 继续读到 null，不要求 Codex 转抄或用户拆文件。此工具只读当前任务已获准工具的成功回执，核验原始哈希和当前授权，不增加网络或目录权限。不要去读 `.gemini` 账户目录里的 CLI 转存文件。

`document_extract` 的 limit 最多500个原文单元。next_start 表示原文下一批单元；result_read 的 next_offset 表示本批回执文本下一段，两者不能混用。先读完当前回执，再按 next_start 继续，记录单元覆盖范围后才声称全文已读。

站点返回验证页、403或其他获取失败时如实说明，不把 HTTP200 的验证页当正文，不绕过验证。已有获准使用的原文快照可作为新任务输入，保留来源URL、获取时间和哈希，明确它是保存的副本。
