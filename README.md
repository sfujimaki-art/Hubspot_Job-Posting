# Hubspot_Job-Posting

求人連携システムのクラウドIP SSO痎通検証 (会社アカウント・本番分離)。

- .github/workflows/sso_cloudip_test.yml : 手動trigger workflow
- .github/scripts/sso_cloudip_test.py : 自己完結SSO検証(シート/HubSpot不要)

Secret: HRHACKER_USER/HRHACKER_PASS または AW_LOGIN_ID/AW_PW

## AirWork 有料広告実績 (週次) の取得と HubSpot 取込 (ローカル PC 実行)

AirWork は GitHub Actions の IP を遮断するため、別のローカル PC で実行する。
画面は「有料広告 → 効果を分析する」(`/advertising/analysis`)。データの種類は
「有料広告実績データ」、集計単位は「求人」、期間は月曜〜日曜の 1 週間。
生成は非同期で、単位ごとに同時に作れるのは 1 ファイル。作成から 24 時間で消える。

### 取得 (`aw_orchestrator.py` の billing フェーズ)

```bash
# 毎週 (月曜以降): 直近の完了週を作成 → 少し待って回収
python -m scripts.job_application_sync.fetchers.aw_orchestrator --phase billing-create --from-active-deals --limit 20
python -m scripts.job_application_sync.fetchers.aw_orchestrator --phase billing-collect

# 1 コマンドで (待ちながら) 取る場合
python -m scripts.job_application_sync.fetchers.aw_orchestrator --phase billing-full --from-active-deals --parallel 3

# 特定の週 / 特定のアカウントだけ
... --phase billing-full --week-start 2026-10-05 --target-login-ids <id1>,<id2>

# バックフィル (2025-05 から直近の完了週まで。口座ごとに 1 週ずつ create→collect→次の週)
... --phase billing-full --from 2025-05-01 --from-active-deals --parallel 3
# --to で終端を指定可。取得済みの週 (保存済み ZIP) と「データなし」確定の週は自動でスキップするので、中断しても同じコマンドで再開できる
```

- 保存先は既定 `~/airwork-exports/billing/<週の月曜>/` (`--billing-dir` で変更)。ファイル名はログイン ID を含まないハッシュ。ハッシュ→ID の対応は同ディレクトリの `billing_manifest.json` (ローカルのみ・コミットしない)。
- `--dry-run` で対象の列挙だけ (ログインしない)。billing は HubSpot に書かないので `--actual` は不要。
- 状態: `data/job_application_sync/aw_billing_queue.json` (create 済み・未回収)、`aw_billing_empty.json` (データなし確定の週)。
- 初回は必ず `--headful --limit 1` で 1 社だけ動かし、期間ピッカー・「求人」ラジオ・ダウンロード行が意図どおり動くか目視確認する (`fetchers/aw_billing_fetcher.py` 冒頭の注意参照)。

### HubSpot 取込 (`airwork_billing_import.py`)

```bash
export HUBSPOT_ACCESS_TOKEN=...            # または .env
python scripts/job_application_sync/airwork_billing_import.py                # dry-run (件数のみ)
python scripts/job_application_sync/airwork_billing_import.py --limit 3 --apply   # 試走
python scripts/job_application_sync/airwork_billing_import.py --apply
```

LISTING(0-420) を (`airwork_account_login_id`, `id_airwork`=参照番号) の両方一致で突合し、
`airwork_weekly_perf` (JSON) と要約 6 項目を更新する。先に HubSpot 側に同名プロパティを作っておくこと
(スクリプトは作らない)。同じ週は置き換えるので再取込しても二重加算されない。

## 媒体から消えた「掲載中」求人を公開終了にする (2026-10-15)

媒体の一覧から消えた求人が HubSpot で掲載中のまま残らないようにする。定数は `end_vanished_listings.py` の先頭。

| 項目 | 値 / ルール |
|---|---|
| `HRH_VANISH_DAYS` | 1。HRハッカーの公開中求人のうち、今回の取込のCSVに載らなかった(最終CSV検出日が今回の取込日より前=1日以上前の)ものを `kyuujin_status=公開終了` / `baitai_genjoukyou_hrhakkaa=非公開` にする |
| 実行条件 (HRH) | `hrhacker_import` の actual で CSV が完全 (`--limit` なし・1行以上・媒体求人ID欠けなし) かつ更新/作成が全部成功した回だけ。店舗 (`id_shop_hrhakkaa`) ごとに判定 |
| AirWork | 完全なエクスポートを処理した口座の公開中のうち、そのエクスポートに無い求人だけ公開終了 (従来の動作)。取得していない口座は触らない。取得に成功した口座は `data/job_application_sync/aw_fetch_ledger.json` にハッシュで記録 |
| `MAX_END_RATIO` | 0.20。1 店舗/口座の公開中のうち 20% を超える終了予定になったら、その店舗/口座は見送り (壊れた取得の疑い)。予定が `MAX_END_FLOOR`=2 件以下なら適用しない |
| 再登場 | 次回の CSV/エクスポートに載れば通常の更新で 公開中 / 掲載中 に戻る |
| 止める | 環境変数 `JAS_HRH_END_VANISHED=0` (HRH) / `JAS_AW_END_MISSING=0` (AW) |

一回限りの是正 (既定は dry-run、件数だけ出力):

```bash
python scripts/job_application_sync/end_vanished_listings.py            # dry-run
python scripts/job_application_sync/end_vanished_listings.py --apply    # 書き込み (100件ずつ)
python scripts/job_application_sync/end_vanished_listings.py --only hrh # hrh / aw の片方だけ
```

HRH は今日の取込が済んでいる(今日検出された求人がある)ときだけ対象。AW は「口座を7日以内に取得済み」かつ「その取得に載らなかった」求人だけが対象。取得日は台帳、無ければ口座内の最終CSV検出日の最大値で推定する。取得が古い口座や日付の無い求人は「要確認」として変更しない。書き込んだ求人は `douki_moto_filename` に `end_vanished_listings` と入る。
