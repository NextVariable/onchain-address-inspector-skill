# 贡献与验证 / Contributing and validation

变更应保留有限区块、只读测试网和证据边界。指标从完整校验后的字段计算，失败/未知/漏查分别记录；修复应先有可复现输入，再添加验证结果。不要用fixture冒充真实查询、覆盖失败记录、提交凭证，或为了通过测试取消TLS校验和完整性判断。

Preserve bounded ranges, read-only testnet access, and evidence boundaries. Calculate metrics from validated fields and distinguish failed, unknown, and missing records. Reproduce a defect before adding verification. Do not present fixtures as live queries, overwrite failure evidence, commit credentials, or disable TLS and integrity checks to pass tests.

运行环境只需Python3.10+；维护检查的第三方依赖仅用于开发和CI：

Runtime requires only Python 3.10+. Third-party maintenance dependencies are used only for development and CI:

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/check_package.py
python3 -m ruff check .
python3 -m ruff format --check .
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage combine
python3 -m coverage report
```

扩展账本测试使用 `MONAD_TEST_SEEDS=10000 python3 -m unittest discover -s tests -v`。公共RPC查询需另外明确范围和预算，将命令与脱敏原始输出记录在已忽略的artifacts/，避免默认CI依赖外部服务。回环HTTP测试需要允许本机临时端口；遇到沙箱权限错误应报告执行环境，不能记为功能通过。

Use `MONAD_TEST_SEEDS=10000 python3 -m unittest discover -s tests -v` for expanded ledgers. Record explicit ranges, budgets, commands, and redacted outputs for public RPC queries under ignored artifacts/; default CI must not depend on public services. Loopback tests require temporary local ports. Report sandbox permission failures as environment limits, not successful tests.

SKILL.md保持核心工作流，参数和结果细节按需放references。运行模块以职责而不是固定行数划分；所有模块必须随安装包复制。公开提交不包含个人过程日志；需要保留的原始记录存放于仓库外。提交前检查差异和残留文件，记录验证；贡献遵循仓库的MIT许可证。


Keep the core workflow in SKILL.md and conditional parameter/output detail in references. Split modules by responsibility, not fixed line counts, and ship every runtime module. Keep personal process logs outside public commits; preserve raw records outside the repository. Review differences and residual files before committing. Contributions follow the repository MIT license.