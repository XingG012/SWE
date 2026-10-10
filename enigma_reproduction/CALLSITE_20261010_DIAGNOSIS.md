# Callsite / eMbrEy0 LEaK 日志核查（2026-10-10）

Callsite 的这次尝试因模型 API 返回 HTTP 400 而退出，不能作为一次正常的
解题能力失败。具体拒绝原因未被当时的代码保留，无法仅凭 400 判断是请求内容、
参数、服务商路由或服务商限制。

## Callsite 的证据

- 日志：`logs/gpt-5.4/Callsite_20261010_091000_033313.log`。
- 题目：`2019q-rev-call_site`，同一实验 `gpt54-fresh-20261010` 的三路并行批次。
- 前 13 次模型调用成功；第 14 次请求于 09:13:20（Asia/Shanghai）返回
  `BadRequestError` / HTTP 400，约 3.02 秒收到拒绝响应。
- 请求 ID：`735aebbd-6a33-4dfa-968e-11ecaadbd55c`。
- ledger 第 14 行的 `retryable=false`、`will_retry=false`、`attempt=1`：
  400 按当前策略直接退出，不是 4 次重试耗尽。
- 已知费用 $0.126009，预算 $0.8；不是预算用尽。失败请求缺少 usage，
  最终费用仍标记未知，不将已知费用冒充完整账单。
- 出错前的最后命令是读取二进制数据 `xxd -s 0xba000 -l 0x500 callsite`，
  命令返回之后，在下一次模型请求失败；不是该 shell 命令直接导致退出。
- 此次项目/网络独立，日志无清理错误。该 worker 随后启动了
  Gibberish Check、halfpike 等后续题，没有因 Callsite 停止整个批次。
- 最新结果还确认：同一 worker 的 Gibberish Check 已正式解出，费用
  $0.693112；halfpike 也因一次 HTTP 400 退出。两次 400 的原始说明均未保留。

原始记录：
`enigma_reproduction/generated/run-20261010T010959993324Z/2019q-rev-call_site-usage.jsonl`。

## eMbrEy0 的证据

- 日志：`logs/gpt-5.4/eMbrEy0_LEaK_20261010_090959_061855.log`。
- `exit_status=submitted`、`solved=true`，与官方环境验旗和 trajectory 记录一致。
- 43 步、45 次成功 API 调用（主模型 43 次，摘要 2 次），0 次 API 失败。
- 参考费用 $0.445841，计费完整，耗时约 334 秒。
- 与 Callsite 同一个三路并行批次，但使用不同网络和 Compose 项目。

## 结论与改动

这次 HTTP 400 没有提供必须串行的证据。并行批次中已验证至少两题成功，
但一次成功也不能证明服务商没有任何并发限制。OpenAI SDK 中 400 对应
BadRequestError，常见限流对应 429；第三方中转站可能采用不同的错误说明，
应以实际返回的具体说明为准。

已补齐诊断信息，保留安全脱敏、限长后的错误说明、错误码、参数名和 request ID，
并在日志头/结果加入 `last_api_error`。相关测试 79 项通过，没有为本次分析发起
模型请求或停止用户正在运行的批次。已运行的 Python 进程不会自动加载此补丁。

串行不需要修改代码，可在正常停止现有批次后运行：

```bash
bash enigma_reproduction/run_gpt54.sh --num-workers 1
```

沿用实验编号/模型配置/预算时，Callsite 这类 API 故障会重新尝试，
eMbrEy0 这类已完成成功题会跳过；不要加 `--from-scratch`。

参考：[OpenAI 错误说明](https://developers.openai.com/api/docs/guides/error-codes)、
[OpenAI Python SDK 文档](https://developers.openai.com/api/reference/python)。
