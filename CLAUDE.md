# 運用手順書 (Claude 向け)

このリポは、HRハッカー・AirWork の求人と応募を HubSpot に取り込むバッチを GitHub Actions で動かしている。
自動修正 (`.github/workflows/auto_fix.yml`) で起動された Claude は、作業の前にこのファイルを読むこと。
人が直すときも同じ決まりに従う。

## 最優先

- 応募の取り込みが止まり続けることが一番の害。応募が HubSpot に入らないと、一次対応の電話と顧客への報告が遅れる。
- その次に避けるのは、データを壊すこと (二重登録・誤った紐付け・取り込んだことにして実は入っていない)。
- 直すときは、前に成功していた状態へ戻すほうを、新しい版に上げるより優先する。

## ワークフロー

| ファイル | 名前 | いつ | 何をする |
|---|---|---|---|
| `applicant_sync.yml` | Applicant Sync | 5分おき (外部の GAS が `workflow_dispatch` で起動。cron は残置) | シート1 (応募通知) を起点に HR の応募を HubSpot に登録し、顧客シートへ転記する。AirWork は処理しない |
| `job_daily.yml` | Job Daily | `job`: JST 1時・12時・20時、`deal_hygiene`: JST 1時40分 | `job` は HR の求人 CSV を HubSpot の求人票に反映 (差分・店舗ID補完・持ち主判定)。`deal_hygiene` は取引まわりの健全性 |
| `hr_reconcile.yml` | HR Reconcile | 毎時35分 | HR の応募一覧 (直近3日) と HubSpot を突き合わせ、通知シートを経由せずに取りこぼしを登録する。登録したら Slack に 1 行、残れば Slack と Issue |
| `auto_fix.yml` | Auto Fix | 上の3つが終わるたび | 失敗を Slack に知らせ、続いたら実行環境の設定を直す |
| `os_canary.yml` | OS Canary | 毎週月曜 | `ubuntu-latest` で Playwright がブラウザを入れられるかを先回りで試す |
| `sheet_url_copy.yml` | Sheet URL Copy | 毎日 JST 2時10分 | 取引の報告シートURLを求人票へ写す |
| `probe_sheet_access.yml` / `sso_cloudip_test.yml` | — | 手動だけ | 確認用。自動修正の対象外 |

## よくある失敗の型と直し方

### 1. runner の OS が変わり、Playwright がブラウザを入れられない

- ログ: `BEWARE: your OS is not officially supported by Playwright` → `Playwright does not support chromium on ubuntuXX.XX-x64` → `Process completed with exit code 1`。段は `Install deps + Playwright`。
- 実例: 2026-10-25 21:11、`ubuntu-latest` が 24.04 → 26.04 に切り替わり、Playwright 1.50.0 が対応していなかった。応募の取り込みが約13時間止まった。
- 直し方: `runs-on` を前に成功していた OS (例 `ubuntu-24.04`) に固定する。Playwright の版は一緒に上げない (上げると媒体の画面操作の挙動が変わる危険がある)。
- 版を上げる判断は人がする。`os_canary.yml` の結果 (最新の Playwright が新しい OS で動くか) を材料にする。

### 2. HR の求人 CSV が欠けていて保護停止した

- ログ: `[hr] CSV欠け: 後続の店舗ID補完・持ち主判定も停止 (正常終了)`、または状態ファイルの `skip_reason == "incomplete_csv"`。
- これは**正常な停止**。前回のスナップショットを残し、次の回で取り直す。直さない。続く場合は HR 側の CSV 生成の問題なので人に回す。

### 3. AirWork のログインに失敗する

- GitHub のサーバーからは媒体側の対策でログインできない (2026-10-16 から)。AirWork の取得は別のローカルPCで動かしている。
- GitHub 側で AirWork を動かすように直さない。AW の定期実行・応募の処理を戻さない。

### 4. 必要なパッケージが無い (`ModuleNotFoundError`)

- `pip install` の行に足りないパッケージを足す。例: `google-api-python-client` (`job_daily.yml` の求人取込で必要、2026-08-06 に発覚)。

### 5. HubSpot・Google・媒体の一時障害 (429・5xx・タイムアウト)

- 5分おきの次の回で通ることが多い。直さない。続く場合は人に回す。

## やってはいけないこと

- 台帳・処理済みの印・キャッシュを消さない (`data/job_application_sync` の台帳、actions/cache の `jas-applicant-state-*` / `jas-job-state-*`、シート1の F 列の印)。消すと同じ応募が二重に登録されるか、全件を新規として扱う。
- `--actual` / `--dry-run` の切り替えを変えない。緑になっても何も取り込まなくなる。
- 失敗した回を手で再実行しない。ワークフローを手で起動しない。直したものが main に入れば、次の自動実行が拾う。
- secret・環境変数・concurrency・cache の保存/復元の段を変えない。
- スクリプト (`scripts/` 以下) は自動修正で変えない。原因がそこにあれば、報告だけして人に回す。

## 公開リポの決まり

- このリポは公開されている。ログ・Slack・Issue・PR・コミットに、氏名・電話・メール・住所・応募者id・顧客名・シートのURL・secret を出さない。
- 出してよいのは 件数・日付・応募経路の番号・店舗ID・ID の hash の先頭数文字まで。
- 応募や求人の CSV を artifact に上げない。ジョブの中だけで使う。
- 報告に失敗のログを引くときは、上の情報が入っていない行だけを1〜3行。
