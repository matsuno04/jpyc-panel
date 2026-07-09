# -*- coding: utf-8 -*-
"""
collector.py — JPYCの全Transferイベントをパブリック RPC から取得して parquet に保存する。

使い方:
    python collector.py                 # 全4チェーン
    python collector.py polygon         # 1チェーンだけ
    python collector.py polygon kaia    # 複数指定

特徴:
- eth_getLogs をブロック幅を自動調整(エラーで半減、成功が続けば拡大)しながら走査
- checkpoint (data/checkpoint_{chain}.json) に進捗を保存 → 中断しても再開できる
- タイムスタンプはグリッド補間(数百回のブロック取得で全イベントに日時を付与)
- decimals() をコントラクトから読み取り metadata に保存(誤桁を防ぐ)
- デプロイブロックは eth_getCode の二分探索で自動特定

出力:
    data/events_{chain}.parquet   … 列: block, log_index, tx_hash, from, to, value_raw, ts
    data/meta_{chain}.json        … decimals, deploy_block, last_scanned など
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows既定(cp932)での日本語出力の文字化け/エラーを防ぐ

from config import (JPYC_CONTRACT, TRANSFER_TOPIC, CHAINS, DATA_DIR)
from rpc import Rpc, RpcError

MIN_CHUNK = 64
GROW_AFTER = 8  # 連続成功でチャンク幅を1.5倍

# ---- Etherscan(v2 統合API)経由の取得 ----
# Polygonのように素の公開RPCでは eth_getLogs のブロック幅制限が厳しすぎて
# 現実的な時間で終わらないチェーン向け。索引化済みDBを検索するため速い。
# 無料APIキー(courriel登録のみ・課金不要)を環境変数 ETHERSCAN_API_KEY に設定して使う。
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
ETHERSCAN_PAGE_SIZE = 1000
# 深いページネーション(3ページ以上)を跨ぐと、まれに境界でイベントが
# 欠落することが判明したため、早めに範囲を縮めて多ページ化を避ける。
ETHERSCAN_MAX_PAGES = 2


def etherscan_get_logs(chain_id, address, topic0, from_block, to_block, api_key, page,
                        max_retries=6):
    params = {
        "chainid": chain_id, "module": "logs", "action": "getLogs",
        "address": address, "topic0": topic0,
        "fromBlock": from_block, "toBlock": to_block,
        "page": page, "offset": ETHERSCAN_PAGE_SIZE, "apikey": api_key,
    }
    url = ETHERSCAN_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "jpyc-panel/1.0"})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            if data.get("status") == "0":
                msg = str(data.get("message", ""))
                if "No records found" in msg:
                    return []
                # レート制限・一時的なエラーはリトライ、それ以外は諦める
                if any(k in msg.lower() for k in ("rate limit", "max calls", "try again", "timeout")):
                    raise RuntimeError(f"etherscan transient error: {msg}")
                raise RuntimeError(f"etherscan error: {data.get('result') or msg}")
            return data.get("result") or []
        except Exception as e:
            last_err = e
            time.sleep(min(2 * (attempt + 1), 15))
    raise RuntimeError(f"etherscan: {max_retries}回リトライしても失敗: {last_err}")


def decode_log_etherscan(lg):
    li = lg.get("logIndex")
    li = 0 if li in (None, "", "0x") else int(li, 16)
    return {
        "block": int(lg["blockNumber"], 16),
        "log_index": li,
        "tx_hash": lg["transactionHash"],
        "from": "0x" + lg["topics"][1][-40:].lower(),
        "to": "0x" + lg["topics"][2][-40:].lower(),
        "value_raw": str(int(lg["data"], 16)) if lg["data"] not in ("0x", "") else "0",
        "ts": int(lg["timeStamp"], 16),  # Etherscan APIはログ毎に正確なtsを返すので補間不要
    }


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


def find_deploy_block(rpc, address, hi):
    """eth_getCode の二分探索でコントラクト作成ブロックを特定する。"""
    lo = 0
    if rpc.get_code(address, hi) in ("0x", None):
        raise RuntimeError("コントラクトが見つかりません(アドレス/チェーンを確認)")
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            code = rpc.get_code(address, mid)
        except RpcError:
            # アーカイブでないノードは古いブロックの getCode に失敗することがある。
            # その場合は「コードあり」とみなして下側を探索(安全側: 早いブロックから走査)
            code = "0x1"
        if code in ("0x", None):
            lo = mid + 1
        else:
            hi = mid
        time.sleep(0.05)
    return lo


def decode_log(lg):
    return {
        "block": int(lg["blockNumber"], 16),
        "log_index": int(lg["logIndex"], 16),
        "tx_hash": lg["transactionHash"],
        "from": "0x" + lg["topics"][1][-40:].lower(),
        "to": "0x" + lg["topics"][2][-40:].lower(),
        # uint256 は float64 で桁落ちするため文字列で保持し、builder 側で変換
        "value_raw": str(int(lg["data"], 16)) if lg["data"] not in ("0x", "") else "0",
    }


def fetch_decimals(rpc):
    try:
        res = rpc.eth_call(JPYC_CONTRACT, "0x313ce567")  # decimals()
        return int(res, 16)
    except Exception:
        return 18  # 取得失敗時はERC-20標準の18を仮定(metaに記録される)


def build_ts_grid(rpc, lo, hi, step):
    """block→timestamp の補間用グリッドを取得。"""
    blocks = list(range(lo, hi + 1, step))
    if blocks[-1] != hi:
        blocks.append(hi)
    ts = []
    for i, b in enumerate(blocks):
        ts.append(rpc.block_timestamp(b))
        if i % 20 == 0:
            print(f"    ts grid {i+1}/{len(blocks)}", end="\r")
        time.sleep(0.05)
    print()
    return np.array(blocks, dtype=np.int64), np.array(ts, dtype=np.int64)


def collect_chain(chain):
    cfg = CHAINS[chain]
    os.makedirs(DATA_DIR, exist_ok=True)
    ckpt_path = os.path.join(DATA_DIR, f"checkpoint_{chain}.json")
    meta_path = os.path.join(DATA_DIR, f"meta_{chain}.json")
    shard_dir = os.path.join(DATA_DIR, f"shards_{chain}")
    os.makedirs(shard_dir, exist_ok=True)

    rpc = Rpc(cfg["rpcs"])
    latest = rpc.latest_block()
    # 直近64ブロックはリオーグ回避のため走査しない
    target = latest - 64

    meta = load_json(meta_path, {})
    if "decimals" not in meta:
        meta["decimals"] = fetch_decimals(rpc)
    if "deploy_block" not in meta:
        print(f"[{chain}] デプロイブロックを二分探索中…")
        meta["deploy_block"] = find_deploy_block(rpc, JPYC_CONTRACT, target)
        print(f"[{chain}] deploy_block = {meta['deploy_block']}")
    save_json(meta_path, meta)

    ckpt = load_json(ckpt_path, {"next_block": meta["deploy_block"], "shard": 0})
    frm = ckpt["next_block"]
    chunk = cfg["init_chunk"]
    ok_streak = 0
    buf = []
    n_events_total = 0
    t0 = time.time()

    print(f"[{chain}] scan {frm:,} → {target:,}")
    while frm <= target:
        to = min(frm + chunk - 1, target)
        try:
            logs = rpc.get_logs(JPYC_CONTRACT, TRANSFER_TOPIC, frm, to)
        except RpcError as e:
            # 範囲超過・結果多すぎ等 → チャンクを半減してリトライ
            if chunk <= MIN_CHUNK:
                print(f"\n[{chain}] 幅{chunk}でも失敗: {e} → エンドポイント切替して待機")
                rpc.i += 1
                time.sleep(5)
                continue
            chunk = max(MIN_CHUNK, chunk // 2)
            ok_streak = 0
            continue

        buf.extend(decode_log(lg) for lg in logs if not lg.get("removed"))
        n_events_total += len(logs)
        ok_streak += 1
        if ok_streak >= GROW_AFTER:
            chunk = min(int(chunk * 1.5), cfg["init_chunk"] * 4)
            ok_streak = 0

        frm = to + 1
        elapsed = time.time() - t0
        print(f"[{chain}] block {frm:,}/{target:,}  events={n_events_total:,} "
              f"chunk={chunk}  {elapsed:,.0f}s", end="\r")

        # 5万件たまるか終端でシャード書き出し + チェックポイント
        if len(buf) >= 50_000 or frm > target:
            if buf:
                df = pd.DataFrame(buf)
                shard = os.path.join(shard_dir, f"{ckpt['shard']:05d}.parquet")
                df.to_parquet(shard, index=False)
                ckpt["shard"] += 1
                buf = []
            ckpt["next_block"] = frm
            save_json(ckpt_path, ckpt)
        time.sleep(0.1)  # パブリックRPCへの礼儀

    print(f"\n[{chain}] 走査完了。タイムスタンプ付与中…")

    # ---- シャード統合 + タイムスタンプ補間 ----
    shards = sorted(os.path.join(shard_dir, f) for f in os.listdir(shard_dir)
                    if f.endswith(".parquet"))
    if not shards:
        print(f"[{chain}] イベント0件")
        return
    ev = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    ev = ev.drop_duplicates(subset=["tx_hash", "log_index"])
    ev = ev.sort_values(["block", "log_index"]).reset_index(drop=True)

    lo, hi = int(ev["block"].min()), int(ev["block"].max())
    gb, gt = build_ts_grid(rpc, lo, hi, cfg["ts_grid_step"])
    ev["ts"] = np.interp(ev["block"].values, gb, gt).astype("int64")

    out = os.path.join(DATA_DIR, f"events_{chain}.parquet")
    ev.to_parquet(out, index=False)
    meta["last_scanned"] = int(target)
    meta["n_events"] = int(len(ev))
    save_json(meta_path, meta)
    print(f"[{chain}] 保存: {out}  ({len(ev):,} events, "
          f"{pd.to_datetime(ev.ts.min(), unit='s').date()} 〜 "
          f"{pd.to_datetime(ev.ts.max(), unit='s').date()})")


def collect_chain_etherscan(chain, api_key):
    """Etherscan(v2統合API)のgetLogsで走査する版。索引化済みなので
    素のRPCよりブロック範囲の制限が緩く、ログにtimeStampも直接含まれる。"""
    cfg = CHAINS[chain]
    chain_id = cfg["etherscan_chain_id"]
    os.makedirs(DATA_DIR, exist_ok=True)
    ckpt_path = os.path.join(DATA_DIR, f"checkpoint_{chain}.json")
    meta_path = os.path.join(DATA_DIR, f"meta_{chain}.json")
    shard_dir = os.path.join(DATA_DIR, f"shards_{chain}")
    os.makedirs(shard_dir, exist_ok=True)

    rpc = Rpc(cfg["rpcs"])  # deploy_block探索・decimals・latest_blockの取得のみに使う
    latest = rpc.latest_block()
    target = latest - 64

    meta = load_json(meta_path, {})
    if "decimals" not in meta:
        meta["decimals"] = fetch_decimals(rpc)
    if "deploy_block" not in meta:
        print(f"[{chain}] デプロイブロックを二分探索中…")
        meta["deploy_block"] = find_deploy_block(rpc, JPYC_CONTRACT, target)
        print(f"[{chain}] deploy_block = {meta['deploy_block']}")
    save_json(meta_path, meta)

    ckpt = load_json(ckpt_path, {"next_block": meta["deploy_block"], "shard": 0})
    frm = ckpt["next_block"]
    chunk = 300_000
    ok_streak = 0
    buf = []
    n_events_total = 0
    t0 = time.time()

    print(f"[{chain}] (Etherscan API) scan {frm:,} → {target:,}")
    while frm <= target:
        to = min(frm + chunk - 1, target)
        page = 1
        range_logs = []
        truncated = False
        while True:
            logs = etherscan_get_logs(chain_id, JPYC_CONTRACT, TRANSFER_TOPIC,
                                       frm, to, api_key, page)
            range_logs.extend(logs)
            if len(logs) < ETHERSCAN_PAGE_SIZE:
                break
            page += 1
            if page > ETHERSCAN_MAX_PAGES:
                truncated = True  # 1クエリ上限(1万件)超過 → 範囲を縮めてやり直し
                break
            time.sleep(0.25)  # 無料枠(秒5回)を守る

        if truncated:
            if chunk <= 1:
                # 1ブロックでも1万件を超える異常事態。データは受け入れて先に進む
                print(f"\n[{chain}] [WARN] block {frm} だけで1万件超。切り捨てて先に進みます")
                buf.extend(decode_log_etherscan(lg) for lg in range_logs)
                n_events_total += len(range_logs)
                frm = to + 1
                continue
            chunk = max(1, chunk // 4)  # 密集区間では大胆に縮める
            ok_streak = 0
            time.sleep(0.25)
            continue

        buf.extend(decode_log_etherscan(lg) for lg in range_logs)
        n_events_total += len(range_logs)
        ok_streak += 1
        if ok_streak >= 4:
            chunk = min(int(chunk * 1.5), 500_000)
            ok_streak = 0

        frm = to + 1
        elapsed = time.time() - t0
        print(f"[{chain}] block {frm:,}/{target:,}  events={n_events_total:,} "
              f"chunk={chunk}  {elapsed:,.0f}s", end="\r")

        if len(buf) >= 50_000 or frm > target:
            if buf:
                df = pd.DataFrame(buf)
                shard = os.path.join(shard_dir, f"{ckpt['shard']:05d}.parquet")
                df.to_parquet(shard, index=False)
                ckpt["shard"] += 1
                buf = []
            ckpt["next_block"] = frm
            save_json(ckpt_path, ckpt)
        time.sleep(0.25)

    print(f"\n[{chain}] 走査完了。")

    shards = sorted(os.path.join(shard_dir, f) for f in os.listdir(shard_dir)
                    if f.endswith(".parquet"))
    if not shards:
        print(f"[{chain}] イベント0件")
        return
    ev = pd.concat([pd.read_parquet(s) for s in shards], ignore_index=True)
    # Etherscan APIはまれにlogIndexを空("0x")で返すことがあり、その場合0として
    # 埋めているため、tx_hash+log_indexだけだと同一tx内の別ログを誤って重複と
    # みなす恐れがある。from/to/valueも含めて重複判定することでこれを避ける。
    ev = ev.drop_duplicates(subset=["tx_hash", "log_index", "from", "to", "value_raw"])
    ev = ev.sort_values(["block", "log_index"]).reset_index(drop=True)

    out = os.path.join(DATA_DIR, f"events_{chain}.parquet")
    ev.to_parquet(out, index=False)
    meta["last_scanned"] = int(target)
    meta["n_events"] = int(len(ev))
    save_json(meta_path, meta)
    print(f"[{chain}] 保存: {out}  ({len(ev):,} events, "
          f"{pd.to_datetime(ev.ts.min(), unit='s').date()} 〜 "
          f"{pd.to_datetime(ev.ts.max(), unit='s').date()})")


if __name__ == "__main__":
    targets = sys.argv[1:] or list(CHAINS)
    api_key = os.environ.get("ETHERSCAN_API_KEY")
    for c in targets:
        if c not in CHAINS:
            print(f"未知のチェーン: {c} (選択肢: {list(CHAINS)})")
            continue
        cfg = CHAINS[c]
        if cfg.get("etherscan_chain_id"):
            if api_key:
                collect_chain_etherscan(c, api_key)
            else:
                print(f"[{c}] ETHERSCAN_API_KEY未設定のため通常のRPC走査にフォールバックします")
                collect_chain(c)
        else:
            collect_chain(c)
