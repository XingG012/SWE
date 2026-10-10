# 当前测试：GPT-5.4 / TeamoRouter

主智能体和摘要模型均为 **`gpt-5.4`**，端点 **`https://api.teamorouter.com/v1`**，
凭据读取 `TEAMOROUTER_API_KEY` 或权限 0600 的 `.secrets/teamorouter_api_key`。
每题预算 **1.0 USD**，主/摘要/格式纠错共用；默认三题试跑整批预算 3.0 USD，
可读日志写入 **`logs/gpt-5.4/`**。

当前只测试 DeepSeek 正式成功日志对应的 **82 道唯一题目**，名单固定于
`gpt54_deepseek82_tasks.json`，每题附带成功日志和结果文件证据。
GPT 已成功的 Collision Course、DockREleakage、Gibberish Check 在名单内，
沿用成功结果并跳过；maze、sfc 已在 $0.8 下达到预算上限失败，也跳过。
当前实际待测 **77 题**；GPT 另已解出的 eMbrEy0 LEaK
不在这 82 题中，保留结果且不运行。三路分配为 26、26、25 题。

新实验编号 `gpt54-deepseek82-20261010`，固定名单明确沿用已成功的 3 题，
并排除之前 $0.8 下已失败的 2 题。手动中断的 The Big Bang 仍待运行。
新实验后续同命令续跑时跳过
已完成题，重试中断/API/基础设施故障题。没有删除旧日志或结果。
名单固定为用户指定的 82 题，不额外纳入早期试跑中成功但没有对应正式成功日志的题。

沿用当前消息、示例与命令格式，通过 OpenAI 兼容 Chat Completions 调用；发送
`max_completion_tokens=4096` 和 `reasoning_effort=none`（官方默认），
保留温度 0、top_p 0.95。需要其他推理档位时适配器自动省略不兼容的采样参数。

