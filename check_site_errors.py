import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

try:
    BERLIN_TZ = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    print("[ERROR] Missing timezone data. Install tzdata package.")
    sys.exit(1)

# ==================== Rate Limiter ====================
_rate_lock = threading.Lock()          # protects _last_request_time
_rps_lock = threading.Lock()           # protects _requests_per_second
_last_request_time = 0.0
_requests_per_second = 5.0             # default, can be overridden by config.json


def set_requests_per_second(value):
    """Set global requests per second limit, guarding against non-positive values."""
    global _requests_per_second
    with _rps_lock:
        _requests_per_second = value if value > 0 else 5.0


def get_requests_per_second():
    """Return current requests per second limit."""
    with _rps_lock:
        return _requests_per_second


def rate_limited_wait():
    """
    Wait if necessary to respect the global request rate limit.
    This implementation releases the lock during sleep to allow concurrency.
    """
    global _last_request_time

    with _rps_lock:
        interval = 1.0 / _requests_per_second

    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        wait_time = max(0, interval - elapsed)

    if wait_time > 0:
        time.sleep(wait_time)

    with _rate_lock:
        _last_request_time = time.monotonic()


# ==================== File & Env Helpers ====================
def load_json_file(filepath):
    """Load and return JSON content from filepath, exit on error."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Failed to read {filepath}: {e}")
        sys.exit(1)


def get_env(name):
    """Return environment variable value or exit if missing."""
    value = os.getenv(name, "")
    if not value:
        print(f"[ERROR] Environment variable {name} is not set.")
        sys.exit(1)
    return value


def create_session():
    """Create a new requests Session with auth headers."""
    session = requests.Session()
    session.headers.update({"X-Auth-Token": get_env("PRODUCTSUP_TOKEN")})
    return session


# ==================== HTTP Request ====================
def retry_request(method, url, session, params=None, retries=3, timeout=30):
    """Send HTTP request with retry and exponential backoff."""
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        rate_limited_wait()
        try:
            resp = session.request(method, url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            print(
                f"[WARN] Request failed (HTTP {resp.status_code}) "
                f"on attempt {attempt}: {url}"
            )
        except Exception as e:
            print(f"[WARN] Request error on attempt {attempt}: {e}")

        if attempt < attempts:
            sleep_time = 2 ** (attempt - 1)
            print(f"       Retrying in {sleep_time}s...")
            time.sleep(sleep_time)

    print(f"[ERROR] Request failed after {attempts} attempts: {url}")
    return None


# ==================== API Functions ====================
def fetch_sites_for_project(base_url, project_id, retry_count, timeout):
    """
    Fetch sites for a project.
    Returns:
        tuple: (sites_list, failed_flag)
    """
    url = f"{base_url}/projects/{project_id}/sites"
    with create_session() as session:
        data = retry_request("GET", url, session, retries=retry_count, timeout=timeout)
    if data is None or not data.get("success"):
        return [], True
    return data.get("Sites", []) or [], False


def fetch_import_history(base_url, site_id, retry_count, timeout, limit=100):
    """
    Fetch import history for a site.
    Returns:
        tuple: (history_list, failed_flag)
    """
    url = f"{base_url}/sites/{site_id}/importhistory"
    params = {"limit": limit}
    with create_session() as session:
        data = retry_request("GET", url, session, params=params,
                            retries=retry_count, timeout=timeout)
    if data is None or not data.get("success"):
        return [], True
    return data.get("Importhistory", []) or [], False


def fetch_errors_for_site(base_url, site_id, retry_count, timeout, limit, pid=None):
    """
    Fetch all errors for a site (optionally for a specific process id).
    Returns:
        tuple: (errors_list, failed_flag)
    """
    # Validate limit to avoid infinite loop
    if limit is None or limit <= 0:
        limit = 500

    all_errors = []
    failed = False
    offset = 0
    with create_session() as session:
        while True:
            params = {"offset": offset, "limit": limit}
            if pid:
                params["pid"] = pid
            url = f"{base_url}/sites/{site_id}/errors"
            data = retry_request(
                "GET", url, session, params=params,
                retries=retry_count, timeout=timeout
            )
            if data is None:
                failed = True
                break
            if not data.get("success"):
                failed = True
                break
            errors = data.get("Errors", []) or []
            all_errors.extend(errors)
            if len(errors) < limit:
                break
            offset += limit
    return all_errors, failed


# ==================== Time & Filtering Helpers ====================
def _parse_datetime_robust(dt_str, assume_tz=None):
    """
    Parse datetime string robustly.
    - Try ISO 8601 via fromisoformat (handles timezone)
    - Fallback to '%Y-%m-%d %H:%M:%S' and assume given timezone
    Returns timezone-aware datetime in UTC, or None on failure.
    """
    if not dt_str:
        return None

    # Try fromisoformat (Python 3.7+)
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            if assume_tz:
                dt = dt.replace(tzinfo=assume_tz)
            else:
                # If no timezone and no assume_tz, assume UTC
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # Fallback to common format
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        if assume_tz:
            dt = dt.replace(tzinfo=assume_tz)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def is_within_time_range(error_datetime, hours):
    """
    Return True if error datetime is within last N hours.
    Handles various datetime formats.
    """
    dt_utc = _parse_datetime_robust(error_datetime, assume_tz=BERLIN_TZ)
    if dt_utc is None:
        return False
    now_utc = datetime.now(timezone.utc)
    return dt_utc >= now_utc - timedelta(hours=hours)


def is_recent_import(import_time_utc_str, hours):
    """
    Return True if import_time_utc string is within last N hours.
    Expects UTC time string without timezone.
    """
    dt_utc = _parse_datetime_robust(import_time_utc_str, assume_tz=timezone.utc)
    if dt_utc is None:
        return False
    now_utc = datetime.now(timezone.utc)
    return dt_utc >= now_utc - timedelta(hours=hours)


def filter_errors(errors, time_range_hours):
    """Return only recent Error entries (type == 'Error')."""
    filtered = []
    for e in errors:
        if e.get("type") != "Error":
            continue
        if not is_within_time_range(e.get("datetime", ""), time_range_hours):
            continue
        filtered.append(e)
    return filtered


def deduplicate_errors(errors):
    """
    Deduplicate errors based on (error code, classification, data/message).
    Returns a new list with duplicates removed (preserving order).
    """
    seen = set()
    result = []
    for err in errors:
        error_code = err.get("error", "")
        classification = err.get("classification", "")
        # Use data if available, else message
        detail = err.get("data", "")
        if not detail:
            detail = err.get("message", "")
        # Normalize detail for hashing
        if isinstance(detail, dict):
            detail_str = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        else:
            detail_str = str(detail)
        key = (error_code, classification, detail_str)
        if key not in seen:
            seen.add(key)
            result.append(err)
    return result


# ==================== DingTalk ====================
def send_dingtalk(webhook_url, markdown_text, timeout):
    """Send markdown message to DingTalk. Return True on success."""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "Site Errors Report",
            "text": markdown_text,
        },
    }
    try:
        rate_limited_wait()  # apply rate limit before sending
        with requests.Session() as session:
            resp = session.post(webhook_url, json=payload, timeout=timeout)
        if resp.status_code == 200 and resp.json().get("errcode") == 0:
            print("[OK] DingTalk message sent successfully.")
            return True
        print(
            f"[ERROR] DingTalk send failed: HTTP {resp.status_code}, "
            f"response: {resp.text}"
        )
        return False
    except Exception as e:
        print(f"[ERROR] DingTalk send exception: {e}")
        return False


def truncate_message(msg, max_len):
    """Truncate message to max_len characters if needed."""
    if not msg:
        return ""
    msg_str = str(msg)
    if len(msg_str) > max_len:
        return msg_str[:max_len] + "..."
    return msg_str


# ==================== Markdown Builder ====================
def build_markdown(
    keyword,
    project_summary,
    report_date,
    failed_sites_count,
    failed_projects_count=0,
    failed_sites_list=None,   # list of site names or dicts
    max_errors_per_site=5,
    max_message_len=500,
    max_failed_sites_display=5,
    show_error_message=False,
):
    """
    Build markdown report text.

    project_summary: dict where each key is project name, value is dict with:
        - total_active_sites: int
        - error_sites: list of site dicts
        - status: "ok" or "failed"   (if failed, project fetch failed)

    failed_sites_list: optional list of site names (str) or dicts with keys:
        'site_id', 'site_name' -- used for listing failed sites.
    max_failed_sites_display: maximum number of failed sites to show in report.
    """
    lines = [f"### {keyword} Site Errors Report ({report_date})\n"]

    total_active_sites = sum(
        data.get("total_active_sites", 0) for data in project_summary.values()
    )
    total_error_sites = sum(
        len(data.get("error_sites", [])) for data in project_summary.values()
    )

    # Summary
    overall_error_rate = (
        round((total_error_sites / total_active_sites) * 100, 1)
        if total_active_sites > 0
        else 0.0
    )

    lines.append("**Summary**")
    lines.append(f"- Total active sites monitored: {total_active_sites}")
    lines.append(f"- Sites with errors: {total_error_sites}")
    lines.append(f"- Overall error rate: {overall_error_rate}%")
    if failed_projects_count > 0:
        lines.append(f"- Failed projects: {failed_projects_count}")
    if failed_sites_count > 0:
        lines.append(f"- Failed sites: {failed_sites_count}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Show warning if no active sites, but still iterate over projects
    if total_active_sites == 0:
        lines.append("⚠ No active sites detected for monitoring.")
        lines.append("")

    # Projects loop (always executed)
    for idx, (project_name, data) in enumerate(project_summary.items(), start=1):
        total = data.get("total_active_sites", 0)
        error_sites = data.get("error_sites", [])
        status = data.get("status", "ok")

        lines.append(f"**{idx}. {project_name}**")
        lines.append("")

        if status == "failed":
            lines.append("⚠ Project check failed")
        elif total > 0:
            error_count = len(error_sites)
            percent = round((error_count / total) * 100, 1)
            lines.append(f"**Error rate: {error_count}/{total} ({percent}%)**")
        else:
            lines.append("**No active monitored sites**")

        if status == "failed":
            pass  # no further details
        elif error_sites:
            for site in error_sites:
                site_name = site.get("site_name", f"Site {site.get('site_id')}")
                lines.append(f"- **{site_name} ({site.get('site_id')})**:")
                errors_to_show = site["errors"][:max_errors_per_site]
                for err in errors_to_show:
                    error_code = err.get("error", "")
                    classification = err.get("classification", "")
                    dt = err.get("datetime", "")
                    lines.append(
                        f"  - [Error] {error_code} - {classification} - {dt}"
                    )
                    if show_error_message:
                        # Prefer 'data' field for error detail, fallback to 'message'
                        detail = err.get("data", "")
                        if not detail:
                            detail = err.get("message", "")
                        detail_str = truncate_message(detail, max_message_len)
                        if detail_str:
                            lines.append(f"    {detail_str}")
                additional = len(site["errors"]) - max_errors_per_site
                if additional > 0:
                    lines.append(f"    + additional {additional} errors")
        else:
            lines.append("✅ No errors found in this project")

        lines.append("")

    # Failed sites detail
    if failed_sites_count > 0 and failed_sites_list:
        lines.append(f"⚠ Failed to check {failed_sites_count} site(s).")
        if failed_sites_list:
            # Take first N sites
            show_failed = failed_sites_list[:max_failed_sites_display]
            lines.append(f"Failed sites (showing up to {max_failed_sites_display}):")
            for item in show_failed:
                if isinstance(item, dict):
                    name = item.get("site_name", f"Site {item.get('site_id')}")
                else:
                    name = str(item)
                lines.append(f"  - {name}")
            if len(failed_sites_list) > max_failed_sites_display:
                lines.append(f"  ... and {len(failed_sites_list) - max_failed_sites_display} more")
    lines.append("\n---")
    lines.append(
        "To add or remove monitored projects/sites, please update the config file or contact the administrator."
    )
    return "\n".join(lines)


# ==================== Main ====================
def main():
    print("=" * 60)
    print("Productsup Site Error Monitor")
    print("=" * 60)

    # Load config
    config = load_json_file("config.json")
    base_url = config.get(
        "pu_base_url", "https://platform-api.productsup.io/platform/v2"
    )
    errors_limit = config.get("errors_limit", 500)
    timeout = config.get("timeout", 30)
    keyword = config.get("keyword", "")
    time_range_hours = config.get("time_range_hours", 24)
    retry_count = config.get("retry_count", 3)
    max_concurrency = config.get("max_concurrency", 5)
    show_error_message = config.get("show_error_message", False)
    requests_per_second = config.get("requests_per_second", 5.0)
    # New configurable options
    import_history_limit = config.get("import_history_limit", 100)
    recent_processes_count = config.get("recent_processes_count", 2)
    max_failed_sites_display = config.get("max_failed_sites_display", 5)
    max_errors_per_site = config.get("max_errors_per_site", 5)
    max_message_len = config.get("max_message_len", 500)

    # Validate numeric parameters
    def validate_positive_int(value, default, name):
        if value is None or not isinstance(value, int) or value <= 0:
            print(f"[WARN] Invalid {name} value '{value}', using default {default}")
            return default
        return value

    def validate_positive_float(value, default, name):
        if value is None or not isinstance(value, (int, float)) or value <= 0:
            print(f"[WARN] Invalid {name} value '{value}', using default {default}")
            return default
        return float(value)

    retry_count = validate_positive_int(retry_count, 3, "retry_count")
    errors_limit = validate_positive_int(errors_limit, 500, "errors_limit")
    max_concurrency = validate_positive_int(max_concurrency, 5, "max_concurrency")
    time_range_hours = validate_positive_int(time_range_hours, 24, "time_range_hours")
    timeout = validate_positive_int(timeout, 30, "timeout")
    requests_per_second = validate_positive_float(
        requests_per_second, 5.0, "requests_per_second"
    )
    import_history_limit = validate_positive_int(
        import_history_limit, 100, "import_history_limit"
    )
    recent_processes_count = validate_positive_int(
        recent_processes_count, 2, "recent_processes_count"
    )
    max_failed_sites_display = validate_positive_int(
        max_failed_sites_display, 5, "max_failed_sites_display"
    )
    max_errors_per_site = validate_positive_int(
        max_errors_per_site, 5, "max_errors_per_site"
    )
    max_message_len = validate_positive_int(
        max_message_len, 500, "max_message_len"
    )

    set_requests_per_second(requests_per_second)

    # Load monitor projects
    monitor_data = load_json_file("monitor_projects.json")
    monitored_projects = monitor_data.get("projects", [])
    if not monitored_projects:
        print("[ERROR] No projects in monitor_projects.json")
        sys.exit(1)

    token = get_env("PRODUCTSUP_TOKEN")
    webhook_str = get_env("DINGTALK_WEBHOOK")
    webhook_urls = [w.strip() for w in webhook_str.split(",") if w.strip()]

    print(f"[INFO] Monitored projects configured: {len(monitored_projects)}")
    print(f"[INFO] Requests per second limit: {requests_per_second}")

    monitored_sites = []
    project_summary = {}
    failed_projects = []  # list of project names

    # Phase 0: Collect sites to monitor
    for proj in monitored_projects:
        project_id = proj.get("project_id")
        project_name = proj.get("project_name", f"Project_{project_id}")

        sites, failed = fetch_sites_for_project(
            base_url, project_id, retry_count, timeout
        )
        if failed:
            print(f"[ERROR] Failed to fetch sites for project: {project_name} (ID: {project_id})")
            failed_projects.append(project_name)
            # Mark this project as failed in summary
            project_summary[project_name] = {
                "total_active_sites": 0,
                "error_sites": [],
                "status": "failed",
            }
            continue

        active_sites = [s for s in sites if s.get("status") == "active"]
        specified_sites = proj.get("sites", [])

        if specified_sites:
            project_monitored_sites = [
                s
                for s in active_sites
                if str(s.get("id")) in [str(x.get("site_id")) for x in specified_sites]
            ]
        else:
            project_monitored_sites = active_sites

        total_active_sites = len(project_monitored_sites)
        project_summary[project_name] = {
            "total_active_sites": total_active_sites,
            "error_sites": [],
            "status": "ok",
        }

        for s in project_monitored_sites:
            site_id = s.get("id")
            site_name = s.get("title") or s.get("name") or f"Site {site_id}"
            monitored_sites.append(
                {
                    "project_id": project_id,
                    "project_name": project_name,
                    "site_id": site_id,
                    "site_name": site_name,
                }
            )

    print(f"[INFO] Monitored active sites: {len(monitored_sites)}")

    if not monitored_sites:
        print("[WARN] No active monitored sites found. Sending alert message only.")
        markdown_text = build_markdown(
            keyword,
            project_summary,
            datetime.now().strftime("%Y-%m-%d"),
            failed_sites_count=0,
            failed_projects_count=len(failed_projects),
            show_error_message=show_error_message,
            max_errors_per_site=max_errors_per_site,
            max_message_len=max_message_len,
            max_failed_sites_display=max_failed_sites_display,
        )
        print("\n" + markdown_text)
        print("\nSending to DingTalk...")
        sent_any = False
        for wh in webhook_urls:
            if send_dingtalk(wh, markdown_text, timeout):
                sent_any = True
        if not sent_any:
            sys.exit(1)
        print("\n[OK] Site error monitoring completed with warning.")
        return

    print("\n[1/2] Fetching errors for active sites (including historical processes)...")
    site_errors = {}          # site_id -> list of Error entries (filtered)
    failed_sites_info = {}    # site_id -> site_name for failed sites
    failed_sites_lock = threading.Lock()
    error_futures_lock = threading.Lock()

    # Thread pools
    history_executor = ThreadPoolExecutor(max_workers=max_concurrency)
    error_executor = ThreadPoolExecutor(max_workers=max_concurrency)
    error_futures = []  # list of (future, site_info, pid)

    # Phase 1: Submit history tasks for each site
    history_futures = []
    for s in monitored_sites:
        future = history_executor.submit(
            fetch_import_history,
            base_url,
            s["site_id"],
            retry_count,
            timeout,
            import_history_limit  # Use configurable limit
        )
        history_futures.append((future, s))

    # Callback for history completion
    def on_history_done(future, site_info):
        s = site_info
        site_id = s["site_id"]
        try:
            history, hist_failed = future.result()
            if hist_failed:
                with failed_sites_lock:
                    failed_sites_info[site_id] = s["site_name"]
                print(f"  Site {site_id} ({s['site_name']}): [ERROR] Failed to fetch import history")
                return

            # Filter history records by time_range_hours
            recent_records = []
            for rec in history:
                import_time = rec.get("import_time_utc") or rec.get("import_time")
                if import_time and is_recent_import(import_time, time_range_hours):
                    recent_records.append(rec)

            # Keep only latest N from filtered records
            # (history is already sorted desc by import_time, but we can sort to be safe)
            recent_records.sort(
                key=lambda x: x.get("import_time_utc") or x.get("import_time") or "",
                reverse=True
            )
            recent_records = recent_records[:recent_processes_count]  # configurable count

            if not recent_records:
                print(f"  Site {site_id} ({s['site_name']}): no recent import processes found, skipping.")
                return

            pids = []
            for rec in recent_records:
                pid = rec.get("pid")
                if pid:
                    pids.append(pid)
            pids = list(dict.fromkeys(pids))  # deduplicate

            print(f"  Site {site_id} ({s['site_name']}): found {len(pids)} recent processes")

            for pid in pids:
                err_future = error_executor.submit(
                    fetch_errors_for_site,
                    base_url,
                    site_id,
                    retry_count,
                    timeout,
                    errors_limit,
                    pid,
                )
                with error_futures_lock:
                    error_futures.append((err_future, s, pid))

        except Exception as e:
            with failed_sites_lock:
                failed_sites_info[site_id] = s["site_name"]
            print(f"  Site {site_id} ({s['site_name']}): [ERROR] Exception in history callback: {e}")

    # Register callbacks
    for future, s in history_futures:
        future.add_done_callback(lambda fut, s=s: on_history_done(fut, s))

    # Wait for all history tasks to complete (callbacks will submit error tasks)
    history_executor.shutdown(wait=True)

    # At this point all error tasks have been submitted (callbacks finished before shutdown returns)
    # Now collect results from error tasks
    errors_by_site = {}
    # Copy the list safely
    with error_futures_lock:
        futures_to_process = list(error_futures)

    for future, s, pid in futures_to_process:
        site_id = s["site_id"]
        try:
            all_errors, failed = future.result()
            if failed:
                with failed_sites_lock:
                    failed_sites_info[site_id] = s["site_name"]
            if site_id not in errors_by_site:
                errors_by_site[site_id] = []
            errors_by_site[site_id].extend(all_errors)
        except Exception as e:
            with failed_sites_lock:
                failed_sites_info[site_id] = s["site_name"]
            print(f"  Site {site_id}, pid {pid}: ERROR {e}")

    # Shutdown error executor
    error_executor.shutdown(wait=True)

    # Deduplicate errors per site and then filter
    for s in monitored_sites:
        site_id = s["site_id"]
        all_errors = errors_by_site.get(site_id, [])
        # Deduplicate
        all_errors = deduplicate_errors(all_errors)
        # Filter for Error type and time range
        filtered = filter_errors(all_errors, time_range_hours)
        if filtered:
            site_errors[site_id] = filtered
            print(f"  Site {site_id} ({s['site_name']}): {len(filtered)} error(s) after filtering")
        else:
            print(f"  Site {site_id} ({s['site_name']}): 0 errors after filtering")

    print("\n[2/2] Building report...")
    # Attach errors to project summary
    for s in monitored_sites:
        pname = s["project_name"]
        site_id = s["site_id"]
        if site_id in site_errors:
            project_summary[pname]["error_sites"].append(
                {
                    "site_name": s["site_name"],
                    "site_id": site_id,
                    "errors": site_errors[site_id],
                }
            )

    # Prepare failed sites list for report
    failed_sites_list = [
        {"site_id": sid, "site_name": name}
        for sid, name in failed_sites_info.items()
    ]

    report_date = datetime.now().strftime("%Y-%m-%d")
    markdown_text = build_markdown(
        keyword,
        project_summary,
        report_date,
        failed_sites_count=len(failed_sites_info),
        failed_projects_count=len(failed_projects),
        failed_sites_list=failed_sites_list,
        show_error_message=show_error_message,
        max_errors_per_site=max_errors_per_site,
        max_message_len=max_message_len,
        max_failed_sites_display=max_failed_sites_display,
    )
    print("\n" + markdown_text)

    print("\nSending to DingTalk...")
    sent_any = False
    for wh in webhook_urls:
        if send_dingtalk(wh, markdown_text, timeout):
            sent_any = True
    if not sent_any:
        sys.exit(1)

    print("\n[OK] Site error monitoring completed.")


if __name__ == "__main__":
    main()