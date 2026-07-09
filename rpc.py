# -*- coding: utf-8 -*-
"""軽量JSON-RPCクライアント。複数エンドポイントのローテーションとリトライ付き。"""
import json
import time
import random
import socket
import urllib.request
import urllib.error

# 環境によってはIPv6経路が不調で、Pythonの素のurllibだと(IPv4なら一瞬のところ)
# 長時間タイムアウトするまで待たされることがある。プロセス全体でIPv4のみを使うよう強制する。
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, endpoints, timeout=30, max_retries=6):
        self.endpoints = list(endpoints)
        self.i = 0
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0

    def _post(self, url, payload):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "jpyc-panel/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def call(self, method, params):
        """1回のRPC呼び出し。失敗したら次のエンドポイントに切替えつつリトライ。"""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id,
                   "method": method, "params": params}
        last_err = None
        for attempt in range(self.max_retries):
            url = self.endpoints[self.i % len(self.endpoints)]
            try:
                res = self._post(url, payload)
                if "error" in res and res["error"]:
                    # レンジ超過などのアプリケーションエラーは呼び出し元で処理したいので
                    # メッセージ付きで投げる
                    raise RpcError(str(res["error"]))
                return res["result"]
            except RpcError:
                raise  # ノードが明示的に返したエラー(範囲超過など)は即座に上へ
            except Exception as e:  # ネットワーク/HTTPエラー → エンドポイント切替
                last_err = e
                self.i += 1
                time.sleep(min(1.5 * (attempt + 1), 8) + random.random())
        raise RpcError(f"all endpoints failed: {last_err}")

    # ---- よく使うメソッドの薄いラッパー ----
    def latest_block(self):
        return int(self.call("eth_blockNumber", []), 16)

    def get_code(self, address, block):
        return self.call("eth_getCode", [address, hex(block)])

    def block_timestamp(self, block):
        b = self.call("eth_getBlockByNumber", [hex(block), False])
        if b is None:
            raise RpcError(f"block {block} not found")
        return int(b["timestamp"], 16)

    def get_logs(self, address, topic0, from_block, to_block):
        return self.call("eth_getLogs", [{
            "address": address,
            "topics": [topic0],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }])

    def eth_call(self, to, data):
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])
