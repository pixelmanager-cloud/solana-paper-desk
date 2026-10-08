"""Read-only provider boundaries; no signing, sendTransaction, or sendBundle methods."""
import asyncio
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .model import digest

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SOL = "So11111111111111111111111111111111111111112"


class SubscriptionRejected(ValueError):
    pass


def api_key(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in your local environment or VPS secret file")
    return value


def fetch_json(url, payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        # Provider responses/URLs can contain API keys. Never echo raw exceptions.
        raise ValueError(f"Provider HTTP {exc.code}; check credentials, plan or rate limit") from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError("Provider connection or response error") from None


def helius_rpc(method, params):
    if method not in ("getTransactionsForAddress", "getMinimumBalanceForRentExemption", "getBlock", "getSlot", "getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts", "getTokenAccounts", "simulateTransaction", "simulateBundle", "getLatestBlockhash"):
        raise ValueError("read-only RPC method allowlist")
    url = "https://mainnet.helius-rpc.com/?" + urlencode({"api-key": api_key("HELIUS_API_KEY")})
    result = fetch_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in result or "result" not in result:
        raise ValueError("Helius RPC error; check plan, quota and request support")
    return result["result"]


def inspect_mint(mint):
    from .security import mint_policy
    result = helius_rpc("getAccountInfo", [mint, {"encoding": "base64", "commitment": "confirmed"}])
    return {"mint": mint, "observed_at": int(time.time()), "slot": result.get("context", {}).get("slot"),
            "policy": mint_policy(result.get("value")), "account": result.get("value")}


def backfill(ledger, address, start, end, max_pages=10, rpc=helius_rpc):
    if not 0 <= start < end or not 1 <= max_pages <= 100:
        raise ValueError("invalid bounded backfill range")
    query = {"address": address, "start": start, "end": end}
    key = "backfill:" + digest(query)
    row = ledger.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    checkpoint = json.loads(row[0]) if row else {"cursor": None, "complete": False}
    count = 0
    if checkpoint["complete"]:
        return {"complete": True, "new_records": 0}
    for _ in range(max_pages):
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100,
                "commitment": "finalized", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                "filters": {"blockTime": {"gte": start, "lt": end}, "status": "any",
                            "tokenAccounts": "balanceChanged"}}
        if checkpoint["cursor"]:
            opts["paginationToken"] = checkpoint["cursor"]
        result = rpc("getTransactionsForAddress", [address, opts])
        if not isinstance(result.get("data"), list):
            raise ValueError("Unexpected historical response schema")
        cursor = result.get("paginationToken")
        prior_cursors = checkpoint.get("seen_cursors", [])
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("Invalid historical cursor")
        if cursor and (cursor == checkpoint["cursor"] or cursor in prior_cursors):
            raise ValueError("Historical cursor cycle")
        next_checkpoint = {"cursor": cursor, "complete": cursor is None,
            "seen_cursors": prior_cursors + ([cursor] if cursor else []),
            "pages": checkpoint.get("pages", []) + [{"request_cursor":checkpoint["cursor"],
                "response_hash":digest(result),"records":len(result["data"])}]}
        # Page payloads and pagination watermark commit together. A crash cannot
        # advance the cursor past records that were never durably stored.
        ledger.db.execute("BEGIN IMMEDIATE")
        page_count=0
        try:
            for item in result["data"]:
                signature = item.get("signature") or (item.get("transaction", {}).get("signatures") or [None])[0]
                source = "history:" + (signature or digest(item))
                page_count += ledger.record_raw(source, int(time.time()), item.get("slot"), item)
            ledger.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps(next_checkpoint)))
            ledger.db.execute("COMMIT")
        except BaseException:
            ledger.db.execute("ROLLBACK")
            raise
        count += page_count
        checkpoint = next_checkpoint
        if checkpoint["complete"]:
            break
    ledger.health(int(time.time()), "BACKFILL_COMPLETE" if checkpoint["complete"] else "BACKFILL_PAGE_LIMIT", query)
    return {"complete": checkpoint["complete"], "new_records": count,
            "notice": "Address-query completion does not establish whole-token history coverage."}


def subscription(addresses):
    if not addresses:
        raise ValueError("at least one address filter required")
    return {"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe", "params": [
        {"vote": False, "failed": False, "accountInclude": addresses},
        {"commitment": "confirmed", "encoding": "jsonParsed", "transactionDetails": "full",
         "showRewards": False, "maxSupportedTransactionVersion": 1}]}


