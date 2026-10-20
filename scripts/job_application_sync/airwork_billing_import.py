"""AirWork の有料広告実績 (週次) を HubSpot の LISTING (0-420) に書き込む.

入力: fetchers/aw_billing_fetcher.py が保存した ZIP
      <dir>/<週の月曜 YYYY-MM-DD>/billing_<ハッシュ>_<週>.zip
      (中の sponsor_*.csv = 有料分。all_ / organic_ は使わない)
      ハッシュ → ログインID は <dir>/billing_manifest.json で引く。

同一求人の判定: (airwork_account_login_id, id_airwork) の **両方** が一致すること。
  CSV の「参照番号」= id_airwork。別アカウントの同じ id には絶対に書かない。
  求人名では突合しない。HRハッカーの LISTING とは結び付けない (id_airwork だけが対象)。

CSV は 求人 × 期間全体 の 1 行 (日次ではない)。週ごとに (口座, 参照番号, 週) で合算する。

書き込むプロパティ (HubSpot に先に作ってある前提。このスクリプトは作らない):
  airwork_weekly_perf                       JSON (indeed_weekly_perf と同形、source=airwork)
  airwork_cost_last4w, airwork_applications_last4w, airwork_cpa_last4w (応募0なら空),
  airwork_cost_last_week, airwork_last_week_start, airwork_cost_total
「直近4週」= その LISTING の最新の取込週を含む 4 週。

★有料広告が無かった週は 0 を書く (2026-10-19、ユーザー指示)。
  受け取りが済んだ週 (ZIP がある週 + aw_billing_empty.json の「その週は実績なし」) について、
  その口座の公開中の求人で有料分の行が無いものに、費用 0・応募 0 の行を足す。
  空欄 = まだ取れていない、0 = 取れていて有料広告は無かった、と見分けられるようにする。
  取れていた実績の行を 0 で上書きはしない。求人を作る前の週には足さない。

既定は dry-run (書かない)。--apply で書く。出力は件数のみ (名前は出さない)。

  HUBSPOT_ACCESS_TOKEN=... python scripts/job_application_sync/airwork_billing_import.py --limit 3
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

HUBSPOT = "https://api.hubapi.com"
LISTINGS = "0-420"
PERF = "airwork_weekly_perf"
SUMMARY = ["airwork_cost_last4w", "airwork_applications_last4w", "airwork_cpa_last4w",
           "airwork_cost_last_week", "airwork_last_week_start", "airwork_cost_total"]
PROP_REF = "id_airwork"
PROP_LOGIN = "airwork_account_login_id"
READ_PROPERTIES = [PROP_REF, PROP_LOGIN, PERF] + SUMMARY
PROP_STATUS = "kyuujin_status"
OPEN_STATUS = "公開中"
DEFAULT_EMPTY_FILE = str(Path(__file__).resolve().parents[2] / "data" / "job_application_sync"
                         / "aw_billing_empty.json")
DEFAULT_DIR = str(Path.home() / "airwork-exports" / "billing")
MANIFEST_NAME = "billing_manifest.json"
HUBSPOT_INTERVAL = 0.5
BATCH = 100

DATE_RE = re.compile(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")


# ---- CSV parsing -------------------------------------------------------------------------------

def parse_number(text):
    """'' / '-' -> None; '50018.0' -> 50018; '¥1,234' -> 1234; '0.0684' -> 0.0684."""
    value = (text or "").strip().replace("¥", "").replace("￥", "").replace(",", "")
    if value in ("", "-"):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if number == int(number) else number


def add_known(a, b):
    """None は欠損。両方欠損のときだけ None。"""
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def parse_period(text):
    """'2025/05/05 - 2025/05/11' 等 -> (start_iso, end_iso) / 読めなければ None."""
    found = DATE_RE.findall(text or "")
    if len(found) < 2:
        return None
    try:
        a = datetime.date(*map(int, found[0]))
        b = datetime.date(*map(int, found[1]))
    except ValueError:
        return None
    if a > b:
        return None
    return a.isoformat(), b.isoformat()


def read_sponsor_csv(text, fallback_week):
    """sponsor CSV の本文 -> {(ref, period_start): row}. 同じ参照番号・週の行は合算.

    fallback_week: (start_iso, end_iso)。「抽出期間」が読めない行に使う。
    """
    totals = {}
    for row in csv.DictReader(io.StringIO(text)):
        ref = (row.get("参照番号") or "").strip()
        if not ref:
            continue
        period = parse_period(row.get("抽出期間")) or fallback_week
        if period is None:
            continue
        item = totals.setdefault((ref, period[0]), {
            "period_start": period[0], "period_end": period[1],
            "impressions": None, "clicks": None, "cost_yen": None, "applications": None})
        item["impressions"] = add_known(item["impressions"], parse_number(row.get("表示回数")))
        item["clicks"] = add_known(item["clicks"], parse_number(row.get("クリック数")))
        item["cost_yen"] = add_known(item["cost_yen"], parse_number(row.get("利用金額")))
        item["applications"] = add_known(item["applications"], parse_number(row.get("応募数")))
    return totals


def sponsor_text_from_zip(path):
    """ZIP から sponsor_*.csv の本文 (UTF-8 BOM 除去済み) を返す。無ければ None."""
    with zipfile.ZipFile(str(path)) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base.startswith("sponsor_") and base.endswith(".csv"):
                return z.read(name).decode("utf-8-sig")
    return None


def week_from_dirname(name):
    try:
        start = datetime.date.fromisoformat(name)
    except ValueError:
        return None
    return start.isoformat(), (start + datetime.timedelta(days=6)).isoformat()


def load_dir(directory):
    """-> ({(login_id, ref): {period_start: row}}, stats).

    ログインIDは manifest で引く。引けない ZIP は unknown_account に数えて飛ばす。
    利用金額も表示回数も 0 / 欠損の週は残さない (インプレッションが無い = 配信実績なし)。
    """
    root = Path(directory)
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        manifest = {}
    stats = collections.Counter()
    out = {}
    for week_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        week = week_from_dirname(week_dir.name)
        if week is None:
            continue
        for zpath in sorted(week_dir.glob("billing_*.zip")):
            m = re.match(r"^billing_([0-9a-f]{8})_", zpath.name)
            login = manifest.get(m.group(1)) if m else None
            if not login:
                stats["unknown_account"] += 1
                continue
            stats["files"] += 1
            try:
                text = sponsor_text_from_zip(zpath)
            except zipfile.BadZipFile:
                stats["bad_zip"] += 1
                continue
            if text is None:
                stats["no_sponsor_csv"] += 1
                continue
            for (ref, start), item in read_sponsor_csv(text, week).items():
                if not ((item["impressions"] or 0) > 0 or (item["cost_yen"] or 0) > 0):
                    continue
                out.setdefault((login, ref), {})[start] = item
    return out, stats


def collected_weeks(directory):
    """{login_id: {週の月曜 ISO}}: 受け取りが済んだ週 (ログインIDの分かる ZIP がある週)。"""
    root = Path(directory)
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        manifest = {}
    out = {}
    if not root.exists():
        return out
    for week_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        week = week_from_dirname(week_dir.name)
        if week is None:
            continue
        for zpath in week_dir.glob("billing_*.zip"):
            m = re.match(r"^billing_([0-9a-f]{8})_", zpath.name)
            login = manifest.get(m.group(1)) if m else None
            if login:
                out.setdefault(login, set()).add(week[0])
    return out


def load_empty_weeks(path):
    """aw_billing_empty.json -> {login_id: {週の月曜 ISO}} (「その週は実績なし」と分かった週)。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for login, weeks in (data or {}).items():
        for w in weeks or []:
            if week_from_dirname(str(w)):
                out.setdefault(login, set()).add(str(w))
    return out


