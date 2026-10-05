# 完整使用说明 / Full usage guide

[![自动测试 / CI](https://github.com/NextVariable/onchain-address-inspector-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/NextVariable/onchain-address-inspector-skill/actions/workflows/ci.yml)

核查钱包与合约地址的链上交易活动，输出成功状态、原生币金额、直接交互集中度和可追溯证据。目前支持 Monad 测试网。只读、无需连接钱包、无需付费 API 密钥；运行只依赖 Python 3.10+ 标准库，curl 是可选传输。它扫描顶层交易，核对回执和区块身份，以整数 wei 计算成功交易金额，并明确说明漏查、未知状态和统计边界。

Inspect on-chain wallet and contract activity, including transaction outcomes, native-coin amounts, direct-interaction concentration, and traceable evidence. Currently supports Monad Testnet. Read-only, with no wallet connection or paid API key required. Runtime uses only the Python 3.10+ standard library; curl is optional. The scanner validates top-level transactions, receipts, and block identities, calculates amounts in integer wei, and discloses gaps and unknown statuses.

调用集中度回答的是“本范围内哪些地址直接向目标发送交易，以及各占多少”，不能据此证明真人用户量、刷量或商业采用。测试网数据也不能代表主网使用情况。

Concentration describes which addresses directly send transactions to the target within the scanned range and their shares. It does not establish human user counts, manipulation, or commercial adoption. Testnet activity does not represent mainnet usage.

## 安装与使用 / Installation and usage

先下载仓库，再把完整 Skill 安装到 Codex 的发现目录。同名目录已存在时命令会停止；更新前请先检查现有内容，避免覆盖自己的修改。

Clone the repository and copy the complete Skill into the Codex discovery directory. Installation stops if the destination already exists; inspect existing files before updating to preserve your changes.

```sh
git clone https://github.com/NextVariable/onchain-address-inspector-skill.git
cd onchain-address-inspector-skill

skill_destination="${CODEX_HOME:-$HOME/.codex}/skills/onchain-address-inspector"
test ! -e "$skill_destination" && mkdir -p "$(dirname "$skill_destination")" && cp -R skills/onchain-address-inspector "$skill_destination"
```

必要时重开 Codex 会话，然后发送：

Restart the Codex session if necessary, then send:

```text
使用 $onchain-address-inspector，核查 Monad 测试网地址 0x000000000000000000000000000000000000dEaD 最近 100 块的顶层活动，说明覆盖情况、发起地址集中度和可核对的交易链接。

Use $onchain-address-inspector to inspect address 0x000000000000000000000000000000000000dEaD on Monad Testnet over the latest 100 blocks. Explain coverage, direct-caller concentration, and traceable transaction links.
```

也可直接运行命令行入口。以下通用地址只演示参数，请替换为目标地址；不预设该地址有活动。

You can also run the CLI. The generic address below demonstrates arguments; replace it with your target. No activity is assumed for this example.

```sh
python3 skills/onchain-address-inspector/scripts/query.py \
  --address 0x000000000000000000000000000000000000dEaD \
  --recent-blocks 100 --progress
```

未给范围时默认最近 100 块；`--recent-blocks N` 与起止范围互斥，两端包含，上限 1000 块。默认每请求 15 秒、最多重试 2 次、并发 4、共享调度速率 5 rps，整个查询的请求、排队和重试共享 120 秒预算。stdout 只有结果 JSON，进度与五秒心跳写入 stderr。完整结果退出码为 0，参数错误或不完整结果为 2；不要用退出码掩盖 JSON 的覆盖记录。

The default is the latest 100 blocks. Explicit start/end bounds are inclusive and mutually exclusive with `--recent-blocks N`; the maximum is 1000 blocks. Defaults: 15 seconds per request, up to 2 retries, concurrency 4, and a shared 5 requests/second schedule. All requests, queues, and retries share a 120-second query budget. stdout contains JSON only; progress and five-second heartbeats go to stderr. Exit code 0 indicates a complete result; 2 indicates invalid arguments or incomplete results. Always inspect JSON coverage.

1000 块仅区块请求的调度间隔就约 200 秒，相关回执还需另行请求。大范围必须明确配置更长预算，例如 `--total-timeout 600`，仍不能保证公共服务完成。默认使用保持 TLS 校验的标准库连接池；检测到代理环境变量时 auto 使用 curl。`--transport http` 或 `--transport curl` 可显式选择，脚本不会自动切换数据来源。

For 1000 blocks, block-request scheduling alone takes about 200 seconds, with additional receipt requests required. Set a longer explicit budget, such as `--total-timeout 600`; completion still depends on the public service. The default standard-library connection pool verifies TLS; auto mode uses curl when proxy environment variables are present. Select `--transport http` or `--transport curl` explicitly if needed. The script does not switch data sources automatically.

Python环境必须有可用的可信CA证书配置。TLS失败时先检查证书路径，可明确选择配置完整的Python或curl；具体诊断见运行说明，不能关闭证书验证。

Python needs a valid trusted CA configuration. On TLS failure, check certificate paths or use a properly configured Python or curl. See the runtime reference for diagnostics; do not disable certificate verification.

凭证端点通过 `MONAD_TESTNET_RPC` 环境变量传入。结果、进度和错误省略 RPC URL，错误正文也不原样显示；不要将带 key 的 URL 写进公开命令、证据或 Git 提交。

Pass credential-bearing endpoints through `MONAD_TESTNET_RPC`. Results, progress, and errors omit the RPC URL and do not echo raw provider errors. Do not put URLs containing keys in public commands, evidence, or Git commits.

## 如何解读结果 / Interpreting results

先看 `coverage.complete`、`coverage.chain_consistent`、`errors`、失败区块和缺失回执。`metrics=null` 表示不能可靠汇总，不表示零活动。部分扫描的统计只描述已取得样本；回执未知不等于链上失败。`transactions` 保存可追溯交易记录，金额以字符串 wei 和 18 位 MON 小数输出，避免浮点损失。

Check `coverage.complete`, `coverage.chain_consistent`, `errors`, failed blocks, and missing receipts first. `metrics=null` means a reliable aggregate is unavailable, not zero activity. Partial metrics describe only retrieved samples; unknown receipt status is not a failed transaction. Transactions retain traceable records, with amounts represented as string wei and 18-decimal MON values.

`caller_ranking` 的分母是所有直接 to 目标的顶层交易，包括失败、未知状态和普通转入。非空 input 子集另行列出，不等于有效函数调用或真实用户。交易链接是供核对的入口，生成链接本身不代表已打开浏览器核验。完整口径见 [官方来源与统计边界](../skills/onchain-address-inspector/references/sources-and-scope.md)，运行和输出字段见 [运行与结果解释](../skills/onchain-address-inspector/references/running-and-output.md)。

The `caller_ranking` denominator includes every retrieved top-level transaction directly sent to the target, including failed, unknown, and ordinary transfers. Nonempty input is reported separately and does not establish a valid function call or a human user. Explorer links are verification entrypoints, not proof of browser inspection. See the linked scope and runtime references for full definitions.

本版不覆盖内部调用、代币转账、间接调用、完整历史、按天查询或目标新建合约追踪。没有 ABI/trace，不推测具体函数、revert 原因或内部资金路径；不会签名、发交易或执行支付。

This version excludes internal calls, token transfers, indirect calls, complete history, date-based queries, and tracking creation of the target contract. Without ABI or traces, it does not infer function names, revert reasons, or internal fund flows. It does not sign, send transactions, or pay.

## 验证与开发 / Validation and development

回归测试覆盖异常响应、整数金额、限速、超时、连接复用和独立安装入口，在 Python 3.10–3.14 运行。测试使用人工账本和本机服务，不需要钱包或公共 RPC：

Regression tests exercise malformed responses, integer amounts, rate limits, deadlines, connection reuse, and independent installation on Python 3.10–3.14. Tests use synthetic ledgers and local services, with no wallet or public RPC required:

```sh
python3 -m unittest discover -s tests -v
```

真实查询结果与覆盖说明见 [验证记录](testing.md)；开发检查见 [贡献说明](../CONTRIBUTING.md)。

See [validation records](testing.md) for live results and coverage, and [contributing](../CONTRIBUTING.md) for development checks.

## 项目结构 / Project structure

```text
skills/onchain-address-inspector/  可复制安装的完整 Skill / Installable Skill
  SKILL.md                  发现信息和核心指引 / Discovery and instructions
  scripts/                  查询、RPC、传输和指标计算 / Runtime modules
  references/               按需加载的参数与统计口径 / On-demand references
  agents/                   Codex 界面元数据 / UI metadata
tests/                      回归与独立安装测试 / Regression and installation tests
scripts/                    仓库维护和证据核对工具 / Maintenance and verification
docs/                       使用、架构与测试说明 / Usage, architecture and testing
```

安装时只需复制 Skill 目录。[架构说明](architecture.md)介绍模块边界，详细使用文档与安装包分开存放。

Copy only the Skill directory for installation. [Architecture](architecture.md) explains module boundaries; extended usage documentation stays outside the installable package.

## 许可 / License

采用 [MIT 许可证](../LICENSE)。安装包也包含许可副本。

Released under [MIT](../LICENSE), with a license copy included in the installable package.
