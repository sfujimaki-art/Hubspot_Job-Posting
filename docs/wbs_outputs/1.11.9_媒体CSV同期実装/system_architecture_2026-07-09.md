# システムアーキテクチャ — 求人/応募 媒体CSV同期 (WBS 1.11.9)

2026-07-09 時点。GitHub は下記 Mermaid を自動レンダリングする(オンライン閲覧可)。
mermaid.live に貼り付けても見られる。

## 全体フロー

```mermaid
flowchart TB
    %% ===== データソース =====
    subgraph SRC["データソース"]
        HRC["HRハッカー<br/>求人CSV(全ステータス)"]
        AWC["AirWork<br/>求人XLSX + 採用ページ(slug)"]
        Q["応募集計シート<br/>(Queue)"]
        ACC["アカウント情報シート<br/>企業ID/PW・管理用メール"]
    end

    %% ===== GAS 外部トリガー =====
    subgraph GAS["GAS 外部トリガー (GitHub cron不発の代替)"]
        G1["dispatchApplicantSync<br/>5分毎"]
        G2["dispatchJobDailyHR / AwCreate / AwCollect<br/>日次(朝)"]
        G3["dispatchSyncDealAssoc / SyncIchijitaiou / Relink<br/>日次(朝)"]
    end

    %% ===== GitHub Actions =====
    subgraph ACT["GitHub Actions (会社publicリポ・無制限無料)"]
        JD["Job Daily<br/>hr / aw-create / aw-collect"]
        AS["Applicant Sync<br/>sync / relink / deal-assoc / ichijitaiou / reconcile"]
    end

    %% ===== HubSpot =====
    subgraph HS["HubSpot"]
        DEAL["(取引 Deal<br/>itijitaiou・kanri_mail・hrhacker_shop_ids)"]
        LST["(求人 LISTING 0-420<br/>ステータス/詳細/URL/1次対応_deforuto)"]
        APP["(応募 APPOINTMENT 0-421<br/>oubosaki_*11 + ichijitaiounoumu)"]
    end

    %% トリガー→Actions
    G1 --> AS
    G2 --> JD
    G3 --> AS

    %% 求人巡回
    HRC -->|"lag+retry / status自動変更<br/>店舗id・公開日"| JD
    AWC -->|"incremental 20社/run<br/>Sheetsキャッシュ・slug URL・詳細"| JD
    JD -->|"create/update"| LST

    %% Deal連携
    ACC -.->|"管理メール索引"| AS
    AS -->|"deal-assoc: HR=店舗id / AW=管理メール"| LST
    DEAL -->|"deal-assoc(関連付け)"| LST
    DEAL -->|"ichijitaiou: itijitaiou→必要/不要"| LST

    %% 応募連携
    Q -->|"応募検知(5分)"| AS
    AS -->|"求人特定→紐付け(typeId=5)<br/>+ 求人情報コピー11 + 1次対応(Deal直読み)"| APP
    APP -->|"応募先求人"| LST
    AS -->|"relink: 対象外→求人出現後に紐付け+コピー"| APP

    %% 集計
    LST -->|"契約求人数 = HR×公開中"| KPI["KPI / 継続率改善"]
    APP -->|"媒体別/求人別/顧客別応募数"| KPI
```

## 実行順序 (日次バッチ, 9時定時前に完了)

```mermaid
flowchart LR
    A["5時<br/>求人巡回<br/>HR + AW-create"] --> B["6時<br/>AW-collect<br/>(生成完了後)"]
    B --> C["7時<br/>deal-assoc<br/>LISTING→取引"]
    C --> D["8時前半<br/>ichijitaiou<br/>取引→求人 1次対応"]
    D --> E["8時後半<br/>relink<br/>対象外回収"]
    F["5分毎<br/>応募sync<br/>(順序非依存)"] -.-> F
```

## 主要な設計判断

| 論点 | 採用した設計 |
|---|---|
| スケジューラ | GitHub cron不発(新規アカ×短間隔)→ **GAS外部トリガー** |
| 順序依存 | **順序非依存**(relink回収 + 1次対応はDeal直読み) |
| AW全社一斉 | **廃止 → incremental 20社/runローテーション + 応募起点オンデマンド** |
| Sheets 429 | account_loader **プロセス内キャッシュ**(1回読み) |
| 求人ステータス | CSV全ステータス取込 + **自動変更**(非公開→公開終了) |
| 求人↔取引 | HR=店舗id→hrhacker_shop_ids / AW=管理メール→kanri_mail_address |
| 応募↔求人 | 媒体求人ID単位・タイトル名寄せ禁止・関連typeId=5 |
| BAN対策 | セッション再利用・12社/run上限・incremental |
```
