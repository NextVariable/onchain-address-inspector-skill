# 官方来源与统计边界 / Official sources and statistical scope

2026-10-05（Asia/Shanghai）核对 [Monad官方测试网文档](https://docs.monad.xyz/developer-essentials/testnet.md)：chain ID 10143（0x279f），原生币MON、18位小数。浏览器为 [MonadVision](https://testnet.monadvision.com) 和 [MonadScan](https://testnet.monadscan.com/)。文档说明测试网曾在2025-12-16从创世重置；历史数据和公共服务可用性都可能变化。

Checked the linked official testnet documentation on 2026-10-05 (Asia/Shanghai): chain ID 10143 (0x279f), native MON with 18 decimals, and the linked MonadVision/MonadScan explorers. The documented genesis reset occurred on 2025-12-16; historical data and public services may change.

官方端点为 `https://testnet-rpc.monad.xyz`（QuickNode、50 rps）、`https://rpc.ankr.com/monad_testnet`（Ankr、300次/10秒、非archive）及 `https://rpc-testnet.monadinfra.com`（Foundation、20 rps、archive、禁止batch）。本版默认Foundation；这些服务的配额不是本工具性能承诺。实际查询以本次chainId、范围及响应为准，历史不可用时报告失败，不静默切换节点或缩小范围。

Official endpoints are QuickNode https://testnet-rpc.monad.xyz (50 rps), Ankr https://rpc.ankr.com/monad_testnet (300 requests/10 seconds, nonarchive), and Foundation https://rpc-testnet.monadinfra.com (20 rps, archive, no batch). Foundation is the default. Provider quotas are not tool performance guarantees. Verify chain ID, bounds, and responses for each run; disclose missing history rather than switching nodes or reducing scope silently.

[Monad JSON-RPC接口](https://docs.monad.xyz/reference/json-rpc/api) 中，本版只允许eth_chainId、eth_blockNumber、eth_getBlockByNumber、eth_getTransactionReceipt和eth_getCode。逐块请求完整交易对象，仅匹配顶层from/to；nonce不是活动列表，Transfer日志不是全部交易。数量遵循 [Ethereum JSON-RPC十六进制规范](https://ethereum.org/developers/docs/apis/json-rpc/#hex-value-encoding)，限uint256宽度；重复JSON字段、非标准常量及不匹配的响应信封会被拒绝。

The linked JSON-RPC API permits this implementation’s five methods: eth_chainId, eth_blockNumber, eth_getBlockByNumber, eth_getTransactionReceipt, and eth_getCode. Full block transactions are matched by top-level from/to. Nonce is not an activity list and Transfer logs are not all transactions. Quantities follow the linked Ethereum hex encoding with uint256 width limits. Duplicate JSON fields, nonstandard constants, and mismatched envelopes are rejected.

成功/失败来自与交易及区块身份匹配的回执status 1/0；缺失、异常或其他状态保持unknown。覆盖完整要求所有区块取得、相关回执有效、无重复异常，并通过相邻parentHash和扫描后的结束块哈希复查。检查不能证明交易根、独立共识、跨查询空洞的链关系或未来不重组。代码查询失败单独警告，不改变顶层交易扫描口径。

Success/failure comes from matching receipt status 1/0; missing, malformed, or other status remains unknown. Completeness requires all blocks, valid relevant receipts, no duplicate anomalies, adjacent parent-hash consistency, and an ending-block recheck. These checks do not prove transaction roots, independent consensus, relationships across gaps, or absence of future reorganization. Code-query failure is a separate warning.

直接to集中度以已获取的所有to=目标顶层交易为分母，包含失败、未知状态及普通转入。发起地址按from去重，不代表人；次数至少两次仅表示本范围内重复交互。并列按地址稳定排序；占比保留精确分子/分母，百分比截断到两位。不同对手是匹配交易from/to的并集，排除目标自身和null。

Direct-to concentration uses all retrieved top-level transactions to the target, including failures, unknowns, and transfers. Unique from addresses are not people; repeated means at least two interactions in this range. Ties sort by address. Shares retain exact numerator/denominator and truncate displayed percentages to two decimals. Counterparties are the union of matching from/to, excluding the target and null.

has_input_data判断合法字节串是否非空；缺失或无效为null。非空input子集不是有效函数调用或真实用户分类：转账可带data，receive/fallback可收空data。input未知不会凭空补齐，分组与未知数量在JSON中明示。

has_input_data indicates whether a valid byte string is nonempty; missing/invalid is null. Nonempty input is not a valid-call or human-user category: transfers can carry data and receive/fallback can accept empty data. Unknown input is not fabricated; groups and unknown counts are explicit.

原生币以整数wei累计，仅计成功顶层value，不含gas、内部转账或代币。失败/未知value只展示，不累计；自转同时计入转入转出。from=目标的合约创建可计入，但to=null没有对手，不能识别“目标是新建合约”的记录。协议系统活动不能当作普通用户采用；has_code不区分合约、委托代码账户或实体身份。

Native amounts use integer wei and successful top-level value only, excluding gas, internal transfers, and tokens. Failed/unknown amounts are displayed but not accumulated. Self-transfers count in both directions. Contract creation from the target can count but null to has no counterparty; creation of the target itself is not identified. System activity does not establish ordinary adoption; code presence does not distinguish contracts, delegated-code accounts, or identities.

匹配交易先按hash去重；完全相同保留一次，字段冲突排除该哈希，两者都标记不完整。raw_transaction_fingerprint是排序JSON的SHA-256，用于发现未展示字段差异，不是链上交易哈希算法或密码学验证。部分查询只描述已获取样本；全部冲突排除时metrics=null，不能将缺口称为零交易或失败交易。

Transactions deduplicate by hash before receipts and metrics. Identical duplicates retain one; conflicts exclude that hash. Both mark the query incomplete. The raw fingerprint is SHA-256 of sorted JSON to detect hidden field differences, not a chain transaction hash or cryptographic verification. Partial queries describe retrieved samples; complete conflict exclusion yields null metrics, not zero or failed activity.

更早修订口径保存在仓库docs/history/sources-and-scope-v1.6.md；此安装包提供当前口径，不依赖仓库外文件运行。


Earlier definitions remain in the repository’s version 1.6 history document. This installation package provides current definitions and does not depend on external repository files at runtime.