# 运行参数与结果解释 / Runtime and output reference

命令入口是 `scripts/query.py`。默认最近100块，最大1000块；`--start-block`和`--end-block`必须同时提供，包含两端，与`--recent-blocks`互斥。用户给出的范围不能静默缩小。输入地址为0x加40位十六进制，规范化为小写；不验证混合大小写checksum。

Run `scripts/query.py`. Default: latest 100 blocks; maximum: 1000. Both inclusive start/end arguments are required together and exclude recent-blocks. Do not silently shorten the user range. Addresses must be 0x plus 40 hexadecimal digits and are normalized to lowercase; mixed-case checksums are not validated.

默认每请求15秒，`--timeout`允许1–60秒；`--retries`允许0–2；并发默认4、`--concurrency`允许1–8。请求及重试共享5 rps调度限制，服务端接收时间受网络和线程调度影响，不是独立的客户端调度计时证据。最大5000笔相关回执，超额明确标unknown。整个查询默认120秒，`--total-timeout`允许1–600秒，所有阶段、排队和重试共享截止。大范围和活跃地址可能超过预算；提高并发只重叠等待，不提高请求速率或保证完成。

Default request timeout is 15 seconds (allowed 1–60), retries 0–2, concurrency 4 (allowed 1–8). Requests and retries share a 5 rps schedule; server arrival times are not client scheduling evidence. At most 5000 related receipts are queried; excess remains unknown. Total budget defaults to 120 seconds (allowed 1–600), covering all phases, queues, and retries. More concurrency overlaps waits without raising rate limits or guaranteeing completion.

`--transport auto`无代理时选Python标准库HTTP/1.1连接池，有HTTP_PROXY/HTTPS_PROXY/ALL_PROXY及小写变体时保留curl。可显式选http或curl；http由Python系统证书验证TLS，curl也不关闭验证。代理环境显式选http意味着选择直接连接，应说明。连接池限制响应体32MiB，完整读取后复用；单请求截止覆盖连接和读取。平台DNS可能短暂留在固定数量daemon线程中，但迟到连接会先检查取消，不继续发送RPC；不承诺毫秒级硬实时退出。

Auto selects the standard-library HTTP/1.1 pool without proxy variables and curl when HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, or lowercase variants exist. Explicit http means direct connection even in a proxy environment. Both transports verify TLS. The pool bounds bodies to 32 MiB, reuses fully consumed responses, and applies request deadlines to connect/read. OS DNS may briefly remain in bounded daemon workers; late jobs check cancellation before sending. Millisecond hard-real-time exit is not guaranteed.

若Python缺少可用CA证书文件，HTTP会报告tls_failure而停止，不得关闭证书校验。选择已配置可信证书的Python，或明确通过SSL_CERT_FILE指定可信CA bundle；也可显式选curl并保留其证书验证。这是运行环境条件，不将失败伪装为查询完成。

Missing trusted Python CA configuration produces tls_failure and stops. Use properly configured Python, a trusted SSL_CERT_FILE bundle, or explicit curl with verification retained. Do not disable certificates or disguise environment failures as complete queries.

`MONAD_TESTNET_RPC`或`--rpc`指定来源，凭证优先环境变量。URL必须为有效HTTP(S)、有效端口，无原始空白、控制字符或非空fragment。脚本只发送允许的五个读方法，不自动切换RPC或拼接不同来源。遇到公共节点失败，报告本次覆盖和错误；可明确选择sources-and-scope.md中的其他端点，重跑结果分别记录。

Choose the source with MONAD_TESTNET_RPC or --rpc; prefer environment variables for credentials. URLs require HTTP(S), valid ports, no raw whitespace/control characters, and no nonempty fragment. Only five allowlisted read methods are sent. Sources are not switched or merged automatically. Report coverage/errors on failure and record any explicitly selected alternative endpoint as a separate run.

stdout始终一个JSON文档，`--progress`将阶段、完成数及五秒心跳写到stderr。阶段包括network、blocks、consistency、receipts、code、done/incomplete；完成数包含失败任务，不代表成功获取。`elapsed_seconds`包含调度和结果整理，不等于服务性能承诺。退出码0表示coverage.complete=true；2表示错误或不完整。帮助命令退出码0且不查询网络。

Stdout is one JSON document. --progress writes phases, counts, and five-second heartbeats to stderr. Phases are network, blocks, consistency, receipts, code, done/incomplete. Counts include failed tasks and do not mean successful retrieval. elapsed_seconds includes scheduling and result assembly, not a performance promise. Exit 0 means complete; exit 2 means error/incomplete. Help exits 0 without network access.

输出schema_version为1.7。requested_range记录输入及本次解析范围，actual_scanned_range只描述已成功块的最小/最大值、成功数及是否有空洞；检查successful_blocks/failed_blocks才可知道实际覆盖。chain_id来自RPC核对。queried_at/completed_at为UTC时间。

Schema version is 1.7. requested_range records input and resolved bounds. actual_scanned_range reports retrieved minimum/maximum blocks, count, and possible gaps; inspect successful_blocks/failed_blocks for actual coverage. chain_id is checked against RPC. Query start/end timestamps are UTC.

coverage含complete、scan_complete、chain_consistent、block_headers、data_anomalies、missing_receipts及逐笔receipt_failures。errors保留错误分类及脱敏attempts。失败分类包括deadline_exceeded、timeout、dns_failure、connection_failure、tls_failure、transport_failure、rate_limited（HTTP429）、http_error、rpc_error、invalid_response、invalid_method、invalid_block、invalid_transaction、receipt_not_found、receipt_mismatch、invalid_receipt、unknown_status和receipt_budget_exceeded。RPC数值code本身不证明限流，null回执不证明pending/失败/不存在；不回显服务message或凭证URL。重试退避和调度期间耗尽截止也保留已有attempts。

Coverage records completeness, consistency, block headers, anomalies, missing receipts, and per-receipt failures. Errors retain categories and redacted attempts, including deadline, timeout, DNS, connection, TLS, transport, rate-limit/HTTP/RPC, invalid data/method/block/transaction/receipt, missing/mismatched receipt, unknown status, and receipt-budget errors. RPC codes alone do not prove throttling; null receipts do not establish pending, failed, or nonexistent transactions. Raw provider messages and credential URLs are not echoed. Attempts remain recorded when backoff or scheduling exhausts the deadline.

transactions保留hash、block/block_hash、UTC timestamp、from/to、value_wei/value_mon、原始交易指纹、input分组、status和explorer_url。metrics是已取得样本的确定性计算；无成功块、链一致性失败或相关记录全部冲突时为null。成功顶层金额在native_value_successful_top_level_only，其他金额不擅自合并。code_at_end_block只说明指定块返回的代码是否非空，失败时为null并警告。

Transactions retain hashes, block identity, UTC time, from/to, wei/MON amounts, raw fingerprints, input grouping, status, and explorer links. Metrics deterministically describe retrieved samples; no successful blocks, inconsistent chains, or complete conflict exclusion yields null. Successful top-level amounts have a separate field; unrelated amounts are not merged. Code presence describes only the specified ending block and yields a warning/null on failure.

performance记录实际传输方式、requests、retries及methods。HTTP模式另记connections_opened和reused_requests；这是连接选择/请求尝试诊断，不是服务端收到次数、链上交易量或真实用户数。计算口径与统计边界读取sources-and-scope.md，不能仅按字段名推断。


Performance records transport, request attempts, retries, and methods; HTTP also records connections opened and reused requests. These are transport diagnostics, not server-received counts, transaction volume, or human users. Read sources-and-scope for definitions instead of inferring semantics from field names.