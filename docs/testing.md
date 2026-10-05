# 测试与验证 / Testing and validation

测试覆盖人工账本、整数金额、成功/失败/未知回执、自转、重复冲突、异常RPC响应、共享限速、超时、TLS、连接复用和独立安装入口。人工账本期望值从输入独立计算；真实并发与网络生命周期通过本机HTTP服务及curl进程验证。测试数据不代表真实链上活动。

Tests cover synthetic ledgers, integer amounts, success/failure/unknown receipts, self-transfers, duplicate conflicts, malformed RPC responses, shared rate limits, deadlines, TLS, connection reuse, and independent installation. Ledger expectations are computed from inputs independently; local HTTP servers and curl exercise actual concurrency and transport lifecycles. Fixtures do not establish live-chain activity.

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check_package.py
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage combine
python3 -m coverage report
```

默认CI不访问公共RPC，不需要钱包或密钥。本机网络测试需要允许监听回环临时端口；权限错误不能记为功能通过。CI覆盖Python 3.10–3.14。扩展账本测试可设置`MONAD_TEST_SEEDS=10000`，样本数不等于链上查询次数。

Default CI uses no public RPC, wallet, or credentials. Local network tests require temporary loopback ports; permission failures do not count as functional passes. CI targets Python 3.10–3.14. Set `MONAD_TEST_SEEDS=10000` for expanded ledgers; sample counts are not live-query counts.

真实网络验证需单独指定地址、范围和预算，并检查JSON覆盖结果。保存个人查询结果时使用已忽略的`artifacts/`目录，公开前检查地址关联、路径与凭证。独立结果核对可运行`python3 scripts/verify_evidence.py artifacts/result.json`；它重新计算保存数据的统计，不等于独立节点或共识验证。旧查询不能证明当前公共服务可用。

Live validation requires a separate explicit address, range, budget, and inspection of JSON coverage. Save personal query outputs under ignored `artifacts/`; review address attribution, paths, and credentials before sharing. Run `python3 scripts/verify_evidence.py artifacts/result.json` for independent recalculation of saved statistics; this is not an independent-node or consensus check. Past queries do not prove current public-service availability.
