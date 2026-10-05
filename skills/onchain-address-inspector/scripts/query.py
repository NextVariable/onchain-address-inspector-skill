#!/usr/bin/env python3
"""只读核查链上地址的顶层交易活动。目前支持 Monad 测试网；需要 Python 3.10+，curl 可选。"""

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.parse

from evidence import (
    EXPLORER,
    QueryError,
    apply_receipt_status,
    metrics,
    parse_block,
    quantity,
)
from rpc_client import Client

CHAIN_ID = 10143
RPC = "https://rpc-testnet.monadinfra.com"
MAX_BLOCKS = 1000
DEFAULT_BLOCKS = 100
MAX_RECEIPTS = 5000


class Progress:
    """Operational progress on stderr; stdout remains one JSON document."""

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = {"phase": "network", "completed": 0, "total": None}
        self.started = time.monotonic()
        self.thread = None

    def emit(self):
        if self.enabled:
            with self.lock:
                event = {
                    **self.state,
                    "elapsed_seconds": int(time.monotonic() - self.started),
                }
            print(
                json.dumps({"progress": event}, ensure_ascii=False),
                file=sys.stderr,
                flush=True,
            )

    def update(self, phase, completed=0, total=None):
        with self.lock:
            self.state = {"phase": phase, "completed": completed, "total": total}
        self.emit()

    def heartbeat(self):
        while not self.stop.wait(5):
            self.emit()

    def start(self):
        if self.enabled:
            self.emit()
            self.thread = threading.Thread(target=self.heartbeat, daemon=True)
            self.thread.start()

    def finish(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        self.emit()


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate(args):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.address):
        raise QueryError("address 必须是 0x 加 40 位十六进制；本工具不验证混合大小写 checksum")
    if args.recent_blocks is not None and (
        args.start_block is not None or args.end_block is not None
    ):
        raise QueryError("recent-blocks 与 start-block/end-block 互斥")
    if (args.start_block is None) != (args.end_block is None):
        raise QueryError("start-block 与 end-block 必须同时提供")
    if args.start_block is not None:
        if args.start_block < 0 or args.end_block < args.start_block:
            raise QueryError("非法区块范围")
        if args.end_block - args.start_block + 1 > MAX_BLOCKS:
            raise QueryError(f"区块范围超过上限 {MAX_BLOCKS}，未扫描")
    if args.recent_blocks is not None and not 1 <= args.recent_blocks <= MAX_BLOCKS:
        raise QueryError(f"recent-blocks 必须在 1–{MAX_BLOCKS} 之间")
    if not 1 <= args.concurrency <= 8 or not 1 <= args.timeout <= 60 or not 0 <= args.retries <= 2:
        raise QueryError("并发 1–8、超时 1–60 秒、重试 0–2 次")
    if not 1 <= args.total_timeout <= 600:
        raise QueryError("整次查询总耗时必须为 1–600 秒")
    # urlsplit silently strips some control characters. Reject before parsing.
    if any(ord(c) <= 32 or ord(c) == 127 for c in args.rpc):
        raise QueryError("RPC 必须是有效 HTTP(S) URL")
    try:
        u = urllib.parse.urlsplit(args.rpc)
        valid = (
            u.scheme in ("https", "http")
            and u.hostname
            and not u.fragment
            and (u.port is None or 1 <= u.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise QueryError("RPC 必须是有效 HTTP(S) URL")


def scan(args, client=None):
    out = {
        "schema_version": "1.7",
        "network": "Monad Testnet",
        "chain_id": None,
        "address": args.address.lower(),
        "requested_range": {
            "start_block": args.start_block,
            "end_block": args.end_block,
            "recent_blocks": args.recent_blocks,
            "default_recent_blocks": DEFAULT_BLOCKS
            if args.start_block is None and args.recent_blocks is None
            else None,
        },
        "actual_scanned_range": None,
        "queried_at": utc(),
        "coverage": {
            "complete": False,
            "scan_complete": False,
            "successful_blocks": [],
            "failed_blocks": [],
            "missing_receipts": [],
            "receipt_failures": [],
            "data_anomalies": [],
            "chain_consistent": False,
            "block_headers": [],
            "scope": "top-level from/to only",
        },
        "metrics": None,
        "transactions": [],
        "warnings": [],
        "errors": [],
        "source": {
            "rpc": "configured endpoint (URL omitted)",
            "rpc_profile": {
                RPC: "Monad Foundation public testnet",
                "https://testnet-rpc.monad.xyz": "QuickNode public testnet",
                "https://rpc.ankr.com/monad_testnet": "Ankr public testnet",
            }.get(args.rpc, "custom endpoint (URL omitted)"),
            "explorer": EXPLORER,
        },
        "code_at_end_block": None,
    }
    progress = Progress(getattr(args, "progress", False))
    progress.start()
    c = None
    try:
        validate(args)
        c = client or Client(
            args.rpc,
            args.timeout,
            args.retries,
            args.total_timeout,
            args.transport,
            args.concurrency,
        )
        out["total_timeout_seconds"] = args.total_timeout
        out["chain_id"] = quantity(c.call("eth_chainId", []))
        if out["chain_id"] != CHAIN_ID:
            raise QueryError(f"错误网络：期望 {CHAIN_ID}，实际 {out['chain_id']}；停止")
        latest = quantity(c.call("eth_blockNumber", []))
        if args.start_block is not None:
            start, end = args.start_block, args.end_block
        else:
            end = latest
            start = max(0, end - (args.recent_blocks or DEFAULT_BLOCKS) + 1)
        out["requested_range"]["resolved"] = {
            "start_block": start,
            "end_block": end,
            "inclusive": True,
        }
        if end > latest:
            raise QueryError("end-block 超过当前最新区块，未扫描")
        minimum_scan_seconds = (end - start) / 5
        if minimum_scan_seconds >= args.total_timeout:
            out["warnings"].append(
                f"按共享5 rps限速，仅区块请求起始间隔至少约{minimum_scan_seconds:.1f}秒，超过当前总预算；预计只能得到部分结果。可明确增加 --total-timeout（最高600秒），回执和网络等待还需额外时间"
            )
        if args.start_block is None:
            out["warnings"].append(
                "最近区块范围以本次 eth_blockNumber 快照为终点；不代表时间范围或完整历史"
            )
        address = out["address"]

        def read_block(n):
            try:
                b = c.call("eth_getBlockByNumber", [hex(n), True])
                found, header = parse_block(b, n, address)
                return n, found, header, None
            except QueryError as e:
                return n, [], None, e.detail()
            except (
                KeyError,
                TypeError,
                ValueError,
                AttributeError,
                OverflowError,
                OSError,
                RecursionError,
            ):
                return (
                    n,
                    [],
                    None,
                    {
                        "category": "invalid_block",
                        "error": "invalid block data",
                        "attempts": [],
                    },
                )

        progress.update("blocks", 0, end - start + 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(read_block, n) for n in range(start, end + 1)]
            for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                n, found, header, error = future.result()
                progress.update("blocks", completed, end - start + 1)
                if error:
                    out["coverage"]["failed_blocks"].append(n)
                    out["errors"].append({"block": n, **error})
                else:
                    out["coverage"]["successful_blocks"].append(n)
                    out["transactions"].extend(found)
                    out["coverage"]["block_headers"].append(header)
        headers = sorted(out["coverage"]["block_headers"], key=lambda h: h["block"])
        out["coverage"]["block_headers"] = headers
        consistency_errors = []
        for previous, current in zip(headers, headers[1:]):
            if (
                current["block"] == previous["block"] + 1
                and current["parent_hash"] != previous["hash"]
            ):
                consistency_errors.append(
                    {
                        "category": "chain_mismatch",
                        "block": current["block"],
                        "error": "相邻区块不属于同一条链，停止计算活动指标",
                    }
                )
        # Re-read the ending block after scanning; no merging of different snapshots.
        progress.update("consistency")
        try:
            ending = c.call("eth_getBlockByNumber", [hex(end), False])
            scanned_end = next((h for h in headers if h["block"] == end), None)
            if (
                not isinstance(ending, dict)
                or quantity(ending["number"]) != end
                or not isinstance(ending.get("hash"), str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", ending["hash"])
            ):
                raise QueryError("结束区块复查响应无效", "invalid_block")
            if scanned_end and ending["hash"].lower() != scanned_end["hash"]:
                raise QueryError("结束区块在扫描期间发生变化", "chain_mismatch")
        except QueryError as e:
            consistency_errors.append(e.detail())
        except (KeyError, TypeError, ValueError):
            consistency_errors.append(
                {"category": "invalid_block", "error": "结束区块复查字段无效"}
            )
        out["errors"].extend(consistency_errors)
        out["coverage"]["chain_consistent"] = not consistency_errors
        # Deduplicate before receipt requests and any metric/value accumulation.
        unique, conflicted = {}, set()
        for t in sorted(out["transactions"], key=lambda t: (t["block"], t["hash"])):
            h = t["hash"]
            if h in conflicted:
                continue
            if h in unique:
                conflict = unique[h] != t
                anomaly = {
                    "hash": h,
                    "category": "conflicting_duplicate" if conflict else "duplicate_transaction",
                    "blocks": [unique[h]["block"], t["block"]],
                }
                out["coverage"]["data_anomalies"].append(anomaly)
                out["errors"].append(
                    {
                        **anomaly,
                        "error": "重复交易字段冲突，排除该哈希"
                        if conflict
                        else "重复交易已去重，查询标记不完整",
                    }
                )
                if conflict:
                    unique.pop(h)
                    conflicted.add(h)
            else:
                unique[h] = t
        txs = sorted(unique.values(), key=lambda t: (t["block"], t["hash"]))
        out["transactions"] = txs

        def receipt(t):
            try:
                r = c.call("eth_getTransactionReceipt", [t["hash"]])
                apply_receipt_status(r, t)
                return None
            except QueryError as e:
                return {"hash": t["hash"], "block": t["block"], **e.detail()}
            except (KeyError, TypeError, ValueError, AttributeError):
                return {
                    "hash": t["hash"],
                    "block": t["block"],
                    "category": "invalid_receipt",
                    "error": "回执字段缺失或格式无效",
                    "attempts": [],
                }

        failures = []
        limit = min(len(txs), MAX_RECEIPTS)
        progress.update("receipts", 0, limit)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(receipt, t) for t in txs[:limit]]
            for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
                failure = future.result()
                if failure:
                    failures.append(failure)
                progress.update("receipts", completed, limit)
        failures.extend(
            {
                "hash": t["hash"],
                "block": t["block"],
                "category": "receipt_budget_exceeded",
                "error": "超过本次回执查询预算",
                "attempts": [],
            }
            for t in txs[limit:]
        )
        failures.sort(key=lambda f: (f["block"], f["hash"]))
        missing = [f["hash"] for f in failures]
        out["coverage"]["missing_receipts"] = missing
        out["coverage"]["receipt_failures"] = failures
        out["errors"].extend(failures)
        if len(txs) > MAX_RECEIPTS:
            out["warnings"].append(f"相关回执超过预算 {MAX_RECEIPTS}，剩余状态未知")
        out["coverage"]["successful_blocks"].sort()
        out["coverage"]["failed_blocks"].sort()
        good = out["coverage"]["successful_blocks"]
        if good:
            out["actual_scanned_range"] = {
                "start_block": min(good),
                "end_block": max(good),
                "successful_count": len(good),
                "may_have_gaps": bool(out["coverage"]["failed_blocks"]),
            }
        out["coverage"]["scan_complete"] = len(good) == end - start + 1
        out["coverage"]["complete"] = (
            out["coverage"]["scan_complete"]
            and not missing
            and not out["coverage"]["data_anomalies"]
            and out["coverage"]["chain_consistent"]
        )
        progress.update("code")
        try:
            code = c.call("eth_getCode", [address, hex(end)])
            if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", code):
                raise QueryError("invalid code")
            out["code_at_end_block"] = {"block": end, "has_code": code != "0x"}
        except QueryError:
            out["warnings"].append("结束区块代码查询失败；不影响顶层交易扫描完整性")
        out["metrics"] = (
            metrics(txs, address)
            if good
            and out["coverage"]["chain_consistent"]
            and (txs or not out["coverage"]["data_anomalies"])
            else None
        )
        if not out["coverage"]["complete"]:
            out["warnings"].append(
                "查询不完整：指标仅描述已获取样本，不能作为指定范围的精确总数或整体集中度"
            )
        if consistency_errors:
            out["warnings"].append("区块一致性未确认，保留交易证据但不汇总指标；需重新查询")
        if not good:
            out["warnings"].append("没有成功获取任何区块，无法计算活动指标；不能据此判断没有活动")
        elif not txs and out["coverage"]["data_anomalies"]:
            out["warnings"].append("相关交易因数据冲突被排除，不能据此判断没有活动")
        elif not txs and not consistency_errors:
            out["warnings"].append("在已成功查询的指定范围和当前统计口径内，没有找到相关顶层交易。")
    except (QueryError, ValueError, TypeError, KeyError) as e:
        out["errors"].append(
            e.detail()
            if isinstance(e, QueryError)
            else {"category": "invalid_response", "error": "invalid network response"}
        )
    out["warnings"].append(
        "不覆盖 internal calls、代币转账、间接调用、完整用户或协议使用量；不识别目标作为新建合约的创建交易。发起者创建合约交易可计入，但无 to 对手。原生币仅累计成功顶层 value，不含 gas 或完整资金流；未知状态不计入。自转同时计入转入转出，对手排除自身。代码存在不证明身份或项目类型。"
    )
    if any(e.get("category") == "deadline_exceeded" for e in out["errors"]):
        out["coverage"]["complete"] = False
        out["warnings"].append(
            "总耗时已到，保留已获取结果；未取得的交易状态未知，可缩小范围后重新查询"
        )
    progress.update("done" if out["coverage"]["complete"] else "incomplete")
    progress.finish()
    if isinstance(c, Client):
        out["performance"] = c.diagnostics()
        c.close()
    out["elapsed_seconds"] = round(time.monotonic() - progress.started, 3)
    out["completed_at"] = utc()
    return out


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo user arguments: an invalid argument may contain a RPC key.
        raise QueryError("命令行参数无效：检查整数类型、必要参数及选项；用 --help 查看格式")


def parser():
    p = JsonArgumentParser(description=__doc__)
    p.add_argument("--address", required=True)
    p.add_argument("--start-block", type=int)
    p.add_argument("--end-block", type=int)
    p.add_argument("--recent-blocks", type=int)
    p.add_argument("--rpc", default=os.environ.get("MONAD_TESTNET_RPC", RPC))
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument(
        "--total-timeout",
        type=int,
        default=120,
        help="整次查询共享截止，含排队、重试和所有阶段，默认120秒",
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--transport",
        choices=["auto", "http", "curl"],
        default="auto",
        help="auto默认复用HTTP连接；代理环境保留curl；不切换RPC来源",
    )
    p.add_argument("--retries", type=int, default=2)
    p.add_argument(
        "--progress",
        action="store_true",
        help="向 stderr 输出脱敏进度及每 5 秒心跳；stdout 保持 JSON",
    )
    return p


if __name__ == "__main__":
    try:
        result = scan(parser().parse_args())
    except QueryError as e:
        result = scan(parser().parse_args(["--address", ""]))
        result["errors"] = [e.detail()]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["coverage"]["complete"] else 2)
