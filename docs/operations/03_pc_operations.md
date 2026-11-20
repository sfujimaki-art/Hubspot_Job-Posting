# 専用 PC の運用

対象は AirWork を動かしている専用 PC（Windows）。AirWork（PR #16）は今動いている。HR（PR #23）は切り替えの後に動く（手順は [04_cutover_hr_to_pc.md](04_cutover_hr_to_pc.md)）。
`未確認` と書いた所は、リポにも Mac 側の記録にも無い事実。PC のセッションが確かめて、この文書に書き足す PR を出す。

## 1. フォルダ

| フォルダ | 中身 | ブランチ | 状態 |
|---|---|---|---|
| `C:\jas_dev` | AirWork の応募・急ぎ取得・巡回・有料広告実績 | `feat/aw-crawl-daily`（PR #16、未マージ） | 2026-10-13 から稼働 |
| `C:\jas_hr` | HR の応募の登録・求人の監視 | 切り替えまでは文書を読むための main。切り替えの前日に `feat/hr-on-local-pc`、切り替え後に main | 切り替え前は何も動かない |
| `C:\jas` | PR #16 のマージ後に AirWork を移す本番のフォルダ（#16 の本文の予定） | — | まだ無い（未確認） |

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

`C:\jas_hr` を切り替えの準備で `feat/hr-on-local-pc` にした後は、Mac のセッションがそのブランチに main を取り込んでおく（04 の 0 章）ので、同じ文書がそのブランチでも読める。

## 2. タスク（タスクスケジューラ）

### AirWork（`C:\jas_dev`）

| 処理 | いつ | タスク名・起動のしかた |
|---|---|---|
| 応募の取り込み | 5 分おき | 未確認 |
| 急ぎ取得（`aw_orchestrator --from-urgent-queue`） | 5 分おき | 未確認 |
| 夜の巡回（求人・画像・有料広告実績） | 毎晩 21:00 | 未確認。起動のスクリプト `run_dev_crawl.ps1`（nightly で `JAS_AW_APPLICANT_CHECK=1` を付ける）が PC にある。リポには無い |

PC のセッションは `Get-ScheduledTask -TaskPath "\JAS\"`（無ければ `Get-ScheduledTask | Where-Object TaskPath -like "*JAS*"`）でタスク名・トリガー・実行するコマンドを確かめ、上の表を埋める。起動のスクリプトの中身を写すときは秘密値を書かない。

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

AirWork の `.env` に実際に入っている名前は未確認。PC のセッションが名前だけを確かめて書き足す。

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

ログの場所・正常のしるしは未確認。PC のセッションが確かめて書き足す。Slack の通知には原因・対処・出どころ（`[PC]` / `[Actions]`）が付き、同じ失敗は 1 日 1 回（#16）。

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

- 403 を受けたら 30 分止まって再開する（`fetchers/aw_pace.py`）。
- ログインできない社がある（2026-10-19 時点で 38 社: 規約の再同意待ち 26・ID/PW 違い 10・成功判定が厳しすぎる 2。#16 の本文）。直すのは別の PR。
- それ以外の対処の手順は未確認。

## 6. PC の電源と再起動

- スリープは「なし」（ふたを閉じたとき・電源ボタンも「何もしない」）。スリープ中はタスクが動かない（目覚めたあと 1 回だけ追いつく）。
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

1〜3 は PC と GitHub・GAS にまたがる。PC のセッションは 1 を、Mac のセッションかユーザーが 2・3 を行う。

### AirWork

Actions へは戻せない（GitHub のサーバーからはログインできない）。PC の中で止める・戻す手順は未確認。