def zero_row(week_start):
    start, end = week_from_dirname(week_start)
    return {"period_start": start, "period_end": end, "impressions": 0, "clicks": 0,
            "cost_yen": 0, "applications": 0, "no_paid_ads": True}


# ---- merge and summary -------------------------------------------------------------------------

def parse_existing(raw):
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    return data


def merge_rows(existing, new_rows):
    """同じ週は新しい行で置き換え。週の昇順で返す。"""
    by_week = {}
    for row in (existing or {}).get("rows", []):
        if isinstance(row, dict) and isinstance(row.get("period_start"), str):
            by_week[row["period_start"]] = row
    for row in new_rows:
        by_week[row["period_start"]] = row
    return [by_week[key] for key in sorted(by_week)]


def build_collection(ref, rows, captured_at, existing):
    """変化が無いときは captured_at を据え置く (再取込で書き込みを発生させない)。"""
    if existing and existing.get("rows") == rows and existing.get("job_id") == ref:
        captured_at = existing.get("captured_at", captured_at)
    return {"schema_version": 1, "source": "airwork", "job_id": ref,
            "captured_at": captured_at, "rows": rows}


def summarize(rows):
    """人が読む要約。cpa は応募 0 のとき ''。"""
    if not rows:
        return None
    latest = max(row["period_start"] for row in rows)
    cutoff = (datetime.date.fromisoformat(latest) - datetime.timedelta(days=21)).isoformat()
    last4 = [row for row in rows if row["period_start"] >= cutoff]
    cost4 = sum(row.get("cost_yen") or 0 for row in last4)
    apps4 = sum(row.get("applications") or 0 for row in last4)
    last = next(row for row in rows if row["period_start"] == latest)
    return {
        "airwork_cost_last4w": cost4,
        "airwork_applications_last4w": apps4,
        "airwork_cpa_last4w": round(cost4 / apps4) if apps4 > 0 else "",
        "airwork_cost_last_week": last.get("cost_yen") or 0,
        "airwork_last_week_start": latest,
        "airwork_cost_total": sum(row.get("cost_yen") or 0 for row in rows),
    }


