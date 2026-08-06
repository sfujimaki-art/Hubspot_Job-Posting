# -*- coding: utf-8 -*-
"""求人ステージ(パイプライン)を求人ステータスに追従させる共通ヘルパー (2026-08-06)。

なぜ必要か:
  求人の状態は kyuujin_status(公開前/公開中/公開終了) と
  hs_pipeline_stage(募集準備中/募集中/選考進行中/採用決定/募集停止) の2系統で
  持たれているが、日次取込は前者しか更新していなかった。そのため
  「公開終了なのにボード上は募集中」が1,083件、「ステージ未設定でボードに
  出ない」が32,626件まで積み上がっていた(2026-08-05 実測)。
  過去に set_listing_stages.py でワンオフ是正されたが取込に組み込まれず凍結。
  同じ轍を踏まないよう、取込のたびに追従させる。

保護ルール:
  「選考進行中」「採用決定」は人が動かす領域なので**機械は上書きしない**。
  これらに居る求人には stage を書かない (呼び出し側で現ステージを渡す)。
"""
from __future__ import annotations

from typing import Optional

PIPELINE_ID = "t_5f0228538e8b269ecd3b553b104c008c"      # 求人票管理
STAGE_JUNBI = "1343099546"      # 募集準備中
STAGE_BOSHUU = "1343099547"     # 募集中
STAGE_SENKOU = "1343099548"     # 選考進行中   ← 人が動かす
STAGE_KETTEI = "1343099549"     # 採用決定     ← 人が動かす
STAGE_TEISHI = "1343099550"     # 募集停止

# kyuujin_status → ステージ
STATUS_TO_STAGE = {
    "公開前": STAGE_JUNBI,
    "公開中": STAGE_BOSHUU,
    "公開終了": STAGE_TEISHI,
}
# 機械が触ってはいけないステージ (人の判断が入っている)
PROTECTED_STAGES = (STAGE_SENKOU, STAGE_KETTEI)


def stage_props(normalized_status: Optional[str],
                current_stage: Optional[str] = None) -> dict:
    """求人ステータスに対応する {hs_pipeline, hs_pipeline_stage} を返す。

    Args:
        normalized_status: kyuujin_status の値 (公開前/公開中/公開終了)
        current_stage: 現在の hs_pipeline_stage (分かる場合)。
            選考進行中/採用決定なら空dictを返し、上書きを避ける。

    Returns:
        更新プロパティのdict。対象外なら {} (何も書かない)。
    """
    if not normalized_status:
        return {}
    stage = STATUS_TO_STAGE.get(normalized_status)
    if not stage:
        return {}
    if current_stage in PROTECTED_STAGES:
        return {}          # 人が動かしたステージは踏み潰さない
    return {"hs_pipeline": PIPELINE_ID, "hs_pipeline_stage": stage}
