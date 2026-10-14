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
