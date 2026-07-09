# -*- coding: utf-8 -*-
"""
JPYC オンチェーン・パネルデータ基盤 設定ファイル
対象: 新JPYC(電子決済手段) 0xE7C3D8C9a439feDe00D2600032D5dB0Be71C3c29
     (Ethereum / Polygon / Avalanche / Kaia 共通アドレス)
※ 前払式JPYC(JPYC Prepaid, 旧v1)は別コントラクト。混ぜないこと。
"""

JPYC_CONTRACT = "0xE7C3D8C9a439feDe00D2600032D5dB0Be71C3c29"

# ERC-20 Transfer(address,address,uint256) のイベントシグネチャ
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ローンチは2025-10-27。デプロイブロックは自動探索するが、
# 探索の下限としてこの日付近のブロックを使う(全ゼロから探索しても動くが遅い)。
LAUNCH_DATE = "2025-10-01"  # 余裕を持って10月頭から

CHAINS = {
    "ethereum": {
        "chain_id": 1,
        "rpcs": [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.drpc.org",
            "https://eth.llamarpc.com",
        ],
        "init_chunk": 20_000,   # eth_getLogs の初期ブロック幅(自動調整される)
        "ts_grid_step": 5_000,  # タイムスタンプ補間グリッドの間隔(ブロック)
        # 公開RPC各社の eth_getLogs が(この日は521エラー等で)不安定だったため、
        # ETHERSCAN_API_KEY があればPolygonと同じくEtherscan経由に切り替える。
        "etherscan_chain_id": 1,
    },
    "polygon": {
        "chain_id": 137,
        "rpcs": [
            "https://polygon.drpc.org",
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
        ],
        "init_chunk": 2_000,
        "ts_grid_step": 20_000,
        # 素のRPCは eth_getLogs のブロック幅制限が厳しく実用的な時間で終わらないため、
        # ETHERSCAN_API_KEY 環境変数があれば Etherscan(v2統合API)経由の索引検索を使う。
        # 無ければ自動的に通常のRPC走査にフォールバックする。
        "etherscan_chain_id": 137,
    },
    "avalanche": {
        "chain_id": 43114,
        "rpcs": [
            "https://api.avax.network/ext/bc/C/rpc",
            "https://avalanche-c-chain-rpc.publicnode.com",
            "https://avax.drpc.org",
        ],
        "init_chunk": 2_000,    # 公式RPCは2048ブロック上限
        "ts_grid_step": 20_000,
    },
    "kaia": {
        "chain_id": 8217,
        "rpcs": [
            "https://public-en.node.kaia.io",
            "https://kaia.drpc.org",
        ],
        "init_chunk": 5_000,
        "ts_grid_step": 40_000,  # ブロック1秒間隔なので粗くてよい
    },
}

# ---- パネル構築のパラメータ ----

# ダスト感度分析の閾値(単位: JPYC = 円)。
# holders_gt0 / holders_ge1 / holders_ge100 / holders_ge1000 / holders_ge10000 の列になる。
DUST_THRESHOLDS = [1.0, 100.0, 1_000.0, 10_000.0]

# 保有分布のバケット(円)。論文の「1万円未満82-98%」の検証には <10_000 を見る。
BALANCE_BUCKETS = [
    ("lt_1k",      0,        1_000),
    ("1k_10k",     1_000,    10_000),
    ("10k_100k",   10_000,   100_000),
    ("100k_1m",    100_000,  1_000_000),
    ("ge_1m",      1_000_000, float("inf")),
]

# コホート保持分析で「保有継続」とみなす残高閾値(円)
RETENTION_THRESHOLD = 1.0

# 出力ディレクトリ
DATA_DIR = "data"      # 生イベント(parquet)とチェックポイント
OUT_DIR = "output"     # パネルCSV・図表
