# 専用 PC の運用

対象は AirWork を動かしている専用 PC（Windows）。AirWork（PR #16）は今動いている。HR（PR #23）は切り替えの後に動く（手順は [04_cutover_hr_to_pc.md](04_cutover_hr_to_pc.md)）。
`未確認` と書いた所は、リポに書かれていない事実。PC のセッションが確かめて、この文書に書き足す PR を出す。
AirWork の欄は 2026-11-20 に PC で確かめた（タスクの設定・起動のスクリプト・ログ・`.env` の名前）。

## 1. フォルダ

| フォルダ | 中身 | ブランチ | 状態 |
|---|---|---|---|
| `C:\jas_dev\Hubspot_Job-Posting` | AirWork の応募・急ぎ取得・巡回・有料広告実績のコード | `feat/aw-crawl-daily`（PR #16、未マージ）のコミットを detached で置いている | 2026-10-13 から稼働 |
| `C:\jas_dev\alerts` | AirWork のコードを直す作業用（`C:\jas_dev\Hubspot_Job-Posting` の worktree） | `feat/aw-crawl-daily` | 作業用。タスクはここを読まない |
| `C:\jas_dev\kit` | AirWork のタスクの起動のスクリプト（`run_dev_applicants.ps1`・`run_dev_crawl.ps1`）。リポには無い | — | 稼働 |
| `C:\jas_hr` | HR の応募の登録・求人の監視 | 切り替えまでは文書を読むための main。切り替えの前日に `feat/hr-on-local-pc`、切り替え後に main | 切り替え前は何も動かない |
| `C:\jas` | AirWork のタスクが共用する土台（下の表）。PR #16 のマージ後は AirWork の本番のコードもここに移す予定（#16 の本文） | `C:\jas\Hubspot_Job-Posting` は main | 稼働（ここのコードは動かしていない） |

`C:\jas` の中身（2026-11-20 確認）:

