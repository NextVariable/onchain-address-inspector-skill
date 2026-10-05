"""RPC data validation and deterministic activity calculations; no network IO."""

import collections
import datetime
import hashlib
import json
import re

EXPLORER = "https://testnet.monadscan.com/tx/"


class QueryError(Exception):
    def __init__(self, message, category="query_error", attempts=None):
        super().__init__(message)
        self.category = category
        self.attempts = attempts or []

    def detail(self):
        return {
            "category": self.category,
            "error": str(self),
            "attempts": self.attempts,
        }


def quantity(v):
    # All quantities used here fit uint256; never parse an unbounded RPC integer.
    if not isinstance(v, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})", v):
        raise QueryError("invalid RPC quantity")
    return int(v, 16)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QueryError("JSON 对象存在重复字段", "invalid_response")
        result[key] = value
    return result


def invalid_constant(value):
    raise QueryError("JSON 包含非标准数值常量", "invalid_response")


def mon(v):
    whole, part = divmod(v, 10**18)
    return f"{whole}.{part:018d}"


def metrics(txs, address):
    counts = collections.Counter(t["status"] for t in txs)
    incoming = [t for t in txs if t["to"] == address]
    callers = collections.Counter(t["from"] for t in incoming)
    peers = {a for t in txs for a in (t["from"], t["to"]) if a and a != address}
    ranking = [
        {
            "address": a,
            "count": n,
            "share_numerator": n,
            "share_denominator": len(incoming),
            "share_percent": f"{(n * 10000 // len(incoming)) // 100}.{(n * 10000 // len(incoming)) % 100:02d}",
        }
        for a, n in sorted(callers.items(), key=lambda x: (-x[1], x[0]))
    ]
    with_data = [t for t in incoming if t.get("has_input_data") is True]
    data_callers = collections.Counter(t["from"] for t in with_data)
    data_ranking = [
        {
            "address": a,
            "count": n,
            "share_numerator": n,
            "share_denominator": len(with_data),
        }
        for a, n in sorted(data_callers.items(), key=lambda x: (-x[1], x[0]))
    ]
    values = {}
    for label, key in [("in", "to"), ("out", "from")]:
        v = sum(int(t["value_wei"]) for t in txs if t[key] == address and t["status"] == "success")
        values[label + "_wei"] = str(v)
        values[label + "_mon"] = mon(v)
    return {
        "direct_transactions": len(txs),
        "success": counts["success"],
        "failed": counts["failed"],
        "unknown": counts["unknown"],
        "distinct_counterparties": len(peers),
        "direct_to_transactions": len(incoming),
        "distinct_direct_initiators": len(callers),
        "caller_ranking": ranking,
        "top_caller": ranking[0] if ranking else None,
        "repeated_direct_initiators": sum(n >= 2 for n in callers.values()),
        "direct_to_with_input_data": {
            "transactions": len(with_data),
            "distinct_initiators": len(data_callers),
            "caller_ranking": data_ranking,
            "repeated_initiators": sum(n >= 2 for n in data_callers.values()),
        },
        "direct_to_without_input_data": sum(t.get("has_input_data") is False for t in incoming),
        "direct_to_unknown_input_data": sum(t.get("has_input_data") is None for t in incoming),
        "native_value_successful_top_level_only": values,
        "concentration_denominator": "已查询区块中 to 等于目标地址的所有顶层交易（包括失败和状态未知；不是完整合约调用数）",
    }


def parse_block(b, n, address):
    """Validate a full block before selecting related top-level transactions."""
    try:
        if not b or quantity(b["number"]) != n or not re.fullmatch(r"0x[0-9a-fA-F]{64}", b["hash"]):
            raise QueryError("区块缺失或身份不符")
        if not isinstance(b.get("transactions"), list):
            raise QueryError("区块 transactions 必须是数组", "invalid_block")
        parent = b.get("parentHash")
        if not isinstance(parent, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", parent):
            raise QueryError("区块缺少有效 parentHash", "invalid_block")
        header = {"block": n, "hash": b["hash"].lower(), "parent_hash": parent.lower()}
        timestamp = datetime.datetime.fromtimestamp(
            quantity(b["timestamp"]), datetime.timezone.utc
        ).isoformat()
        found = []
        for t in b["transactions"]:
            if not isinstance(t, dict):
                raise QueryError("未返回完整交易对象")
            if "to" not in t or not isinstance(t.get("from"), str):
                raise QueryError("交易缺少必要地址字段", "invalid_transaction")
            if t["to"] is not None and not isinstance(t["to"], str):
                raise QueryError("交易 to 字段格式无效", "invalid_transaction")
            f, to = t["from"].lower(), t["to"].lower() if t["to"] is not None else None
            if to == "":
                raise QueryError("空 to 不等于合约创建的 null", "invalid_transaction")
            if not re.fullmatch(r"0x[0-9a-f]{40}", f) or (
                to and not re.fullmatch(r"0x[0-9a-f]{40}", to)
            ):
                raise QueryError("非法交易地址")
            if quantity(t["blockNumber"]) != n or t["blockHash"].lower() != b["hash"].lower():
                raise QueryError("交易与区块不一致")
            h = t["hash"].lower()
            if not re.fullmatch(r"0x[0-9a-f]{64}", h):
                raise QueryError("非法交易哈希")
            value = quantity(t["value"])
            if f != address and to != address:
                continue
            input_data = t.get("input")
            has_input_data = (
                (len(input_data) > 2)
                if isinstance(input_data, str)
                and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", input_data)
                else None
            )
            found.append(
                {
                    "hash": h,
                    "block": n,
                    "block_hash": b["hash"].lower(),
                    "timestamp": timestamp,
                    "from": f,
                    "to": to,
                    "value_wei": str(value),
                    "value_mon": mon(value),
                    "raw_transaction_fingerprint": hashlib.sha256(
                        json.dumps(
                            t, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ).encode()
                    ).hexdigest(),
                    "has_input_data": has_input_data,
                    "status": "unknown",
                    "explorer_url": EXPLORER + h,
                }
            )
        return found, header
    except QueryError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
        OSError,
        RecursionError,
    ):
        raise QueryError("invalid block data", "invalid_block") from None


def apply_receipt_status(r, t):
    """Only matching receipts with status 0/1 can resolve an unknown status."""
    try:
        if r is None:
            raise QueryError("RPC 返回 null 回执", "receipt_not_found")
        if not isinstance(r, dict):
            raise QueryError("回执不是对象", "invalid_receipt")
        if (
            r["transactionHash"].lower() != t["hash"]
            or r["blockHash"].lower() != t["block_hash"]
            or quantity(r["blockNumber"]) != t["block"]
        ):
            raise QueryError("回执交易或区块身份不一致", "receipt_mismatch")
        status = quantity(r["status"])
        if status not in (0, 1):
            raise QueryError("未知回执状态", "unknown_status")
        t["status"] = "success" if status else "failed"
    except QueryError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise QueryError("回执字段缺失或格式无效", "invalid_receipt") from None
