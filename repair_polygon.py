# -*- coding: utf-8 -*-
"""負残高アドレスだけを狙って、Etherscan APIから完全な送受信履歴を取り直して
抜けているイベントを補完する一回限りの修復スクリプト。"""
import os
import sys
import json
import time
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import JPYC_CONTRACT, TRANSFER_TOPIC, DATA_DIR
from collector import etherscan_get_logs, decode_log_etherscan

CHAIN = "polygon"
CHAIN_ID = 137
API_KEY = os.environ["ETHERSCAN_API_KEY"]


def fetch_all_for_topic(topic_pos, addr_topic, from_block, to_block):
    """topic1(送信元)またはtopic2(受信先)を固定して全件ページングで取得。"""
    out = []
    lo = from_block
    while lo <= to_block:
        hi = to_block
        while True:
            kwargs = {"chainid": CHAIN_ID, "module": "logs", "action": "getLogs",
                      "address": JPYC_CONTRACT, "topic0": TRANSFER_TOPIC,
                      f"topic0_{topic_pos}_opr": "and", f"topic{topic_pos}": addr_topic,
                      "fromBlock": lo, "toBlock": hi, "page": 1, "offset": 1000,
                      "apikey": API_KEY}
            import urllib.parse, urllib.request
            url = "https://api.etherscan.io/v2/api?" + urllib.parse.urlencode(kwargs)
            all_logs = []
            page = 1
            truncated = False
            while True:
                kwargs["page"] = page
                url = "https://api.etherscan.io/v2/api?" + urllib.parse.urlencode(kwargs)
                req = urllib.request.Request(url, headers={"User-Agent": "jpyc-panel/1.0"})
                for attempt in range(6):
                    try:
                        with urllib.request.urlopen(req, timeout=30) as r:
                            data = json.loads(r.read().decode())
                        break
                    except Exception:
                        time.sleep(2 * (attempt + 1))
                else:
                    raise RuntimeError("repeated failure")
                if data.get("status") == "0":
                    msg = str(data.get("message", ""))
                    if "No records found" in msg:
                        logs = []
                    else:
                        raise RuntimeError(f"etherscan error: {data}")
                else:
                    logs = data.get("result") or []
                all_logs.extend(logs)
                if len(logs) < 1000:
                    break
                page += 1
                if page > 10:
                    truncated = True
                    break
                time.sleep(0.25)
            if truncated:
                hi = (lo + hi) // 2
                continue
            out.extend(all_logs)
            break
        lo = hi + 1
    return out


def main():
    master_path = os.path.join("output", f"address_master_{CHAIN}.csv")
    master = pd.read_csv(master_path)
    neg = master[master["balance"] < -1e-9]
    print(f"負残高アドレス: {len(neg)}件")

    ev_path = os.path.join(DATA_DIR, f"events_{CHAIN}.parquet")
    ev = pd.read_parquet(ev_path)
    existing_keys = set(zip(ev["tx_hash"], ev["log_index"]))

    new_rows = []
    for i, addr in enumerate(neg["address"]):
        addr_topic = "0x" + "0" * 24 + addr[2:]
        for pos in (1, 2):
            logs = fetch_all_for_topic(pos, addr_topic, 0, 100_000_000)
            for lg in logs:
                key_tx = lg["transactionHash"]
                li = lg.get("logIndex")
                key_li = 0 if li in (None, "", "0x") else int(li, 16)
                if (key_tx, key_li) not in existing_keys:
                    row = decode_log_etherscan(lg)
                    new_rows.append(row)
                    existing_keys.add((key_tx, key_li))
        print(f"  {i+1}/{len(neg)} {addr} 確認済み (追加候補 {len(new_rows)}件)", end="\r")
        time.sleep(0.1)

    print(f"\n補完する新規イベント: {len(new_rows)}件")
    if new_rows:
        add_df = pd.DataFrame(new_rows)
        ev2 = pd.concat([ev, add_df], ignore_index=True)
        ev2 = ev2.drop_duplicates(subset=["tx_hash", "log_index", "from", "to", "value_raw"])
        ev2 = ev2.sort_values(["block", "log_index"]).reset_index(drop=True)
        ev2.to_parquet(ev_path, index=False)
        print(f"保存しました: {ev_path} (合計 {len(ev2):,} 件)")
    else:
        print("補完対象なし")


if __name__ == "__main__":
    main()
