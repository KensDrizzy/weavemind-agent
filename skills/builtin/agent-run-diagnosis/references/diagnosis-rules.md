# 诊断规则和证据边界

| 类别 | 可观察证据 | 仍需核对 |
| --- | --- | --- |
| retrieval_failure | 检索工具明确返回无匹配 | 索引范围、资料是否存在、查询是否准确；不是召回率评测 |
| tool_failure | 异常、退出码、已知工具错误文本、审批拒绝 | 参数、依赖、权限或服务问题；拒绝不等于权限策略错误 |
| planning_failure | 计划生成错误、任务失败、依赖跳过 | 失败源是否在工具；依赖传播不等于 Planner 出错 |
| closing_failure | 正常返回时无最终答复或待执行工具请求 | 上限、空响应和路由；成功措辞与失败共存只算待验证候选 |
| repeated_calls | 同工具、同参数摘要至少三次 | 轮询是否有进展；脱敏和截断可造成同签名 |
| model_failure | 模型回调报告异常 | 后续重试是否已恢复 |
| interrupted | 取消或运行异常 | 用户主动取消、异常、执行限制分别处理 |

## 轨迹字段

每行一个 JSON 事件：schema_version、run_id、session_id、seq、timestamp、elapsed_ms、event、data。

- run.start/end：目标、模式、停止原因、最终可见答复摘要；task_success 始终为 unknown，等待验收。
- chain/llm/tool.start/end：span_id、parent_span_id、耗时、结果状态。llm 只记录调用元数据和可用 usage，不保存提示词或思维内容。
- tool.start：工具名、脱敏参数、签名；tool.end：结果摘要和 returned/error/empty。returned 只表示工具返回。
- hook.Plan*、task.start/end/skipped/replayed：关联计划及执行记录。
- graph.config：checkpoint thread_id，可与 task_records 的 act:thread_id 或 plan_id 对照。
- run.stop：迭代上限、浏览器循环或停滞检测。

## 限制

文件为本地 JSONL，逐事件刷新，写入失败不阻断 Agent。max_events 超限后停止收集普通事件，但仍记录 run.end 和 dropped_events。没有 run.end 可能是尚在运行或进程退出，也可能是日志不完整。

参数、目标和结果仅做常见凭证脱敏与长度限制，并非通用敏感数据识别。轨迹不应直接公开。图片和模型思维不采集，资料正文摘要仍可能含业务信息。

图回调覆盖 ReAct、Plan、Team，以及正确传播回调上下文的子 Agent。图调用之前的 CLI 意图快捷路径、复杂度分类和直接执行的 CLI 命令不在本次 run 范围内。部分第三方工具内部重试不会单独上报。超时后仍在后台执行的任务，其父运行关闭后的事件不会继续写入。

脚本规则只给出候选和证据。语义上的“答非所问”“检索结果不相关”需要读取结果和目标判断；它们不是确定性脚本能可靠裁决的结论。