def norm(value):
    if value is None:
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10]
    if re.fullmatch(r"\d{12,}", text):
        return datetime.datetime.fromtimestamp(
            int(text) / 1000, datetime.timezone.utc).date().isoformat()
    try:
        number = float(text)
        return str(int(number)) if number == int(number) else str(number)
    except ValueError:
        return text


def plan_listing(ref, props, new_rows, captured_at, zero_rows=()):
    """-> 書くプロパティ (変化なしなら None)。

    zero_rows: 有料広告が無かった週の 0 の行。すでに行がある週 (取れていた実績) は上書きしない。
    """
    existing = parse_existing(props.get(PERF))
    rows = merge_rows(existing, new_rows)
    have = {row["period_start"] for row in rows}
    extra = [z for z in zero_rows if z["period_start"] not in have]
    if extra:
        rows = merge_rows({"rows": rows}, extra)
    collection = build_collection(ref, rows, captured_at, existing)
    wanted = {PERF: json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
              **summarize(rows)}
    same = norm(props.get(PERF)) == norm(wanted[PERF]) and all(
        norm(props.get(k)) == norm(wanted[k]) for k in SUMMARY)
    return None if same else wanted


def match_records(records, keys):
    """検索結果 -> {(login_id, ref): [record,...]}。keys にある (login, ref) だけ残す。

    id_airwork が同じでも airwork_account_login_id が違う LISTING は対象外。
    """
    wanted = set(keys)
    found = collections.defaultdict(list)
    for record in records:
        props = record.get("properties") or {}
        key = ((props.get(PROP_LOGIN) or "").strip(), (props.get(PROP_REF) or "").strip())
        if key in wanted:
            found[key].append(record)
    return found