def record_notification(ledger, message, now):
    if message.get("method") != "transactionNotification":
        return False
    result = message.get("params", {}).get("result", {})
    signature, slot = result.get("signature"), result.get("slot")
    if not isinstance(signature, str) or not signature or type(slot) is not int:
        raise ValueError("Unexpected notification schema")
    return ledger.record_raw("confirmed:" + signature, now, slot, message)


async def record_stream(ledger, addresses, seconds, max_records, max_bytes):
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise ValueError("Install requirements-live.txt for streaming") from None
    if not 1 <= seconds <= 86400 or max_records < 1 or max_bytes < 1:
        raise ValueError("recording requires positive bounded duration/count/bytes")
    key = api_key("HELIUS_API_KEY")
    url = "wss://mainnet.helius-rpc.com/?" + urlencode({"api-key": key})
    deadline = time.monotonic() + seconds
    count, received_bytes, attempts, connections = 0, 0, 0, 0
    ledger.health(int(time.time()), "CAPTURE_START_UNVERIFIED", {"addresses": addresses})
    while time.monotonic() < deadline and count < max_records and received_bytes < max_bytes:
        try:
            async with connect(url, ping_interval=20, ping_timeout=20, open_timeout=10,
                               max_size=8 * 1024 * 1024, max_queue=32) as ws:
                await ws.send(json.dumps(subscription(addresses)))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if ack.get("id") != 1 or "result" not in ack or "error" in ack:
                    raise SubscriptionRejected("Helius subscription rejected; check key and Developer entitlement")
                connections += 1
                ledger.health(int(time.time()), "STREAM_CONNECTED", {"attempt": attempts})
                while time.monotonic() < deadline and count < max_records and received_bytes < max_bytes:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(30, max(.01, deadline - time.monotonic())))
                    received_bytes += len(raw.encode() if isinstance(raw, str) else raw)
                    count += record_notification(ledger, json.loads(raw), int(time.time()))
        except SubscriptionRejected:
            ledger.health(int(time.time()), "SUBSCRIPTION_REJECTED", {})
            raise
        except asyncio.TimeoutError:
            ledger.health(int(time.time()), "STREAM_IDLE_OR_TIMEOUT", {})
        except Exception as exc:
            # Explicit evidence gap; reconnect alone never marks history complete.
            ledger.health(int(time.time()), "STREAM_GAP", {"error_class": type(exc).__name__})
        attempts += 1
        if time.monotonic() < deadline and count < max_records and received_bytes < max_bytes:
            await asyncio.sleep(min(2 ** min(attempts, 4), max(0, deadline - time.monotonic())))
    if not connections:
        raise ValueError("Helius connection not established; inspect health records for error classes")
    result = {"new_records": count, "received_bytes": received_bytes, "history_complete": False,
              "status": "CAPTURED" if count else "CONNECTED_NO_EVENTS"}
    ledger.health(int(time.time()), "CAPTURE_STOP", result)
    return result


def jupiter_probe(input_mint, output_mint, amount, taker, *, max_accounts=None, for_bundle=False):
    if amount <= 0:
        raise ValueError("amount must be positive integer base units")
    query = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount),
             "taker": taker, "slippageBps": "100", "transactionVersion": "0"}
    if max_accounts is not None:
        if type(max_accounts) is not int or not 1<=max_accounts<=64:raise ValueError("Invalid route account limit")
        query['maxAccounts']=str(max_accounts)
    if type(for_bundle) is not bool:raise ValueError("Invalid bundle routing flag")
    if for_bundle:query['forJitoBundle']='true'
    result = fetch_json("https://api.jup.ag/swap/v2/build?" + urlencode(query),
                        headers={"x-api-key": api_key("JUPITER_API_KEY")})
    for field in ("inAmount", "outAmount", "routePlan"):
        if field not in result:
            raise ValueError("Unexpected Jupiter build response")
    return {"kind": "unsigned_route_probe", "observed_at": int(time.time()), "request": query,
            "response": result, "notice": "No signing or submission. Quote is not a guaranteed fill."}


def jupiter_sequence_probe(input_mint,output_mint,amount,taker):
    return jupiter_probe(input_mint,output_mint,amount,taker,max_accounts=32,for_bundle=True)