| 場所 | 中身 |
|---|---|
| `C:\jas\venv` | タスクが使う Python（3.12.10、Playwright 1.50） |
| `C:\jas\ms-playwright` | ブラウザ（`PLAYWRIGHT_BROWSERS_PATH`）。回ごとに `C:\jas\kit\browser_check.py` が起動を確かめ、無ければ入れ直す |
| `C:\jas\secrets` | `.env` とシートの鍵のファイル（3 章） |
| `C:\jas\kit` | 共通の関数 `common.ps1`（パス・ログ・鍵・`.env` の読み込み・古いファイルの掃除）。本番用の `register_tasks.ps1`・`run_aw_*.ps1` もあるが、そのタスクは登録されていない |
| `C:\jas\logs` | ログ（4 章） |
| `C:\jas\locks` | 同じ処理が重ならないための鍵のファイル（`aw_applicants.lock`・`aw_urgent.lock`・`aw_billing.lock`・`aw_crawl.lock`・`aw_relink.lock`・`autopilot.lock`） |
| `C:\jas\autopilot` | 見張り役（5 章）。`watchdog.py`・手順書 `runbook.md`・対応の記録 `incidents.md` |
| `C:\jas\Hubspot_Job-Posting\data\job_application_sync` | AirWork の応募の台帳 `applicant_ledger.json` と AirWork のログインの控え `aw_sessions\`。コードは `C:\jas_dev` のものを使い、台帳とログインの控えだけここを使う（起動のスクリプトが `JAS_LEDGER_PATH`・`JAS_SESSION_DIR` で渡す） |

急ぎ取得の待ち行列 `aw_urgent_jobs.json`・有料広告実績の待ち行列 `aw_billing_queue.json`・夜の応募の突き合わせの控えは `C:\jas_dev\Hubspot_Job-Posting\data\job_application_sync\` にある。

AirWork のコードの入れ替え: 作業用の `C:\jas_dev\alerts` でコミット・push し、AirWork の Python が 1 つも動いていない時に `git -C C:\jas_dev\Hubspot_Job-Posting fetch C:\jas_dev\alerts feat/aw-crawl-daily` → `git -C C:\jas_dev\Hubspot_Job-Posting checkout <コミット>`。

- AirWork と HR はフォルダもコードも分ける。#16 は応募の担当者を「取引の担当者」で決める古い版で、main は PR #22 で「取引のコンサル担当」に変えた。同じフォルダにすると HR の応募の担当者が変わる。
- #16 が main に入ったら 2 つのフォルダを 1 つにまとめられる（そのとき AW と HR を 1 本のタスクにするかは未決。06）。
- 各フォルダの `data/job_application_sync/` に台帳・控え、`scratchpad/` に CSV・ログが溜まる。`data/` は `.gitignore` に入っていないので、コミットしない。

### 文書の読み方（PC のセッション）

`C:\jas_dev` は #16 のブランチなので、そこで `git pull` しても main の文書は入らない。稼働中のフォルダでブランチを切り替えない。

```powershell
# C:\jas_hr が無ければ main で作る（タスクを登録しない限り何も動かない）
git clone https://github.com/sfujimaki-art/Hubspot_Job-Posting.git C:\jas_hr
# 以後
cd C:\jas_hr
git pull
# CLAUDE.md と docs\operations\ を読む
```

`C:\jas_hr` を切り替えの準備で `feat/hr-on-local-pc` にした後は、PC のセッションがそのブランチに main を取り込んでおく（04 の 0 章）ので、同じ文書がそのブランチでも読める。

## 2. タスク（タスクスケジューラ）

### AirWork（`C:\jas_dev`）

2026-11-20 確認。タスクはタスクスケジューラの直下（`\`）にあり、`\JAS\` フォルダではない。どれも `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File <スクリプト>` で起動し、前の回が残っていれば次は始めない（`IgnoreNew`）。

| タスク | いつ | 起動のスクリプト | 中身 | タスクの上限 |
|---|---|---|---|---|
| `JAS_AW_Applicants_dev` | 5 分おき | `C:\jas_dev\kit\run_dev_applicants.ps1` | `applicant_sync.py --actual --source sheet1 --media AW`。古いログ・CSV の掃除もここで行う | 2 時間 |
| `JAS_AW_Urgent_dev` | 5 分おき | `C:\jas_dev\kit\run_dev_crawl.ps1 -Phase urgent` | 急ぎ取得の待ち行列に社があるときだけ `aw_orchestrator.py --phase full --from-urgent-queue --actual --parallel 1 --limit 5` → 応募の紐付け直し（`applicant_sync.py --actual --relink`）。無ければ Python を起動しない | 2 時間 |
| `JAS_AW_Billing_dev` | 5 分おき | `C:\jas_dev\kit\run_dev_crawl.ps1 -Phase billing` | 作成から 10 分たった有料広告実績があるときだけ `aw_orchestrator.py --phase billing-collect` → `airwork_billing_import.py --apply` | 3 時間 |
| `JAS_AW_Crawl_Nightly_dev` | 毎晩 21:00 | `C:\jas_dev\kit\run_dev_crawl.ps1 -Phase nightly` | `JAS_AW_APPLICANT_CHECK=1` を付けて `aw_orchestrator.py --phase full --from-active-deals --from-applications --actual --applications-cutoff <60 日前> --parallel 1 --skip-ok-since <開始時刻>` を最大 3 回（取れなかった社だけ回し直す。403 の停止中は最大 2 時間待つ）→ `--phase billing-create` → `airwork_billing_import.py --apply` → `aw_applicant_check.py --report` → 紐付け直し。ふだん 10〜12 時間かかり、翌朝 9 時ごろ終わる | 23 時間 |
| `JAS_AW_Watchdog` | 15 分おき | `C:\jas\autopilot\run_watchdog.ps1` | 見張り役（5 章） | 2 時間 |
| `JAS_AW_Crawl_Collect_dev`・`JAS_AW_Crawl_CreateApps_dev` | — | `run_dev_crawl.ps1 -Phase collect` / `create-apps` | 2026-10-16 までの旧方式。無効のまま残している（戻すとき用）。有効にしない | — |

- 実行するのは PC にログオンしている Windows のユーザーで、ログオンの種類は「ユーザーがログオンしているときのみ実行する」（`Interactive`）。ログオンしていない間は AirWork のタスクは動かない（6 章）。HR のタスク（下）は S4U で登録するので、ここが違う。
- 起動のスクリプトは `C:\jas\kit\common.ps1` を読み、`C:\jas\secrets\.env` を読み込み、ブラウザを確かめてから、`C:\jas_dev\Hubspot_Job-Posting` を作業フォルダにして `C:\jas\venv` の Python を動かす。コードの `git pull` はしない。
- スクリプトが足す環境変数（`.env` には無い）: `GOOGLE_SA_JSON`（無ければ `C:\jas\secrets\google_sa.json`）・`GOOGLE_APPLICATION_CREDENTIALS`・`SHEETS_AUTH_MODE`（無ければ `sa`）・`CUSTOMER_SHEET_START`・`PYTHONIOENCODING`・`PYTHONUTF8`・`GITHUB_WORKFLOW`（Slack の出どころの表示）・`JAS_LEDGER_PATH`・`JAS_SESSION_DIR`・`JAS_AW_URGENT_QUEUE`・nightly だけ `JAS_AW_APPLICANT_CHECK`。
- AirWork が 403 を返すと `%TEMP%\jas_aw_blocked_until.txt` に止める期限が書かれ、その間はどのスクリプトも Python を起動しない。

状態を見るコマンド:

```powershell
Get-ScheduledTask -TaskName "JAS_AW_*" | ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; "{0} {1} 最後={2} 結果={3}" -f $_.TaskName, $_.State, $i.LastRunTime, $i.LastTaskResult }
Get-Process python -ErrorAction SilentlyContinue   # 動いている Python（コードの入れ替えはここが空の時）
```

### HR（`C:\jas_hr`、#23 の後）

`scripts/job_application_sync/pc/register_hr_tasks.ps1` が登録する（既定は無効で登録、`-Enable` で有効）。

| タスク | いつ | 中身 | 上限 |
|---|---|---|---|
| `\JAS\JAS_HR_applicants` | 5 分おき | `pc_runner.py applicants` = `applicant_sync.py --actual --source sheet1 --media HR`（Actions と同じ）。後段のリクロジ・顧客シート・紐付け直しを含む | 子の処理 25 分、タスク 35 分 |
| `\JAS\JAS_HR_listings` | 1:01・12:01・20:01 | `pc_runner.py listings` = `hr_watcher.py --actual --skip-aw --slack` → CSV 欠けなら止める → `backfill_deal_shop_id_via_email.py` → `resolve_listing_owner.py` | 子の処理 100 分、タスク 150 分 |

- 前の回が残っていれば次は始めない（タスクの設定 `IgnoreNew` と `pc_runner.py` のファイルの鍵 `data/job_application_sync/pc_hr_*.lock` の二重）。
- 求人の回は、応募の回が動いている間は始めるのを待つ（最大 15 分）。応募の回は求人の回を待たない。
- HR は AirWork の順番の鍵（`fetchers/aw_pace.py`）に入らない。
- 登録はログオンしていなくても動く形（S4U、パスワードを保存しない）。AirWork のタスクと同じ Windows のユーザーで登録する。
- ブラウザ（Playwright の chromium）は PC に 1 回だけ入れる。

### 状態を見るコマンド

```powershell
Get-ScheduledTask -TaskPath "\JAS\" | Format-Table TaskName,State
Get-ScheduledTaskInfo -TaskPath "\JAS\" -TaskName JAS_HR_applicants   # 最後に動いた時刻と結果（0 が成功）
```

## 3. env（名前だけ。値は書かない）

各フォルダの `.env`（`.gitignore` 済み）に置く。GitHub の Secrets は読み出せないので、値は手元の控え・元の鍵ファイルから入れる。

| 名前 | 中身 | AW | HR |
|---|---|---|---|
| `HUBSPOT_ACCESS_TOKEN` | HubSpot の鍵 | ○ | ○ |
| `HRHACKER_USER` / `HRHACKER_PASS` | HRハッカーの管理アカウント | — | ○ |
| `JAS_APPLICANT_QUEUE_SHEET_ID` | 集約シート（シート1・リクロジ応募・PC稼働のタブ） | ○ | ○ |
| `JAS_SHEET_ID` | アカウントのシート（AirWork の企業ID など） | ○ | ○ |
| `SLACK_APPLICANT_ALERT_WEBHOOK` | Slack の知らせ先 | ○ | ○ |
| `SHEETS_AUTH_MODE` と `GOOGLE_SA_JSON` または `GOOGLE_OAUTH_TOKEN` | シートを読み書きする鍵 | ○ | ○（AW と同じでよい） |
| `CUSTOMER_SHEET_SA_JSON` | 顧客シートに書く専用の鍵ファイルのパス | — | ○（用意のしかたは未決。06） |
| `CUSTOMER_SHEET_ALLOW` | 顧客シートに写してよいシートの一覧 | — | ○（同上） |
| `CUSTOMER_SHEET_START` | `2026-08-04T00:00:00Z`（Actions と同じ） | — | ○ |
| `JAS_HR_SINCE` | 切り替えの日（YYYY-MM-DD）。この日より前の通知は読まない。14 日たてば効かなくなる | — | ○（切り替えで足す） |
| `JAS_LEDGER_PATH` / `JAS_SESSION_DIR` / `JAS_LEDGER_MERGE_FROM` | 台帳・セッションの置き場所 | ○（あれば） | ×（HR の `.env` には入れない。AW と同じ台帳を使ってしまう） |
| `JAS_AW_*` | AirWork の間隔・並列・画像・急ぎ取得などの調整（`fetchers/aw_pace.py`・`aw_orchestrator.py` が読む） | ○（あれば） | — |
| `JAS_HRH_END_VANISHED` / `JAS_AW_END_MISSING` | `0` で消えた求人の終了を止める | 任意 | 任意 |

AirWork の `.env` は `C:\jas\secrets\.env`（`C:\jas_dev` には `.env` が無い）。2026-11-20 に名前だけ確かめた。入っているのは次の 7 つ。

`HUBSPOT_ACCESS_TOKEN`・`JAS_APPLICANT_QUEUE_SHEET_ID`・`JAS_SHEET_ID`・`SLACK_APPLICANT_ALERT_WEBHOOK`・`SHEETS_AUTH_MODE`・`GOOGLE_SA_JSON`・`CUSTOMER_SHEET_ALLOW`

- シートの鍵のファイルは `C:\jas\secrets\google_sa.json`。
- `JAS_LEDGER_PATH`・`JAS_SESSION_DIR`・`JAS_AW_URGENT_QUEUE`・`CUSTOMER_SHEET_START` は `.env` ではなく起動のスクリプトが入れる（2 章）。`JAS_LEDGER_MERGE_FROM` と `JAS_AW_*` の調整の名前は入っていない（コードの既定値で動いている）。

## 4. ログの場所と見方

### HR（#23 の後）

- `C:\jas_hr\scratchpad\pc_logs\applicants-<日付>.log`・`listings-<日付>.log`（14 日で消える）。
- 応募者の CSV（個人情報入り）は 3 日、求人 CSV・店舗一覧・求人の控えは 14 日で消える（どれも最新の 1 つは残す）。ログ・CSV の中身を PR やチャットに貼らない。

| 処理 | 正常のしるし | 気にする行 |
|---|---|---|
| 応募 | `シート1直読み (cutoff=…, dry_run=False)`、通知があれば `✅ HR: 応募取込 linked=… dup=… 範囲=…`、`[applicant_sync-done]` の中が `'failed': 0`、最後に `結果: HR応募 成功`（新しい通知が無い回は `成功 (新しい通知なし)`） | `❌ HR:`、`⏳ HR:`、時間切れ、`結果: HR応募 失敗…`、`飛ばした` が何回も続く |
| 応募の後段 | `[customer_sheet_sync] run_all: 12シート 成功=12`、`[relink]`、`[reclogi]` | `[customer_sheet_sync] 許可リスト空 -> 転記スキップ`（`CUSTOMER_SHEET_ALLOW` が無い）、顧客シート転記の全体失敗（鍵が無い・読めない） |
| 求人 | `[hr_watcher] 差分検出` のあと `[hr_watcher-done]` に `"skip_reason": "skip_aw_flag"`（取り込み済み）、または `[hr_watcher] 差分0件` と `"skip_reason": "no_diff"`。最後に `結果: HR求人 成功 (取り込み済み)` か `成功 (差分なし)` | `CSV欠け検知`（結果は `止めた (CSV欠け)`）、`A-1 失敗 / 前回の控えを残す`（結果は `失敗 (取り込み。控えを残した)`）、`⚠️ … が失敗 (本線は継続` |
| どちらも | `[pc_runner …] 終了: rc=0` | `rc` が 0 以外、結果が `失敗`・`終了 (結果が読めない)`、`❌`・`Traceback` |

ログで `dup=` が急に増えていたら二重登録の疑い。ふだんは数十。

### AirWork

2026-11-20 確認。日付は PC の日付。`C:\jas\logs` の 30 日より古いファイルは応募の回が消す。

| ログ | 中身 |
|---|---|
| `C:\jas\logs\applicants\dev_<日付>.log` | 応募の取り込み（5 分おきの回が追記） |
| `C:\jas\logs\crawl\dev_<日付>.log` | 夜の巡回（21:00 に始まった日の名前のまま、翌朝まで追記） |
| `C:\jas\logs\crawl\dev_urgent_<日付>.log`・`dev_billing_<日付>.log` | 急ぎ取得・有料広告実績の受け取り |
| `C:\jas\logs\autopilot\<日付>.log` | 見張り役 |
| `C:\jas_dev\Hubspot_Job-Posting\scripts\job_application_sync\logs\*.json` | 求人の取り込み・有料広告実績の回ごとの詳しい結果 |

ログの中の社名は先頭だけ（`株式***`）、ログインの ID は先頭 2 文字（`ab…`）に伏せてある。中身を PR やチャットに貼らない。

| 処理 | 正常のしるし | 気にする行 |
|---|---|---|
| どれも | `---- 終了 rc=0 所要…秒 ----` | `rc` が 0 以外、`ERROR`、`Traceback` |
| 応募 | `[applicant_sync] シート1直読み (cutoff=…, dry_run=False)`、社ごとに `✅ 株式***: 応募N件 linked=… unlinked=0 … dup=…`、最後に `[applicant_sync-done] {…}` | `❌ 株式***:`（ログインの失敗は `ID か PW が違います` / `パスワードの再設定を求める画面です` などと画面の文字が付く）、`unlinked` が 0 でない、`⏸ … 求人の取得を待ちます` が同じ社で何時間も続く、`AirWork の 403 で HH:MM まで停止中` |
| 急ぎ取得 | `[orchestrate-done] ok=… ng=0`、`[orchestrate-summary]` に `✅ OK` | `ng` が 0 でない、同じ社が何回も積まれ直す |
| 夜の巡回 | `[done N/M] OK …`、`応募の突き合わせ (株式***): {'checked': …, 'missing': 0, …}`、`[applicant_check] 未通知 0件`、翌朝の `---- 終了 rc=0` | `[done …] NG` が続く、`missing` が 0 でない（集約シートのタブ「報告_応募取り込み」と Slack に出る）、昼を過ぎても `---- 終了` が無い |

Slack の通知には原因・対処・出どころ（`[PC]` / `[Actions]`）が付き、同じ失敗は 1 日 1 回（#16）。

## 5. 止まったときの知らせと対処

### HR（#23 の後）

PC が回ごとに集約シートのタブ「PC稼働」へ結果を書き、Actions の `pc_heartbeat_watch.yml` が 30 分おきに B 列を読む。

| 列 | 中身 | 書く人 |
|---|---|---|
| A | 処理（`HR応募` / `HR求人`） | 手で作る |
| B | 最後に成功した時刻 | PC（成功の回だけ） |
| C | 最後の結果（成功・飛ばした・失敗・終了） | PC |
| D | 知らせた時刻 | 見張り |
| E | 最後に動いた時刻 | PC |

- B が HR応募 30 分 / HR求人 13 時間より古ければ Slack。止まったままなら 6 時間おき、戻ったら 1 回。タブ・行が無ければ知らせない。
- 気づくのは止まってから 30 分〜1 時間ほど後（GitHub の cron が遅れた日はもっと後）。見張りは知らせるだけで、代わりに取り込まない。

| しるし | まず見ること |
|---|---|
| 「専用PCの HR応募 が 30分以上成功していません」 | 通知の「最後に動いた」が古ければ PC が止まっている（電源・ネット・Windows Update の再起動・タスクの状態）。新しければ動いていて失敗が続いている（C 列とログの `❌ HR:` など） |
| 「専用PCの HR求人 が 13時間以上成功していません」 | 同上。求人はログの時間切れや HRハッカーのログイン失敗 |
| 「… が戻りました」 | 何もしなくてよい。止まっていた間の応募は、戻った最初の回で直近 14 日の通知から取り込まれる |

14 日より前の通知を取り込み直すとき（Actions の「開始日を指定した回収」の代わり。HubSpot に書くので、ユーザーが求めたときだけ）:

```powershell
cd C:\jas_hr
.venv\Scripts\python scripts\job_application_sync\pc_runner.py applicants -- --hr-cutoff YYYY-MM-DD
```

### AirWork

- 403 を受けたら 30 分止まって再開する（`fetchers/aw_pace.py`、`%TEMP%\jas_aw_blocked_until.txt`）。
- ログインできない社がある（2026-10-19 時点で 38 社: 規約の再同意待ち 26・ID/PW 違い 10・成功判定が厳しすぎる 2。#16 の本文）。直すのは別の PR。2026-11-16 からは、失敗した画面の文字をログ・Slack に出し、AirWork がパスワードを初期化した社（「パスワードの再設定を求める画面」）を分けて出す。直すのは顧客シートのパスワード（人の作業）。
- 見張り役 `JAS_AW_Watchdog`（15 分おき、`C:\jas\autopilot\watchdog.py`）が次を見つけると、Slack に知らせて Claude Code を起動し、手順書 `C:\jas\autopilot\runbook.md` に沿って直させる（同じ異常は 3 時間に 1 回、1 日 8 回まで。対応の記録は `C:\jas\autopilot\incidents.md` と `C:\jas\autopilot\runs\`）。
  - 応募のログが 25 分更新されない
  - 直近 30 分に 3 社以上が同じエラーで失敗し、成功が 0 社（ID/PW 違いなど社ごとの失敗は数えない）
  - 応募・急ぎ取得・有料広告実績のどれかが 3 回続けて `rc` が 0 以外、または 30 分に `Traceback` が 2 回以上
  - 夜の巡回で 30 社続けて NG、または前の晩の巡回が昼を過ぎても終わらない
  - 有料広告実績で同じ社の失敗が繰り返す・溜まりすぎる
  - AirWork のタスクのどれかが無効（止めるときは見張り役も止める。7 章）

## 6. PC の電源と再起動

- スリープは「なし」（ふたを閉じたとき・電源ボタンも「何もしない」）。スリープ中はタスクが動かない（目覚めたあと 1 回だけ追いつく）。
- AirWork のタスクは「ユーザーがログオンしているときのみ実行する」なので、再起動のあとは Windows にログオンするまで動かない。自動ログオンは設定されていない（2026-11-20 確認）。再起動したら PC にログオンする。HR のタスク（S4U）はログオンしなくても動く。
- Windows Update のアクティブ時間を、HR の求人の回（1:01・12:01・20:01）と AirWork の夜の巡回（21:00）に重ならないようにする。再起動の間の応募の回は飛ぶが、次の回で取り込む。

## 7. 戻し方

### HR を Actions へ戻す

PR #23 の Revert はしない（Actions に `JAS_HR_SINCE` を渡す行まで消えるため）。

1. PC のタスクを止める:
   ```powershell
   Disable-ScheduledTask -TaskPath "\JAS\" -TaskName JAS_HR_applicants
   Disable-ScheduledTask -TaskPath "\JAS\" -TaskName JAS_HR_listings
   ```
   タスクスケジューラで 2 つとも「実行中」でなくなるのを待つ。
2. GitHub のリポの Settings → Secrets and variables → Actions → Variables で、先に `JAS_HR_SINCE` = 戻す日（YYYY-MM-DD）、次に `HR_ON_ACTIONS` = `1` を作る。`JAS_HR_SINCE` を入れずに戻すと、Actions の台帳は切り替えの時点で止まっているので、切り替えの日〜戻す日の通知（最大 14 日分）を新規として取り直し、全員の重複を確かめる。重複の確認が失敗すると同じ応募を作る。
3. GAS プロジェクト applicant-dispatch のトリガーを足し直す: `dispatchApplicantSync`（時間主導型・分ベース・5 分おき）、`dispatchJobDailyHR`（日付ベース・午前 1〜2 時、午後 0〜1 時、午後 8〜9 時の 3 本。Actions の cron は外したので GAS から起動する）。
4. 戻した最初の Applicant Sync の回のログで `シート1直読み (cutoff=戻す日 …)` を確かめる。
5. 14 日たったら Variables の `JAS_HR_SINCE` は消してよい。PC へまた移すときは `HR_ON_ACTIONS` を消してから 04 の順に進める。

1〜3 は PC と GitHub・GAS にまたがる。1〜3 とも PC のセッションが行う（GAS の画面の操作はユーザーに頼んでもよい）。

### AirWork

Actions へは戻せない（GitHub のサーバーからはログインできない）。PC の中で止める・再開する手順（2026-11-13〜16 に実際に止めて再開したときの手順）:

止める:

```powershell
# 見張り役を先に止める（止めないと「タスクが無効」を異常として Claude を起動する）
Disable-ScheduledTask -TaskName JAS_AW_Watchdog
Disable-ScheduledTask -TaskName JAS_AW_Applicants_dev
Disable-ScheduledTask -TaskName JAS_AW_Urgent_dev
Disable-ScheduledTask -TaskName JAS_AW_Billing_dev
Disable-ScheduledTask -TaskName JAS_AW_Crawl_Nightly_dev
Get-Process python -ErrorAction SilentlyContinue   # 空になるまで待つ（夜の巡回の途中なら、止めるか終わるのを待つかを決める）
```

再開する: 上の 5 つを `Enable-ScheduledTask` で有効に戻す（`JAS_AW_Watchdog` は最後）。`JAS_AW_Crawl_Collect_dev`・`JAS_AW_Crawl_CreateApps_dev` は有効にしない。

- 止めていた間の応募は、シート1の通知が 14 日以内なら、再開した最初の回から取り込まれる（台帳で処理済みでない行を読む）。
- 夜の応募の突き合わせは、前の回が見た日の続きから見る（最大 14 日さかのぼる）。
- 有料広告実績は先週の分だけ作るので、1 週間を超えて止めると、その間の週は作られない（推測。コードの既定の動きから）。
