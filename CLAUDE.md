# Hubspot_Job-Posting の入口（Mac・専用 PC のどちらのセッションも最初に読む）

## このリポの目的

求人媒体（HRハッカー・AirWork・リクロジ）の求人と応募を HubSpot に写す仕組み。

- 求人の監視: 媒体の求人を HubSpot の求人（LISTING `0-420`）に作る・更新する・終了にする。
- 応募の登録: 媒体の応募を HubSpot の応募（APPOINTMENT `0-421`）に作り、求人と結び付ける。
- 取引まわり: 求人を取引（`0-3`）に紐付け、担当者・一次対応・メモを写す。
- 顧客シートへの写し: 応募を顧客ごとの報告シートに追記する。

運用の文書は `docs/operations/` にある。下の表から読む。

## 決まりごと（ユーザー指示）

- ブランチ・PR・マージはしてよい（2026-11-20 ユーザー許可）。
- GitHub Actions の手動実行はしない（`gh run list` / `gh run view` の読み取りはよい）。
- 台帳・処理済みの印は消さない（応募の台帳 `applicant_ledger.json`、シート1の「受入済」、Actions のキャッシュ、HubSpot の `kokyakushiitotenkijoukyou` など）。
- HubSpot・シートへの書き込みは、その作業でユーザーが求めたときだけ。スクリプトは `--dry-run` でも外部に触るものがあるので、走らせる前にコードを読む。
- 秘密値は値を書かない・画面やログに出さない。
- 公開リポ。顧客名・応募者の個人情報・メールアドレス・シートや GAS プロジェクトの ID・URL・トークンを書かない（名前で呼ぶ。例「GAS プロジェクト applicant-dispatch」「集約シート」）。
- コミット前に `git diff --cached` を見て、上のものが入っていないか grep で確かめる（例: `@`・`docs.google.com/spreadsheets`・`script.google.com`・長い数字の ID）。
- `git add` はファイル名を指定する（`-A`・`.` は使わない）。`data/` は `.gitignore` に入っていないので、PC の台帳・控えをコミットしない。
- 報告は日本語。作ったファイルはフルパスで伝える。

## どの文書を読むか

| 知りたいこと | 文書 |
|---|---|
| 何が・どこで・いつ動いているか | [docs/operations/01_map.md](docs/operations/01_map.md) |
| 処理の流れ・HubSpot の項目・台帳の置き場所 | [docs/operations/02_mechanics.md](docs/operations/02_mechanics.md) |
| 専用 PC のフォルダ・タスク・env・ログ・止まったとき | [docs/operations/03_pc_operations.md](docs/operations/03_pc_operations.md) |
| HR を専用 PC へ移す手順 | [docs/operations/04_cutover_hr_to_pc.md](docs/operations/04_cutover_hr_to_pc.md) |
| 決まったこと（日付・原文） | [docs/operations/05_decisions.md](docs/operations/05_decisions.md) |
| 論点（案）と未決事項 | [docs/operations/06_open_issues.md](docs/operations/06_open_issues.md) |

文書と食い違うときは、コードと実際の実行の記録を正とし、文書を直す。決まったことは 05 に、案は 06 に書き、混ぜない。

## Mac と専用 PC の見分け方と役目

2026-11-20 ユーザー決定: 「以降は専用PCで全部管理したいかな、GItでMacは確認できるくらいで。」

| | 専用 PC のセッション | Mac のセッション |
|---|---|---|
| 見分け方 | OS が Windows（パスが `C:\...`）。AirWork が `C:\jas_dev` で動いている | OS が macOS（パスが `/Users/...`） |
| 役目 | すべて。コードの変更・PR・マージ、PC のタスク・ログ・`.env`、切り替えの手順（04）の全部、Actions の記録の読み取り、文書の更新 | Git で確認するだけ（読む・`gh` で記録を見る）。ブランチ・PR・マージ・文書の更新はしない |
| 触らないもの | 稼働中のフォルダ（`C:\jas_dev`、切り替え後の `C:\jas_hr`）でのブランチ切り替え・コード編集。開発は別のフォルダ（例 `C:\jas_work`）で行い、PR → マージ → 稼働中のフォルダで `git pull` の順にする | このリポへの書き込み全部 |

PC で文書を読むとき: `C:\jas_dev` は PR #16 のブランチで動いているので、そこで `git pull` しても main の文書は入らない。main の文書は `C:\jas_hr`（main で clone したもの）で `git pull` して読む。詳しくは 03 の「文書の読み方」。