# ---- HubSpot -----------------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, token, session=None, sleep=time.sleep, clock=time.monotonic):
        import requests
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        self.sleep, self.clock, self.last = sleep, clock, None

    def call(self, method, path, body=None, attempts=6):
        for attempt in range(attempts):
            if self.last is not None:
                wait = HUBSPOT_INTERVAL - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            response = self.session.request(method, HUBSPOT + path, headers=self.headers,
                                            json=body, timeout=60)
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                self.sleep(max(delay, 1.0))
                continue
            if response.status_code >= 500 and attempt < attempts - 1:
                self.sleep(2.0)
                continue
            return response
        raise RuntimeError("hubspot_rate_limited")

    def find_by_refs(self, refs):
        """id_airwork が refs に含まれる LISTING (100 件ずつ)。口座の絞り込みは呼び出し側。"""
        found = []
        for index in range(0, len(refs), BATCH):
            chunk = refs[index:index + BATCH]
            after = None
            while True:
                body = {"filterGroups": [{"filters": [
                    {"propertyName": PROP_REF, "operator": "IN", "values": chunk}]}],
                    "properties": READ_PROPERTIES, "limit": 100}
                if after:
                    body["after"] = after
                response = self.call("POST", f"/crm/v3/objects/{LISTINGS}/search", body)
                if response.status_code != 200:
                    raise RuntimeError("hubspot_search_failed_%d" % response.status_code)
                data = response.json()
                found.extend(data.get("results", []))
                after = ((data.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    break
        return found

    def open_listings(self, logins):
        """口座 (airwork_account_login_id) が logins にある公開中の AirWork の LISTING。"""
        found = []
        for index in range(0, len(logins), BATCH):
            chunk = logins[index:index + BATCH]
            after = None
            while True:
                body = {"filterGroups": [{"filters": [
                    {"propertyName": PROP_LOGIN, "operator": "IN", "values": chunk},
                    {"propertyName": PROP_STATUS, "operator": "EQ", "value": OPEN_STATUS},
                    {"propertyName": PROP_REF, "operator": "HAS_PROPERTY"}]}],
                    "properties": READ_PROPERTIES + ["hs_createdate"], "limit": 100}
                if after:
                    body["after"] = after
                response = self.call("POST", f"/crm/v3/objects/{LISTINGS}/search", body)
                if response.status_code != 200:
                    raise RuntimeError("hubspot_search_failed_%d" % response.status_code)
                data = response.json()
                found.extend(data.get("results", []))
                after = ((data.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    break
        return found

    def batch_update(self, inputs):
        response = self.call("POST", f"/crm/v3/objects/{LISTINGS}/batch/update", {"inputs": inputs})
        if response.status_code == 207:
            return not response.json().get("errors")
        return response.status_code in (200, 201, 204)


def _zero_rows_for(records, weeks_by_login):
    """公開中の LISTING ごとの 0 の行 -> ({(login, ref): [zero_row,...]}, {(login, ref): record}, ambiguous)。

    求人を作った週より前の週には足さない (まだ無かった求人に 0 を付けない)。
    """
    zeros, recs, seen, ambiguous = {}, {}, collections.Counter(), set()
    for record in records:
        props = record.get("properties") or {}
        key = ((props.get(PROP_LOGIN) or "").strip(), (props.get(PROP_REF) or "").strip())
        if not all(key) or key[0] not in weeks_by_login:
            continue
        seen[key] += 1
        if seen[key] > 1:
            ambiguous.add(key)
            continue
        created = norm(props.get("hs_createdate"))[:10]
        rows = [zero_row(w) for w in sorted(weeks_by_login[key[0]])
                if not created or week_from_dirname(w)[1] >= created]
        if rows:
            zeros[key], recs[key] = rows, record
    for key in ambiguous:
        zeros.pop(key, None)
        recs.pop(key, None)
    return zeros, recs, len(ambiguous)


def run(hubspot, directory, apply=False, limit=None, now=None, zero_fill=False,
        empty_weeks=None):
    """zero_fill=True で、受け取りが済んだ週に有料広告が無かった公開中の求人へ 0 を書く。

    empty_weeks: {login_id: {週}} (「その週は実績なし」と分かった週。ZIP が無い週)。
    """
    data, load_stats = load_dir(directory)
    captured_at = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = collections.Counter(files=load_stats.get("files", 0), pairs=len(data))
    errors = collections.Counter({k: v for k, v in load_stats.items()
                                  if k in ("unknown_account", "bad_zip", "no_sponsor_csv")})
    refs = sorted({ref for _, ref in data})
    by_key = match_records(hubspot.find_by_refs(refs), data.keys()) if refs else {}
    zeros, zero_recs = {}, {}
    if zero_fill:
        weeks = collected_weeks(directory)
        for login, ws in (empty_weeks or {}).items():
            weeks.setdefault(login, set()).update(ws)
        if weeks:
            zeros, zero_recs, amb = _zero_rows_for(hubspot.open_listings(sorted(weeks)), weeks)
            if amb:
                errors["ambiguous_key_open"] += amb
    pending = []
    for key in sorted(set(data) | set(zeros)):
        if key in data:
            records = by_key.get(key, [])
            if not records:
                stats["unmatched"] += 1
                continue
            if len(records) > 1:
                errors["ambiguous_key"] += 1
                continue
            stats["matched"] += 1
            record, real = records[0], list(data[key].values())
        else:
            record, real = zero_recs[key], []
        zrows = zeros.get(key, [])
        wanted = plan_listing(key[1], record.get("properties") or {}, real, captured_at,
                              zero_rows=zrows)
        if wanted is None:
            stats["skipped_same"] += 1
            continue
        if limit is not None and stats["to_write"] >= limit:
            continue
        stats["to_write"] += 1
        if key not in data:
            stats["zero_filled"] += 1
        pending.append({"id": record["id"], "properties": wanted})
    if apply:
        for index in range(0, len(pending), BATCH):
            chunk = pending[index:index + BATCH]
            if hubspot.batch_update(chunk):
                stats["written"] += len(chunk)
            else:
                errors["hubspot_write_failed"] += len(chunk)
    return {"mode": "apply" if apply else "dry-run", "files": stats["files"],
            "pairs": stats["pairs"], "matched": stats["matched"],
            "unmatched": stats["unmatched"], "to_write": stats["to_write"],
            "written": stats["written"], "skipped_same": stats["skipped_same"],
            "zero_fill_listings": len(zeros), "zero_filled_to_write": stats["zero_filled"],
            "errors": dict(errors),
            "listing_ids": [item["id"] for item in pending] if limit is not None else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=DEFAULT_DIR, help="fetcher の保存先 (週ごとの ZIP)")
    parser.add_argument("--apply", action="store_true", help="HubSpot に書く (既定は dry-run)")
    parser.add_argument("--limit", type=int, help="書き込む LISTING を最大 N 件に (試走)")
    parser.add_argument("--no-zero-fill", action="store_true",
                        help="有料広告が無かった週に 0 を書かない (既定は書く)")
    parser.add_argument("--empty-file", default=DEFAULT_EMPTY_FILE,
                        help="「その週は実績なし」の記録 (aw_billing_empty.json)")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except Exception:  # noqa: BLE001
        pass
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not token:
        print(json.dumps({"error": "missing_hubspot_token"}))
        return 1
    try:
        summary = run(HubSpotClient(token), args.dir, apply=args.apply, limit=args.limit,
                      zero_fill=not args.no_zero_fill,
                      empty_weeks=load_empty_weeks(args.empty_file))
    except RuntimeError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
