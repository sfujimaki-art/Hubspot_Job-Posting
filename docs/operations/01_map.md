# 全体の地図（何が・どこで・いつ）

2026-11-20 時点。処理の中身は [02_mechanics.md](02_mechanics.md)、専用 PC の詳細は [03_pc_operations.md](03_pc_operations.md)。

## 動く場所

| 場所 | 中身 |
|---|---|
| GitHub Actions | このリポのワークフロー（`.github/workflows/`）。HRハッカーにはログインできる。AirWork は 2026-10-10 から GitHub のサーバーを遮断しているのでログインできない |
| GAS プロジェクト applicant-dispatch | 時間主導のトリガーで GitHub のワークフローを起動する（`workflow_dispatch`）。写しは `scripts/job_application_sync/gas_dispatch_trigger.gs`。GitHub の cron が短い間隔で着かないための代わり |
| 専用 PC（Windows） | タスクスケジューラで動く。AirWork は `C:\jas_dev`（PR #16 のブランチ）。HR は PR #23 の切り替え後に `C:\jas_hr`（main） |

## 今の状態（2026-11-20、PR #24 マージ後・PR #23 の切り替え前）

| 処理 | 場所 | いつ | 起動 |
|---|---|---|---|
| HR の応募の登録 | Actions `applicant_sync.yml`（mode=sync、media=BOTH） | 5 分おき | GAS `dispatchApplicantSync` |
| HR の求人の監視 | Actions `job_daily.yml`（phase=hr） | JST 1:00・12:00・20:00 | cron 3 本。ほかに GAS `dispatchJobDailyHR` |
| 取引まわり（deal_hygiene） | Actions `job_daily.yml` の `deal_hygiene` | JST 1:40 | cron |
| 求人→取引の紐付け | Actions `applicant_sync.yml`（mode=deal-assoc） | GAS のトリガーの時刻 | GAS `dispatchSyncDealAssoc` |
| 一次対応の転記 | Actions `applicant_sync.yml`（mode=ichijitaiou） | GAS のトリガーの時刻 | GAS `dispatchSyncIchijitaiou` |
| 対象外の応募の紐付け直し | Actions `applicant_sync.yml`（mode=relink） | GAS のトリガーの時刻（ほかに応募の回の最後に毎回 500 件） | GAS `dispatchRelink` |
| 報告シート URL の写し（取引→求人） | Actions `sheet_url_copy.yml` | JST 2:10 | cron |
| AirWork の応募の取り込み | 専用 PC `C:\jas_dev` | 5 分おき | タスクスケジューラ |
| AirWork の急ぎ取得（応募の求人が無い社） | 専用 PC `C:\jas_dev` | 5 分おき | タスクスケジューラ |
| AirWork の求人の巡回・画像・有料広告実績 | 専用 PC `C:\jas_dev` | 毎晩 21:00 | タスクスケジューラ |
| リクロジの応募 | HR の応募の回の後段 | 5 分おき | 応募の回に含まれる |
| 顧客シートへの写し | HR の応募の回の後段 | 5 分おき | 応募の回に含まれる |

- GAS のトリガーは 5 本: `dispatchSyncIchijitaiou`・`dispatchJobDailyHR`・`dispatchRelink`・`dispatchSyncDealAssoc`・`dispatchApplicantSync`。`dispatchAwCreate`・`dispatchAwCollect` は 2026-11-20 に削除した。`dispatchApplicantSync` 以外の時刻は GAS の画面で確かめる。
- Actions の AirWork の段（`aw-*`）は Variables の `AW_ON_ACTIONS=1` のときだけ動く（PR #24）。今は入れていないので動かない。
- 手で起動するだけのワークフロー: `probe_sheet_access.yml`（顧客シートを読めるかの確認）、`sso_cloudip_test.yml`（ログインの確認）。手動実行はしない決まり。
- 専用 PC の AirWork のタスク名・起動のスクリプトはリポに無い。PC のセッションが確かめて 03 に書き足す。

## PR #23 の切り替え後

切り替えの手順は [04_cutover_hr_to_pc.md](04_cutover_hr_to_pc.md)。日時は未決（[06_open_issues.md](06_open_issues.md)）。

| 処理 | 場所 | いつ | 起動 |
|---|---|---|---|
| HR の応募の登録（後段のリクロジ・顧客シート・紐付け直しを含む） | 専用 PC `C:\jas_hr` | 5 分おき | タスク `\JAS\JAS_HR_applicants` |
| HR の求人の監視 | 専用 PC `C:\jas_hr` | 1:01・12:01・20:01 | タスク `\JAS\JAS_HR_listings` |
| PC の見張り | Actions `pc_heartbeat_watch.yml` | 30 分おき | cron |
| 取引まわり（deal_hygiene） | Actions | JST 1:40 | cron（変わらない） |
| 報告シート URL の写し | Actions | JST 2:10 | cron（変わらない） |
| deal-assoc・ichijitaiou・relink | Actions | GAS のトリガーの時刻 | GAS（変わらない） |
| AirWork | 専用 PC `C:\jas_dev` | 変わらない | 変わらない |

- GAS の `dispatchApplicantSync`・`dispatchJobDailyHR` は消す。残るのは 3 本。
- Actions の HR の応募（mode=sync）と求人（phase=hr）は、Variables の `HR_ON_ACTIONS=1` が無ければスキップになる。
- Actions は HR の予備として残さない（同時に動くと同じ応募を二重に作りうる）。

## 状態の確かめ方（読み取りだけ）

```bash
gh run list --workflow applicant_sync.yml -L 300 --json conclusion,createdAt,startedAt,updatedAt,databaseId
gh run list --workflow job_daily.yml -L 60 --json conclusion,createdAt,event,databaseId
gh run view <id> --json jobs -q '.jobs[].steps[]|"\(.name)\t\(.startedAt)\t\(.completedAt)"'
# 要約の行だけを見る（個人情報の行を出さない）
gh run view <id> --log | awk -F'\t' '{print $3}' | sed 's/^[0-9T:.-]*Z //' \
  | grep -E "^\[(applicant_sync-done|relink)\]|台帳による除外|HR詳細ページ: 対象|run_all|^PHASE=|\[hr_watcher\] 差分|create-done"
```

AirWork の `aw-*` は Actions で動いても全社ログイン失敗のまま「成功」と表示されていた。Actions の成否表示を AirWork の根拠にしない。PC の状態は 03 の方法で見る。
