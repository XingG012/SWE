# EnIGMA / DeepSeek Flash 复现准备

当前项目根目录是官方 SWE-agent v0.7，基线 commit 为
`084d5d164729ddcbe8fbc05877ead403549ef669`，工作分支为 `enigma-v0.7`。
原项目 `origin` 是 `https://github.com/XingG012/SWE.git`，原 `main` 的
`40ca3f37b159dd65eb538a4f0a9934ce57563f26` 已保留；`upstream` 指向官方仓库。
切换前工作区没有已跟踪文件改动。当前改动是下面列明的复现适配，尚未提交。

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
| 当前预算 | 正式运行及新试跑均为每题 0.4 USD；正式整批命令为 80 USD，默认三题试跑整批为 1.2 USD |

三题真实试跑已完成：2 题成功，1 题格式停止，总费用上界约 0.153 USD。
详见 [试跑结果](RESULTS.md)。历史诊断试跑使用每题 3 USD / 整批 9 USD；随后按用户要求，
当前正式运行和新试跑默认每题 **0.4 USD**。正式整批预算必须通过 `--total` 明确指定。
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
规划器、执行器、日志、方法配置或工具。历史诊断试跑每题 3 USD 来自 EnIGMA 论文；当前每题 0.4 USD 由用户指定。

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
7. 依赖：独立 `.venv-enigma`，不修改系统/用户原 Python 环境。
   `swebench` 2.x 导入依赖已删除的 `modal.Mount`，固定 Modal 0.64.235 修复导入。
   不调用 Modal 云端服务。`constraints.txt` 记兼容约束，`requirements.lock.txt` 记录实际完整版本。

官方六类 YAML、demonstrations、command 文件、交互会话定义和 Dockerfile 没有修改。
离线测试位于 `tests/test_deepseek_flash_reproduction.py`；测试使用假凭据和模拟响应，不调用模型。
本次预算与日志适配后共 23 项测试通过，覆盖正式与试跑入口、单题日志、最终指标、
中断收尾、跨题隔离和脱敏。最后本地预检发现 Docker 服务未运行，运行前先执行下面的启动命令；
启动入口仍会检查 Docker、固定镜像、凭据与配置，不会跳过预检。

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
新试跑默认每题 0.4 USD、整批 1.2 USD，与当前正式运行的每题预算相同。
不能根据三道诊断题估计整个 NYU 数据集的普遍解题成本或成功率。

本次已成功下载并验证的官方镜像固定为：
`sweagent/enigma@sha256:59353332506b31afc3483b093e0d5bcbc5e8194cb30b78bc5397f3d3582cb410`。
创建时间为 2024-08-29，展开大小约 4.52 GB。实测 conda 初始化脚本、gdb、radare2、sage、
tshark、sqlmap、Ghidra analyzeHeadless 与所需 Python CTF 包可用。
初次拉取遇到网络 EOF，第二次重试成功；没有换用其他项目镜像。

完整测试由用户手动启动，必须明确填写整批预算。下面给出每题 0.4 USD、整批 80 USD 的
命令形式；每题预算由用户指定，不是已验证的最佳预算。
入口检查附件完整性，不会静默忽略缺失题目。目前缺失附件已补齐。

```bash
sudo service docker start
.venv-enigma/bin/python enigma_reproduction/run_nyu.py --run --per-instance 0.4 --total 80
```

正式运行和试跑的可读日志统一保存在 Git 忽略的 **`logs/deepseek-flash/`**，
每题只有一个 `.log` 文件，例如 `Beleaf_20261008_180000_123456.log`。
开头约十行列出题目、成功/退出状态、0.4 USD 预算、费用上下界、步骤、主/摘要调用次数、
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