费用按 [GPT-5.4 官方参考价格](https://developers.openai.com/api/docs/models/gpt-5.4)
估算：每百万 tokens 输入 2.50 USD、缓存输入 0.25 USD、输出 15.00 USD；
超过 272000 输入 tokens 后，该会话按输入 2 倍、输出 1.5 倍估算。
中转站实际价格尚未取得，**预算停止阈值与费用均为参考价格估算，不代表中转站账单**。
ledger 标记 `pricing_accounting=official_rate_reference_estimate`，沿用响应后预算检查。

2026-10-09 验证：最小真实请求成功，返回模型 `gpt-5.4-2026-03-05`，输入 7 tokens、
输出 10 tokens，`finish_reason=stop`；适配器实际发送的 `reasoning_effort=none` +
`max_completion_tokens=4096` + 温度/top_p 请求格式被接受。52 项离线测试通过。
此前 x1api 端点对 `gpt-5.4` 返回 HTTP 404（该分组不支持此模型），已切换至 TeamoRouter。
诊断汇总：`generated/gpt54-connectivity.json`。

数据、六类官方提示词、示例、工具、评分与固定 Docker 镜像沿用现有实现。
续跑按实验编号、选题名单、模型、端点、采样口径和预算筛选，旧模型结果不会导致 GPT-5.4 跳题。

```bash
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --settings enigma_reproduction/gpt54_settings.json --preflight
# 只预览名单、已成功/已失败跳过、剩余数量和预算；不调用模型、不启动 Docker
bash enigma_reproduction/run_gpt54.sh --dry-run
# 用户手动启动：仅 82 题范围内的待测题，默认三路并行，每题 $1.0，整批 $82
bash enigma_reproduction/run_gpt54.sh
```

2026-10-10 本次配置验证：选题预览 82/跳过 5（成功 3、旧预算失败 2）/剩余 77，单题 $1.0、整批 $82、
三路并行；Docker/凭据/固定镜像/选题 SHA256/数据集一致性预检查通过，
95 项相关测试通过。本次仅验证配置，0 次模型调用，没有启动解题。
预览证据：`generated/selection-preview.json`；配置验证：
`generated/gpt54-deepseek82-verification.json`。

## 2026-10-10：并行隔离、API 恢复与预算规则

新版运行器为每次题目尝试创建唯一 Compose 项目和 Docker 网络。原题的服务域名、
容器内部端口、附件和评分保持原样；生成私有运行配置，取消宿主机端口发布和固定容器名，
并为题目内部网络与命名卷使用独立名称。Compose 启动必须成功并通过运行/健康状态等待，
agent 接入后核对题目域名只解析到本次项目的服务。结束时只清理本次资源。
这些措施适用于当前支持的各模型。Docker 的外部网络名称不会自动受 project 隔离，
所以不能仅加 `-p`；见 [Docker 网络文档](https://docs.docker.com/reference/compose-file/networks/)。

`Triathlon or Sprint` 包含宿主机网络的转发服务，保持原题实现并使用独占锁；普通题共享锁
并行运行，该特殊题开始时等待其他题退出，结束后继续并行。此锁覆盖使用新版运行器启动的
各模型批次。旧程序没有使用该锁，不应与新版宿主机网络题同时运行。

模型接口默认最多尝试 4 次（首发 + 最多 3 次重试），仅重试连接异常及 HTTP
408/409/429/5xx，使用指数退避和随机抖动。400/401/403/404 等错误直接报告。
SDK 内部重试继续关闭，避免无法统计的隐式重试；逐次 ledger 保存异常类型、底层异常类别、
HTTP 状态、可用的 request ID、尝试次数和已知/未知费用。错误分类参考
[OpenAI Python SDK](https://github.com/openai/openai-python#retries)。
2026-10-10 针对 Callsite 的 HTTP 400 补充了错误诊断：只提取错误码、参数名、
类型和最多 500 字符的脱敏错误说明，不记录完整响应正文或请求头；同时写入
usage ledger、可读日志与结果中的 `last_api_error`。400 仍不自动重复请求。
补丁在下次启动生效；旧记录中没有保存的服务端说明无法追溯恢复。
连接尚未建立且请求未发送的失败记 0 费用；已发送但响应中断的请求保留未知费用，
恢复成功后也不会把未知费用抹掉。已收到 usage 的响应按官方配置正常计费。
重试耗尽的 API 故障不会被续跑永久跳过；普通解题失败仍保持跳过。

`run_config.json` 现在保存隔离/重试协议版本，续跑还检查价格和重试配置。
旧共享网络批次不作为新版已完成题，原日志保持保留。自动补跑基础设施故障应与有效的
单次解题尝试区分，历史费用仍保存在各次尝试记录中，不把重试成本删除。

预算按用户要求继续使用官方参考价格，主模型、摘要和格式纠错共同计费。
**下一批单题预算 = 相同模型和配置下、计费完整的成功题平均费用 × 2**。
失败题费用照常保留在总费用中，成功均价只统计成功题；一批开始后不自动改变阈值。
本次按用户要求固定每题 1.0 USD；成功均价的两倍仍作为后续建议，不自动覆盖本次预算。
摘要花费也包含在成功均价内。

2026-10-10 的完整集旧实验编号为 `gpt54-fresh-20261010`；当前已换为
`gpt54-deepseek82-20261010`，沿用的 3 道已成功题在固定选题文件中明确记录。
其余旧结果不参与这次进度；同编号、同预算中已完成题仍可用于断点续跑。
以后彻底重测时使用新的 `--experiment` 编号，不必删除日志。

手动 Ctrl+C 或向父进程发送 SIGTERM 时，父进程通知各 worker 保存中断状态并清理资源。
每题结果原子写入 `results.json`，随后重跑同一命令即可跳过已完成题、重试中断题及
API/基础设施故障题；恢复到题目级别，中断题从头解，不恢复模型对话或容器执行现场。
单题报错记录后继续下一题，预算用尽则停止。不要在续跑时加 `--from-scratch`。
SIGKILL、断电或磁盘故障无法承诺正常清理；缺少完成记录的题仍会重新尝试。
同一实验的顶层启动器有互斥锁，避免重复启动造成双重解题。
结果先保存，再更新可读日志；日志收尾错误不会丢失断点或阻止下一题，
过程日志缺失时恢复最终指标并明确标记缺失。
正常停止最多等待 45 秒，随后强制停止未退出的 worker。
整批 `--total` 是每次启动的预算，历史费用保留，不会因重启被删除。

```bash
# 当前 GPT：三路并行；整批预算需显式指定
.venv-enigma/bin/python enigma_reproduction/run_nyu.py \
  --settings enigma_reproduction/gpt54_settings.json \
  --run --num-workers 3 --per-instance 1.0 --total 82

# 等价快捷入口：首次运行仅选 DeepSeek 成功集并排除 GPT 已成功题；后续运行自动续跑
bash enigma_reproduction/run_gpt54.sh

# 将来再次彻底重测：换实验编号；正常续跑沿用该编号
bash enigma_reproduction/run_gpt54.sh --experiment gpt54-another-experiment

# 有成功结果后，查看均价和下一批预算建议，不会调用模型或启动 Docker
.venv-enigma/bin/python enigma_reproduction/run_nyu.py \
  --settings enigma_reproduction/gpt54_settings.json --budget-report

# 后续换模型，传对应的配置文件，不必覆盖 settings.json
# --settings enigma_reproduction/deepseek_settings.json
# --settings enigma_reproduction/gemini_settings.json
# --settings enigma_reproduction/claude_settings.json
```

真实 Docker 隔离验证同时启动 `sfc` 和 `Gibberish Check`：两题都通过
`rev.chal.csaw.io:8000` 访问，各自分别返回 `WELCOME` / `Find the Key!`；
清理 sfc 后另一题仍可访问，全部测试资源已清理，0 次模型调用。
证据：`generated/isolation-smoke-20261010T005232097426Z/report.json`。
可用 `.venv-enigma/bin/python enigma_reproduction/check_isolation.py` 重复这项无模型验证。
2026-10-10 相关离线测试 77 项通过，包含真实子进程中断后的 worker 退出、
中断题落盘、单题异常后继续下一题、旧实验排除、重复启动互斥和原子写入失败恢复。

下面是 Claude Sonnet 5、Gemini 和 DeepSeek 的历史配置与验证记录。旧模型使用各自
保存的 `claude_settings.json` / `gemini_settings.json` / `deepseek_settings.json`；
历史日志和原始结果保留。

# 历史测试：Claude Sonnet 5 / x1api

主智能体和摘要模型均为 `claude-sonnet-5`，端点 `https://x1api.top/v1`，
凭据读取 `X1API_API_KEY` 或权限 0600 的 `.secrets/x1api_api_key`，使用用户
已切换分组的原密钥。每题预算 1.4 USD；默认三题试跑整批 4.2 USD；
可读日志 `logs/claude-sonnet-5/`。

沿用 OpenAI 兼容 Chat Completions，输出限制 4096 tokens。按
[Sonnet 5 参数说明](https://platform.claude.com/docs/en/models/sonnet-5/overview)
请求省略自定义温度和 top_p，使用服务端默认采样及自适应思考，记录
`sampling_mode=provider_default`、`sampling_parameters_sent=false`。

费用按 [Claude 官方参考价格](https://platform.claude.com/docs/en/about-claude/pricing)
估算：输入 2 USD、缓存读取 0.20 USD、输出 10 USD/M tokens；报告缓存写入时按
5 分钟 2.50 / 1 小时 4 USD 记录范围。中转站实际价格尚未取得，预算与费用为参考估算。

2026-10-09 验证：52 项离线测试通过，最小真实请求成功返回 `OK`（`claude-sonnet-5`，
输入 28 tokens、输出 4 tokens，约 17.68 秒）；用户授权后完成一次 ShreeRamQuest
首轮输入对照（返回 `claude-sonnet-5`，未拒答，首个动作 `curl -i http://web.chal.csaw.io:80`，
仅记录未执行）。诊断：`generated/sonnet5-connectivity.json`、
`generated/sonnet5-ctf-check-20261009T141415151176Z/report.json`。

# 历史测试：GPT-5.4 / x1api（早期端点，现已切换 TeamoRouter）

主智能体和摘要模型均为 `gpt-5.4`，每题预算 **0.8 USD**（主/摘要共用），
默认三题试跑整批预算 2.4 USD，可读日志写入 **`logs/gpt-5.4/`**。
早期端点 `https://x1api.top/v1`，凭据读取 `X1API_API_KEY` 或权限 0600 的
`.secrets/x1api_api_key`。现已按用户要求切换至 TeamoRouter（见顶部当前配置）。

2026-10-09 22:02（Asia/Shanghai）分组切换后复核：模型列表仍无 `gpt-5.4`，
实际最小生成请求仍返回 HTTP 404；此前 43 项离线测试通过。服务商错误：
`Model "gpt-5.4" is not supported by any configured account in this group`。
当前密钥所属分组缺少该模型的支持，尚未验证 GPT-5.4 的生成或 CTF 响应。
诊断位于 `generated/gpt54-verification-*/report.json` 与 `logs/gpt-5.4/API验证_*.log`。

请求沿用 EnIGMA 消息和命令格式，使用 Chat Completions；发送
`max_completion_tokens=4096` 和 `reasoning_effort=none`（官方默认），
保留温度 0、top_p 0.95。需要其他推理档位时适配器自动省略不兼容的采样参数。
模型、端点、推理档位和每题预算不同的历史结果不会被当前续跑跳过。

费用暂按 [GPT-5.4 官方参考价格](https://developers.openai.com/api/docs/models/gpt-5.4)
估算：每百万 tokens 输入 2.50 USD、缓存输入 0.25 USD、输出 15.00 USD；
超过 272000 输入 tokens 后，该会话按输入 2 倍、输出 1.5 倍估算。
中转站实际价格尚未获取，**预算停止阈值与费用均为参考价格估算，不代表中转站账单**。
ledger 标记 `pricing_accounting=official_rate_reference_estimate`，沿用响应后预算检查。

数据、类别配置、示例、工具、评分与固定 Docker 镜像沿用现有 EnIGMA。

```bash
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --pilot
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.8 --total 160
```

以下是 Gemini 和 DeepSeek 的历史配置与验证记录。旧适配器使用各自保存的
`gemini_settings.json` / `deepseek_settings.json`；历史日志与原始结果保留。

# 历史测试：Gemini 3.6 Flash / TeamoRouter

主智能体和摘要模型均为 `gemini-3.6-flash`，使用
`https://api.teamorouter.com/v1` 的 OpenAI 兼容接口。用户给出的 Google GenAI
示例可用于原生 Gemini 协议；这里沿用 SWE-agent 现有消息格式和已安装的 OpenAI SDK，
不向 Gemini 发送 DeepSeek 专用的 `thinking` 字段。
接口参考：[TeamoRouter 接入文档](https://api.teamorouter.com/docs/api-integration)。

- 每题预算：**0.8 USD**，主智能体、摘要与格式纠错共用。
- 默认三题试跑整批预算：**2.4 USD**。
- 可读日志：**`logs/gemini-3.6-flash/题目名_时间戳.log`**。
- 凭据：环境变量 `TEAMOROUTER_API_KEY`，或权限 `0600` 的
  `.secrets/teamorouter_api_key`；入口自动读取该文件。密钥不写入配置、轨迹或 Git。
- 断点续跑按模型、端点、采样配置和每题预算筛选历史批次，Gemini 不会跳过 DeepSeek
  已做过的题目。生成数据仍在 `enigma_reproduction/generated/run-*` 中，
  `run_config.json` 和每题结果记录模型与计费口径。
- 数据、六类提示词、示例、工具、评分及 Docker 镜像沿用当前 EnIGMA 实现。

```bash
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --preflight
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --pilot
# 全测试集 200 题；显式填写本次整批预算
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.8 --total 160
# 需要并行时追加 --num-workers 4
```

配置价格参考 [TeamoRouter 公示价格](https://api.teamorouter.com/zh)：每百万 tokens
输入 0.30 USD、缓存输入 0.03 USD、输出 1.50 USD。中转站折扣会变化，配置是核对时的
价格快照，**费用与预算停止阈值按该快照估算，并非服务商精确账单或保证上下界**。
为兼容现有分析，保留 `cost_upper_usd` / `cost_lower_usd` 字段名；Gemini 的两组价格
相同，ledger、结果和汇总通过 `pricing_accounting=router_rate_snapshot_estimate` 标明口径。
主模型和摘要均使用响应中的完整 `completion_tokens`，不重复加推理 tokens。
预算沿用响应后检查，会超出阈值至多一个在途请求的费用；无有效 usage 时记为未知，
结束该题。沿用当前批次行为：单题接口错误记为已完成失败，继续后面的题目。

2026-10-09 21:42（Asia/Shanghai）充值后验证：最小真实请求成功返回 `OK`，
返回模型为 `gemini-3.6-flash`，输入 268 tokens、输出 40 tokens，耗时约 9.20 秒，
配置价格估算费用 0.0001404 USD，usage 计费数据有效。Docker/官方镜像/配置预检通过。
此前 37 项离线测试通过；尚未启动正式题目。先前余额不足记录保留，当前验证汇总位于
`generated/gemini-connectivity.json`，逐次诊断位于 `generated/gemini-verification-*/`，
可读验证日志位于 `logs/gemini-3.6-flash/`。

旧 DeepSeek 模型适配仍可读取 `deepseek_settings.json`，历史日志和结果保留。
本节以下是此前 DeepSeek 实验的历史说明，其默认模型、预算与命令不适用于当前 Gemini 配置。

# EnIGMA / DeepSeek Flash 复现准备

当前项目根目录是官方 SWE-agent v0.7，基线 commit 为
`084d5d164729ddcbe8fbc05877ead403549ef669`，工作分支为 `enigma-v0.7`。
原项目 `origin` 是 `https://github.com/XingG012/SWE.git`，原 `main` 的
`40ca3f37b159dd65eb538a4f0a9934ce57563f26` 已保留；`upstream` 指向官方仓库。
切换前工作区没有已跟踪文件改动。复现适配主体已提交为
`b520e6c9f8a1b2e7d4a1e52aa3a20da0921a5caf`，它的父提交就是上述官方 v0.7 基线。
本次启动预检修复及文档更新保留在工作区，未新增提交。

依据：[ICML 2025 正式论文](https://proceedings.mlr.press/v267/abramovich25a.html)、
[官方 v0.7 源码](https://github.com/SWE-agent/SWE-agent/tree/v0.7)、
[v0.7 EnIGMA 文档](https://swe-agent.com/0.7/usage/enigma/)。
正式 PDF SHA256：`ef4914f8452197ab3123d4d014a5c7e618476cfca552f628b613f8fdb71d5b89`。
没有修改论文，也没有启动其他模型、Cybench、Intercode 或 Ours。

## 实验对应关系

| 项目 | 本次准备 |
|---|---|
| 运行实现 | 本项目中的官方 SWE-agent 0.7.0；不使用新版实现 |
| 数据 | `/home/xing/.nyuctf/v20250206/test_dataset.json`，测试划分与元数据未修改 |
| 测试口径 | NYU 测试集 200 题，pass@1；3 题试跑不代表全测试集结果 |
| 主提示词与示例 | 按类别直接加载 `config/ctf/ctf_{category}.yaml` 和官方开发集 demonstrations |
| ACI / 交互工具 | 原文件编辑、搜索、提交、反编译、反汇编、debugger、server connection、forfeit |
| 历史 | 官方 `Last5Observations`；保留思考与原历史处理逻辑 |
| 摘要 | 官方 `LMSummarizer`，105 行阈值；保留 200000 字符及 xxd/hexdump/strings 的 SimpleSummarizer 回退 |
| 采样 | 论文附录 C.2：温度 0、top_p 0.95；配置仍发送这两个值 |
| 停止 | 官方 submit/forfeit、格式失败、上下文失败、API 错误、成本限制；未添加 Ours 的回合限制或解题策略 |
| 评分 | 官方 `SWEEnv.validate_submission`，保留原精确匹配及 `flag{...}` 包装规则；只把 `exit_status == submitted` 计为成功 |
| 容器 | 官方 `sweagent/enigma`，解析后按不可变 image ID 运行；不用 `ctfenv` 或 `ctfenv:multiagent` 替代 |
| 模型 | 主智能体和摘要均为 `deepseek-flash`；仅 OpenAI 兼容端点 `https://api.deepseek.com` |
| 当前预算 | 正式运行及新试跑均为每题 0.1 USD；正式整批命令为 20 USD，默认三题试跑整批为 0.3 USD |

三题真实试跑已完成：2 题成功，1 题格式停止，总费用上界约 0.153 USD。
详见 [试跑结果](RESULTS.md)。历史诊断试跑使用每题 3 USD / 整批 9 USD；随后按用户要求，
当前正式运行和新试跑默认每题 **0.1 USD**。正式整批预算必须通过 `--total` 明确指定。
没有将两道成功样本的成本推广为整个测试集的平均成本。

论文表 6 为 crypto 53、forensics 15、pwn 38、rev 51、misc 24、web 19。
本地为 crypto 52、forensics 15、pwn 39、rev 51、misc 24、web 19，总数同为 200。
接受这一分类差异，没有改动本地类别、补造样本或排除题目以迎合论文统计。
本地数据仓库 commit：`1dc13a0dc41a71504f727649679e2b5a6d0cb1b1`。
测试 manifest SHA256：`aefc52210cb7f15509d82f3ee4b163df6fc331ee446a0977bca1db5fd99fa0a4`。
`2023f-for-forensings` 原来缺少附件 `forensings.zip`。已按该题提供的公开 Google Drive 链接
补齐，大小 436127134 bytes，验证 ZIP 格式，没有执行或解压内容。
附件 SHA256：`d7754455afabf7d9778022a4f963e1e0ba81f9a003323458a52e33eb33268d89`。
这是数据目录唯一新增的文件；没有覆盖已有文件、修改题目 JSON、改变测试划分或类别。
重新核验全部测试题所声明的附件均存在。

`/home/xing/llm_ctf_automation_qwen_3/` 可访问。只读查看模型接口与预算参考：
原 baseline 的默认成本上限为 10 USD，部分配置输出上限为 4096 tokens；没有导入其提示词、
规划器、执行器、日志、方法配置或工具。历史诊断试跑每题 3 USD 来自 EnIGMA 论文；当前每题 0.1 USD 由用户指定。

## 模型与计费差异

2026-10-08 核对的 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)
将 `deepseek-flash` 对应到 V4.1 Flash，上下文为 1M。这个在线别名可能变化，
usage ledger 会记录服务返回的模型标识；这不能替代服务商提供的固定权重版本。

每百万 tokens 的 USD 价格：

| 时段 | 缓存命中输入 | 缓存未命中输入 | 输出 |
|---|---:|---:|---:|
| Peak | 0.006 | 0.30 | 1.20 |
| Off-peak | 0.003 | 0.15 | 0.60 |

`APIStats` 和预算使用 peak 价格计算的**保守费用上界**，同时记录 off-peak 费用下界。
费用不是服务商账单的精确值；未自行猜测中国节假日或请求跨时段的计费规则。
主调用、摘要调用、格式纠错调用都经过同一适配器，缓存命中分别计价。
输出费用使用完整 `completion_tokens`，其中已包含推理 tokens 时不再重复相加。
价格缺失、非正价格或 token usage 无效时停止，费用标记为未知，不按零费用继续。

沿用 v0.7 的响应后成本检查，并增加发请求前检查当前累计成本；因此预算是停止阈值，
可能超出阈值一个在途请求的费用。主智能体和摘要共用预算口径，整批也累计所有已知费用。
未知计费结果会停止整个试跑，需与服务商账单核对后再启动新批次。
4096 tokens 是模型输出预算限制，主调用和摘要一致，且已记录在 settings 中。

根据 [DeepSeek 参数文档](https://api-docs.deepseek.com/guides/thinking_mode/)，
思考模式默认开启且温度不生效；非思考模式的 top_p 固定为 1。
此次显式使用 `thinking=disabled`，保留温度 0 的请求设置；虽然仍传论文的 top_p 0.95，
服务端实际忽略它。无法把该差异称为完全相同的采样条件。没有把独立 reasoning_content
插入 EnIGMA 的讨论、提示词或历史，也没有换成原生 function calling。

## 已记录补丁及影响

1. `sweagent/agent/deepseek_flash.py` 和 models 的模型分派：只增加 Flash 接口、非零价格、
   token/cache usage 检查、主/摘要共用预算、私密数值 ledger。继承 v0.7 的消息与示例构造。
   关闭 SDK 自动重试，避免网络结果不确定时发生无法核算的重复付费；认证/API 错误脱敏并停止。
2. `agents.py`：摘要检查主智能体累计预算；摘要发生成本或接口错误时先保留已知花费，
   再通过原环境 `exit_cost` / `exit_error` 终止。正常摘要仍按原路径合并统计，不重复计费。
3. `summarizer.py`：修正字典形式 model 配置的属性拼写；每题清空摘要历史，避免跨题累积
   system 消息；失败时也清理本次 user 消息。Flash 的计费/API 错误不被原 catch-all 吞掉。
   摘要提示词、阈值、输出保存、回退机制均保留。
4. `utils/secrets.py` / `utils/log.py`：输出前脱敏环境与注册凭据、认证头及 traceback。
   模型输入出现凭据时阻止请求，模型返回的凭据在进入轨迹前脱敏。
   每题结束后关闭该题的日志 handler，防止后续题目日志重复写入前一道题的文件。
   `utils/config.py` 将可选 `keys.cfg` 不存在的提示降为 debug；使用环境凭据时这不是错误。
5. `.gitignore` / `.dockerignore`：私密文件、环境、生成结果和 Word 不进入 Git；
   Docker 构建上下文只允许官方 Dockerfile 需要的 `docker/` 文件。
   `environment/utils.py` 在启动题目 Docker Compose 时过滤凭据环境，防止 Compose
   变量插值意外把宿主端模型密钥传给题目服务。
   另为官方 `image_exists` 增加 image ID/digest 查询兼容；原 Docker reference filter
   只匹配标签，会把已存在的固定镜像 ID 误判为不存在。没有改变镜像内容或容器工具。
6. `run.py` 增加可选日志路径；默认官方 CLI 行为保留。NYU 入口为正式与试跑统一指定
   `logs/deepseek-flash/题目名_时间戳.log`，不再另建一份 run 日志。`task_log.py` 在文件开头
   写入指标，结束或 Ctrl+C 时在日志 handler 关闭后原子更新，按块保留已经写入的过程。
   `metrics.py` 的被动 hook 记录每步模型响应、动作、观察及指标；不改写 agent 状态。
   文件名去除路径与非法字符，时间戳采用 Asia/Shanghai 并包含微秒，重复运行保留旧日志。
   日志目录权限 0700、文件权限 0600，仍使用凭据脱敏过滤。
7. `run_nyu.py` 的启动预检接受基于固定 v0.7 的已提交适配补丁。原先要求 HEAD
   精确等于裸 v0.7 提交，会在提交复现补丁后错误拒绝运行。现在验证基线祖先关系，
   并对未适配的官方 Python 文件、配置、示例与 Dockerfile 做基线差异检查；
   仅放行已记录的模型、计费、兼容与日志适配文件。预检及批次配置记录实际运行 commit 与工作区差异 SHA256。
   此修复只改变来源核验，不改变解题方法、预算或评分。
8. `environment/utils.py` 将 `docker network connect ctfnet` 的等待时间从错误复用
   容器启动轮询的 1 秒，改为独立的 60 秒超时，适应 WSL 上的 Docker 网络创建延迟。
   改用 `subprocess.run(check=True)`，失败退出立即报基础设施错误，超时后回收 CLI 子进程，
   传入过滤凭据的环境，错误诊断脱敏。没有自动重试，不改变网络、工具或模型调用。
   原容器轮询延迟、题目动作执行超时、模型预算与停止条件没有改动。
9. 依赖：独立 `.venv-enigma`，不修改系统/用户原 Python 环境。
   `swebench` 2.x 导入依赖已删除的 `modal.Mount`，固定 Modal 0.64.235 修复导入。
   不调用 Modal 云端服务。`constraints.txt` 记兼容约束，`requirements.lock.txt` 记录实际完整版本。

官方六类 YAML、demonstrations、command 文件、交互会话定义和 Dockerfile 没有修改。
离线测试位于 `tests/test_deepseek_flash_reproduction.py`；测试使用假凭据和模拟响应，不调用模型。
本次启动检查及网络兼容修复后共 26 项离线测试通过，覆盖正式与试跑入口、单题日志、最终指标、
中断收尾、跨题隔离、脱敏，以及已提交补丁放行、官方方法改动拦截、网络超时与连接失败的停止和脱敏。
Docker 服务已恢复，固定官方镜像的 conda 与 CTF 工具再次通过离线容器检查；
随后对正式首题 `2021f-rev-maze` 按入口解析后的固定 image ID 验证了完整
`SWEEnv.reset(0)`，确认 ctfnet 已接入、附件与依赖准备完成、命令通道可用；
全程没有实例化智能体、调用模型或解题，验证容器已删除。
证据位于 `enigma_reproduction/generated/environment_smoke.json`。
启动入口仍会检查 Docker、固定镜像、凭据与配置。

## 操作

在项目根目录运行。环境已安装；重新安装可用：

```bash
.venv-enigma/bin/python -m pip install -r enigma_reproduction/requirements.lock.txt
.venv-enigma/bin/python -m pip install --no-deps -e .
```

凭据读取环境变量 `DEEPSEEK_API_KEY`，或权限 `0600` 的 `.secrets/deepseek_api_key`。
不要在命令行参数或示例中填写真实密钥，不需要把密钥再次发送到聊天。
该文件只由宿主端读取；解题容器没有凭据环境变量、项目根目录挂载或凭据附件。
官方代码只复制 challenge 中列出的附件，`challenge.json` 仅在宿主端用于元数据及评分。

```bash
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --prepare
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --preflight
.venv-enigma/bin/python -m pytest -q tests/test_deepseek_flash_reproduction.py
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --pilot
```

启动前要求 Docker daemon 可连接、官方镜像可用、凭据私密、六类配置可加载。
入口不满足条件会直接停止，不创建空数据目录，不使用 Ours 镜像降级。
首次可 `docker pull sweagent/enigma:latest`；实际启动时保存并使用不可变 image ID。
如需从源码构建：

```bash
docker build --platform linux/amd64 -f docker/enigma.Dockerfile -t enigma:v0.7-084d5d1 .
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --pilot --image enigma:v0.7-084d5d1
```

默认诊断题为 `2017q-cry-another_xor`、`2019q-rev-beleaf`、`2018q-msc-short_circuit`。
它们有完整附件且不需要题目服务；选题没有查看标准 flag 或旧解题轨迹，不包含泄漏 flag 的玩具题。
新试跑默认每题 0.1 USD、整批 0.3 USD，与当前正式运行的每题预算相同。
不能根据三道诊断题估计整个 NYU 数据集的普遍解题成本或成功率。

本次已成功下载并验证的官方镜像固定为：
`sweagent/enigma@sha256:59353332506b31afc3483b093e0d5bcbc5e8194cb30b78bc5397f3d3582cb410`。
创建时间为 2024-08-29，展开大小约 4.52 GB。实测 conda 初始化脚本、gdb、radare2、sage、
tshark、sqlmap、Ghidra analyzeHeadless 与所需 Python CTF 包可用。
初次拉取遇到网络 EOF，第二次重试成功；没有换用其他项目镜像。

完整测试由用户手动启动，必须明确填写整批预算。下面给出每题 0.1 USD、整批 20 USD 的
命令形式；每题预算由用户指定，不是已验证的最佳预算。
入口检查附件完整性，不会静默忽略缺失题目。目前缺失附件已补齐。

```bash
sudo service docker start
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.1 --total 20
```

### 断点续跑

手动中断（Ctrl+C）后重新运行同一命令，会自动跳过之前批次已完成的题目，从
中断的那道题继续。判断依据是历史 `run-*/results.json`（试跑对应 `pilot-*/results.json`）
里同模型、同配置、同预算、同实验 ID 和同隔离协议的条目：

- 已完成的有效尝试（无论成功或失败）会被跳过，保持 pass@1 口径、不重复计费；
- 被中断的题保存为 `interrupted`，重新运行时会从这道题重新开始解题（不恢复题内会话）；
- 基础设施故障（`infrastructure_error`）、API 重试耗尽、API 失败或计费不完整的 `exit_error` 也会重试；
- 某题报错后当前批次直接继续下一题，历史尝试和日志始终保留。

```bash
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.1 --total 20
```

重新运行时 `--total` 作用于剩余题目，可按剩余预算填写。若确需从头完整重跑，
加 `--from-scratch` 忽略历史结果。正式运行与试跑的已完成记录互不影响。

### 多进程分块并行

同一模型可将整批拆成多个子进程并行解题（`--run` 专用）。`--num-workers N` 把
题目切成 N 个连续分块，每个分块一个独立子进程，各自写独立的 `run-*/results.json`、
usage ledger 与日志；父进程按分块大小等分 `--total`，全部结束后汇总本次成功数与
已知费用上界。

```bash
sudo service docker start
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.1 --total 20 --num-workers 4
```

每个 worker 的完整输出另存于 `enigma_reproduction/generated/parallel-*/worker-*.log`，
父进程在标准输出打印每个 worker 的分块大小、退出码和汇总结果。并行度受 Docker
容器资源与 API 速率限制约束。每题使用唯一 Compose 项目和私有 Docker 网络，保留
题目的内部端口和 DNS 别名，移除宿主端口发布及固定容器名；清理仅操作本题资源。
使用 host 网络的特殊题会自动取得排他锁，其他题可并发。断点续跑与并行兼容：重跑会自动跳过所有已完成题目后
再重新分块。

正式运行和试跑的可读日志统一保存在 Git 忽略的 **`logs/deepseek-flash/`**，
每题只有一个 `.log` 文件，例如 `Beleaf_20261008_180000_123456.log`。
开头约十行列出题目、成功/退出状态、0.1 USD 预算、费用上下界、步骤、主/摘要调用次数、
token/缓存统计、各项耗时、工具频次及原始数据路径；后面连续保存提示、思考、动作、
每步响应与观察。运行中日志持续写入，开头状态为 `running`；结束或 Ctrl+C 时填入最终指标。
进程被 SIGKILL 或机器断电时无法执行收尾，可能仍显示 `running`，过程记录保留。

用于分析核验的数值数据与官方轨迹另存于 `enigma_reproduction/generated/` 和 `trajectories/`，
不放进模型日志文件夹。
每题 `*-usage.jsonl` 含主/摘要调用 token 数、上下界费用、计费是否完整；
`results.json` 含成功与退出状态；`summary.json` 汇总成功题的费用中位数和最大值。
`*-steps.jsonl` 记录每步工具类别、时间、动作与摘要时间、观察行数/字符数、主/摘要合计
token 与费用增量。usage ledger 另含调用延迟、输出 token/s、消息数量、输入/输出字符数、
finish reason。每题记录初始化时间、总耗时、步数、工具调用次数、缓存命中率、API 时间。
动作与摘要时间直接来自 v0.7 的 `execution_time`，包含摘要模块时间，不能当作纯容器执行时间。
整批包含 token/API 总数、成功率、退出原因分布和六类别的题数/成功数/费用。
这些是被动观测，不改变提示词、动作或摘要。官方 `.traj`、参数与预测仍保留；新运行的可读日志只写入上述模型目录，历史 run 日志不改动。
失败题仍计入总花费。报告不把基础设施故障当成解题失败的有效实验，也不假设它花费为零。
数据 inventory 保留所有 200 题，并记录缺失附件；全量评分不得悄悄删除缺失样本改变分母。

若在当前工具执行沙箱内访问 Docker 被权限拒绝，需在正常 Linux 终端运行，
或由已授权的执行工具使用可访问 Docker 的环境。不是数据路径不存在，也不是凭据错误。
任何命令都不上传整个项目、Word、旧日志或私密配置；模型只接收官方方法构造的题目上下文和观察。

### Qwen3-Max：DeepSeek 成功的 82 题

使用 `qwen3_max_settings.json` 和固定 SHA256 的 `qwen3_max_deepseek82_carry2_tasks.json`。
该清单与 GPT 实验的 DeepSeek 成功 82 题完全一致，保留每道题的 DeepSeek 成功证据，
但不继承 GPT 的成功或失败结果。按用户要求保留 Qwen 已成功的 A Poem For You
和 beleaf，重新运行其余 80 题。两题成功来自启用 thinking 的 v2 实验，证据和来源
配置记录在清单的 `carried_success_evidence` 中；统计时应与本轮关闭 thinking 的结果区分。
传入 `--ids` 也只能从这 82 题中选择。
凭据读取 `DASHSCOPE_API_KEY` 或权限 `0600` 的 `.secrets/dashscope_api_key`。

OpenAI 兼容地址为
`https://llm-c83fttgtl5nbioqs.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。
DashScope 原生地址记录在设置中，当前适配器使用 OpenAI 兼容接口。
请求显式设置 `enable_thinking=false`，不发送 `thinking_budget`。使用流式输出，
通过 `stream_options.include_usage=true` 读取最终 usage；仅最终回复进入动作解析。
主模型与摘要模型共用每题 0.1 USD 预算。费用按官方北京地域的美元列表价逐请求阶梯估算，
API 报告的缓存命中 token 按标准输入单价的 20% 估算，其余输入按全价计费。
专属端点的实际合同账单可能不同；如合同缓存价格不同，调整设置中的
`pricing.implicit_cache_multiplier`。账本同时保存不扣缓存折扣的参考费用
`reference_cost_without_cache_discount_usd`，用于核对预算口径。
参考：[官方价格](https://www.alibabacloud.com/help/en/model-studio/model-pricing)、
[流式 usage](https://help.aliyun.com/en/model-studio/stream)。

默认每题预算 0.1 USD、本次启动总预算 8.2 USD、3 个 worker。预算按返回的真实 usage
检查，最后一次已发出的调用可能使费用超过阈值；这沿用现有 GPT/DeepSeek 的停止语义。
API 超时或人工中断时没有返回的用量标记为未知，不假定费用为零。

```bash
# 默认 3 路并行；Ctrl+C 停止后，再执行同一命令自动续跑。
bash enigma_reproduction/run_qwen3_max.sh

# 可选：直接并行复测三题，不做百炼连接检查。
bash enigma_reproduction/run_qwen3_max_smoke.sh

# 切换到 2 或 4 路并行，同一实验仍按历史完成记录续跑。
bash enigma_reproduction/run_qwen3_max.sh --num-workers 2
bash enigma_reproduction/run_qwen3_max.sh --num-workers 4

# 只预览题目选择，不发模型请求。
bash enigma_reproduction/run_qwen3_max.sh --dry-run
```

可读日志统一写入 `logs/qwen3-max/题目名_上海时间.log`；不同 worker 使用独立进程、
结果目录、费用账本、日志和解题容器。父进程批次输出位于
`enigma_reproduction/generated/parallel-*/worker-*.log`。
两个启动脚本均直接开始解题，不执行百炼连接检查。
当前实验 ID 为 `qwen3-max-deepseek82-no-thinking-carry2-20261010`，重新运行时保持该 ID；
改用 `--experiment 新名称` 可建立独立实验。
同一实验的第二个启动器会被锁阻止，以免重复解题；需要 2–4 路时使用 `--num-workers`。
如只想跳过问题题目，可运行 `bash enigma_reproduction/run_qwen3_max.sh --skip-ids 题目ID`，
或用 `--ids` 指定清单中的部分题目。`--skip-ids` 仅作用于本次启动，后续续跑若仍想排除
这些题，应保留同一参数；
基础设施/API 故障默认保留待重试状态，不会被记为有效的 pass@1 失败。

修正版保留官方六类别提示词、demonstrations、ACI、摘要与评分流程。只在 Qwen
适配层做以下处理，实验结果应注明 `first_command_v1`，与原始模型输出直接进入最后
代码块解析器的运行区分：

- 显式设置 `enable_thinking=false`，与 DeepSeek 实验一样关闭 thinking；回复长度为 `max_tokens=4096`。
- 为解题请求设置终端提示符停止词，减少模拟后续终端轮次。摘要请求不带这些停止词。
- 只保留回复的第一个完整 shell 命令块及此前 discussion；后续模拟输出和动作不进入
  官方动作解析器或下一轮历史。完整 `<think>...</think>` 区块也不进入历史。
- 拒绝不完整代码块、空命令和裸 Python 代码；最多重试一次。重试照常计入主/摘要
  共用预算，不把未执行的模拟输出作为事实返回给模型。
- 空回复已有的 usage 正常计费，再做一次有上限的格式纠正；缺失 usage 或接口失败
  不使用这条回复纠正路径，也不会被当成零费用。

每次主模型请求的 usage 账本保留 `raw_response`、`response_normalization`、
`output_attempt`、`enable_thinking`，可比较原回复和实际执行命令。
旧配置已保存为 `qwen3_max_legacy_settings.json`；最初的 19 题日志和结果已按要求删除。
此前实验的失败结果不会被当前实验复用，仅显式保留清单中的两题成功。
历史实验可用该配置显式运行，但不会被修正版实验的续跑判断混用。
