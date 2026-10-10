# halfpike 退出定位（2026-10-10）

日志：`logs/gpt-5.4/halfpike_20261010_091851_419767.log`。

## 已确认的直接原因

前 23 次模型调用成功，第 24 次于 09:20:49（Asia/Shanghai）收到
TeamoRouter 接口返回的 HTTP 400 / `BadRequestError`，随后转换成 `exit_error`。
已知参考费用 $0.2418175，未触及 $0.8 预算。不是验旗失败、模型主动放弃，
也不是 Docker 连接失败或本地 Python 请求编码异常。

该次请求 ID：`58814ee1-3718-4c53-9840-efa0a3220ecd`。

当时错误记录只保存了状态码、异常类别、request ID，没有保存服务端
错误说明和错误码。刚补充的诊断字段不能追溯已经发生的错误，运行中的
Python 进程也不会自动加载补丁。

## 离线请求核对

已依据保存的 trajectory 和原配置 `Last5Observations` 重建请求。
先重建第 23 次成功请求，与 ledger 的 47 条消息、50188 字符精确一致，
再重建失败请求：49 条消息、51325 字符，完整请求 JSON 54922 UTF-8 字节。
角色/内容字段合法、UTF-8 编码正常、JSON 能严格序列化，没有意外控制字符。

同题此前有 53814 字符的成功请求；同批 maze 有 144216 字符、46262 输入
token 的成功请求。这不能证明中转站所有路由的上下文限制一致，但不支持
简单地将这次失败解释为统一的约 51 KB 请求体限制。

Callsite 与 halfpike 的两次 400 都发生在 `xxd` 输出加入下一次模型请求后：
前者 5439 字符输出，后者 1215 字符输出。两份消息都是正常的十六进制文本，
不是直接发送二进制。这个共同点值得服务商排查内容/网关处理，但不能仅凭
两次相关性认定有内容拦截，更不能据此修改原题工具或删掉解题内容。

## 仍缺少的证据与准备好的检查

需要取得具体的服务端错误说明，才能区分中转站/上游限制、路由错误或
请求内容兼容性问题。仅凭 HTTP 400 无法判定哪一方负有根本责任。

已准备将失败请求精确重放一次至原端点 `https://api.teamorouter.com/v1`，
沿用 `gpt-5.4`、输出上限 4096、reasoning=none、temperature=0、top_p=0.95。
不会重跑评测、不会执行模型返回命令、不会发送 API 密钥到请求消息中。

此重放被自动审批拒绝，因其涉及完整题目对话再次发送给中转站并可能产生费用，
需要用户明确批准。此次重放没有发出；当前仍只有离线核对证据。

离线数据：
`enigma_reproduction/generated/halfpike-request-diagnosis-20261010/offline-report.json`、
`enigma_reproduction/generated/halfpike-request-diagnosis-20261010/comparison-report.json`。
