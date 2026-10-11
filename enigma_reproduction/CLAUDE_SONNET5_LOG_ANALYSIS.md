# Sonnet 5 两题首轮退出分析（2026-10-10）

对象：`logs/claude-sonnet-5/A_Poem_For_You_20261010_190745_906553.log`
和 `logs/claude-sonnet-5/polly-crack-this_20261010_190745_910511.log`。

两题都在第一轮 API 返回后，因本地缓存用量校验报
`Flash cache usage invalid; stopping`，随后执行 `exit_error`。
它们没有执行解题动作，也没有触及 0.8 USD 预算阈值。
“Flash”是共用父类的错误文本；实际适配器和请求模型仍是 `claude-sonnet-5`。

两题 usage 账本分别记录 `InvalidCacheUsage`，缓存写入量为 2322 和 23096。
这表明接口已经返回了可解析的用量数据。没有密钥无效、分组无模型或 HTTP 错误的证据。
旧账本没有记录触发错误的命中值与输入总量，因此不能还原这两次的全部原始字段。
日志里的 token=0、已知费用=0 是校验失败后统计没有更新；实际费用为未知，不能解释为免费。

使用纯合成颜色、星期文本的新前缀请求复现：

```json
{
  "completion_tokens": 2,
  "prompt_tokens": 8930,
  "total_tokens": 8932,
  "prompt_tokens_details": {
    "cached_tokens": null,
    "cache_creation_tokens": 8930
  }
}
```

旧代码直接将 `cached_tokens` 交给整数校验，`null` 因而被判为非法。
原先三个极短请求没有覆盖首次缓存写入分支，不能证明真实题目输入的缓存兼容性。
证据：`generated/sonnet5-synthetic-cache-20261010T111254276004Z/fresh-prefix-report.json`。

修正仅涉及 Sonnet 适配器：命中值未报告时按普通输入单价估算上限，预算扣上限；
下限考虑剩余输入可能命中缓存的折扣。缓存写入费用仍计入，显式标记命中量未报告，
保存脱敏原始用量。负数、非整数、超范围等非法值仍停止。
已返回但本地校验失败的请求也明确记录 `request_succeeded=true`，避免误报成接口失败。

包含捕获用量的 SDK 对象回放、共享预算、故障续跑等测试共 89 项通过；随后新增脱敏
测试并重新跑 Sonnet 专项，17 项通过。修正适配器的真实合成请求返回 OK 并成功记账，
该次返回的是整数命中值；`null` 分支由捕获用量的离线回放验证，尚未重新跑这两题。
两题续跑预览均为待重试，正常执行原启动命令即可重新尝试。

一次重放原题目首轮提示的诊断请求被自动审批拒绝，原因是向第三方中转站外传完整
轨迹和提示词的授权范围；该请求未执行。后续接口诊断只发送纯合成文本。
