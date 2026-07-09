/**
 * 応募連携/求人デイリー 外部トリガー (WBS 1.11.9)
 *
 * GitHub Actions の cron が「新規アカウント × 最短5分間隔」で不発のため、
 * GAS の time-trigger から GitHub API (workflow_dispatch) を叩いて確実に起動する。
 * PC不要・完全外部。queue と同じ GAS 基盤に相乗り可。
 *
 * ── セットアップ ──────────────────────────────────────────────
 * 1. GitHub PAT を作成 (classic, scope="workflow"。または fine-grained で
 *    sfujimaki-art/Hubspot_Job-Posting に Actions:write)。
 *    ※ makimaki1006(collaborator) or sfujimaki-art いずれかのアカウントで発行。
 * 2. GASエディタ → プロジェクトの設定 → スクリプト プロパティ で
 *    キー: GITHUB_PAT  値: 発行したトークン  を登録。
 * 3. トリガー登録:
 *    - dispatchApplicantSync : 時間主導型 / 分ベース / 5分おき (要件: 応募5分に1回)
 *    - dispatchJobDaily      : 時間主導型 / 日タイマー / 午前7時など
 *    (aw-create→aw-collect の2フェーズは dispatchAwCreate/dispatchAwCollect を
 *     それぞれ時間差トリガーで。まずは HR/応募だけでも可)
 * ────────────────────────────────────────────────────────────
 */

var REPO = 'sfujimaki-art/Hubspot_Job-Posting';

function _dispatch(workflowFile, inputs) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_PAT');
  if (!token) { throw new Error('GITHUB_PAT 未設定 (スクリプトプロパティ)'); }
  var url = 'https://api.github.com/repos/' + REPO +
            '/actions/workflows/' + workflowFile + '/dispatches';
  var res = UrlFetchApp.fetch(url, {
    method: 'post',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ ref: 'main', inputs: inputs }),
    muteHttpExceptions: true
  });
  var code = res.getResponseCode();
  console.log(workflowFile + ' -> ' + code + (code === 204 ? ' OK' : ' ' + res.getContentText()));
  return code === 204;
}

/** 応募連携 (実書込・BOTH)。**5分おきトリガー**(要件: 応募確認は5分に1回)。
 *  runが5分超でも concurrency group で次runはキュー=直列化され多重起動しない。
 *  AWは12社/runで漸次消化(BAN対策のセッション再利用+上限)。 */
function dispatchApplicantSync() {
  _dispatch('applicant_sync.yml', { mode: 'sync', media: 'BOTH', dry_run: 'false' });
}

/** 再紐付けスイープ: 対象外応募を後から出来た求人に紐付け。
 *  求人巡回(aw-collect/HR)の後に実行する。日次 or 求人巡回の30分後トリガー推奨。 */
function dispatchRelink() {
  _dispatch('applicant_sync.yml', { mode: 'relink', dry_run: 'false' });
}

/** LISTING→Deal 関連付け: 新規求人を取引に紐付ける(§21.1要件)。
 *  求人巡回の後、sync_ichijitaiou の前に実行する(1次対応連動はDeal関連を前提とするため)。
 *  ※ 実行順序: 求人巡回 → dealAssoc → sync_ichijitaiou → relink。 */
function dispatchSyncDealAssoc() {
  _dispatch('applicant_sync.yml', { mode: 'deal-assoc', dry_run: 'false' });
}

/** 1次対応連動: Deal.itijitaiou → LISTING.ichijitaiounoumu_deforuto。
 *  求人巡回(HR/AW-collect)+dealAssoc の後、relinkの前に実行する。 */
function dispatchSyncIchijitaiou() {
  _dispatch('applicant_sync.yml', { mode: 'ichijitaiou', dry_run: 'false' });
}

/** 求人デイリー: HR差分。日次(朝)トリガー推奨。 */
function dispatchJobDailyHR() {
  _dispatch('job_daily.yml', { phase: 'hr', dry_run: 'false' });
}

/** 求人デイリー: AW生成トリガー(create)。日次。数分後に collect を打つ。 */
function dispatchAwCreate() {
  _dispatch('job_daily.yml', { phase: 'aw-create', target_login_ids: '', limit: '', dry_run: 'false' });
}

/** 求人デイリー: AW回収(collect)。create の20分後トリガー推奨。 */
function dispatchAwCollect() {
  _dispatch('job_daily.yml', { phase: 'aw-collect', target_login_ids: '', limit: '', dry_run: 'false' });
}

/** 接続テスト: 手動実行して 204 が返れば PAT/権限OK。 */
function testDispatch() {
  var ok = _dispatch('applicant_sync.yml', { media: 'HR', dry_run: 'true' });
  console.log(ok ? '✅ 接続OK (dry-run dispatch成功)' : '❌ 失敗 (上のログ参照)');
}
