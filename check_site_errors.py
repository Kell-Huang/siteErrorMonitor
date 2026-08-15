#!/usr/bin/env python3
"""
Productsup Site Error Monitor

Reads monitored projects from monitor_projects.json, fetches errors for
active sites only, filters recent Error entries, and sends a summary to
DingTalk.

Configuration is loaded from config.json. Sensitive values are read from
environment variables:
  - PRODUCTSUP_TOKEN : Productsup API token
  - DINGTALK_WEBHOOK : DingTalk robot webhook URL
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

try:
    BERLIN_TZ = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    print("[ERROR] Missing timezone data. Install tzdata package.")
    sys.exit(1)


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


def retry_request(method, url, headers, params=None, retries=3, timeout=30):
    """Send HTTP request with retry and exponential backoff."""
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, timeout=timeout
            )
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


def fetch_sites_for_project(base_url, project_id, headers, retry_count, timeout):
    """Return list of sites for a project."""
    url = f"{base_url}/projects/{project_id}/sites"
    data = retry_request("GET", url, headers, retries=retry_count, timeout=timeout)
    if data and data.get("success"):
        return data.get("Sites", [])
    return []


def fetch_errors_for_site(base_url, site_id, headers, retry_count, timeout, limit):
    """
    Fetch all errors for a site using pagination.

    Returns:
        tuple: (errors_list, failed_flag)
    """
    all_errors = []
    failed = False
    offset = 0
    while True:
        params = {"offset": offset, "limit": limit}
        url = f"{base_url}/sites/{site_id}/errors"
        data = retry_request(
            "GET", url, headers, params=params, retries=retry_count, timeout=timeout
        )
        if data is None:
            failed = True
            break
        if not data.get("success"):
            failed = True
            break
        errors = data.get("Errors", [])
        all_errors.extend(errors)
        if len(errors) < limit:
            break
        offset += limit
    return all_errors, failed


def is_within_time_range(error_datetime, hours):
    """Return True if Berlin time string is within last N hours."""
    try:
        err_time = datetime.strptime(error_datetime, "%Y-%m-%d %H:%M:%S")
        err_time = err_time.replace(tzinfo=BERLIN_TZ)
    except (ValueError, TypeError):
        return False
    err_time_utc = err_time.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    return err_time_utc >= now_utc - timedelta(hours=hours)


def filter_errors(errors, time_range_hours):
    """Return only recent Error entries."""
    filtered = []
    for e in errors:
        if e.get("type") != "Error":
            continue
        if not is_within_time_range(e.get("datetime", ""), time_range_hours):
            continue
        filtered.append(e)
    return filtered


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
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
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
    if len(msg) > max_len:
        return msg[:max_len] + "..."
    return msg


def build_markdown(keyword, project_error_map, report_date, failed_sites_count,
                   max_errors_per_site=5, max_message_len=500):
    """Build markdown report text."""
    lines = [f"### {keyword} Productsup Site Errors Report ({report_date})\n"]

    if project_error_map:
        for project_name, data in project_error_map.items():
            rate = data["rate"]
            lines.append(f"**{project_name}**")
            lines.append("")
            lines.append(f"**Error rate: {rate[0]}/{rate[1]} ({rate[2]}%)**")
            for site in data["sites"]:
                lines.append(f"- Site {site['site_name']} ({site['site_id']}):")
                errors_to_show = site["errors"][:max_errors_per_site]
                for err in errors_to_show:
                    msg = truncate_message(err.get("message", ""), max_message_len)
                    error_code = err.get("error", "")
                    classification = err.get("classification", "")
                    dt = err.get("datetime", "")
                    lines.append(
                        f"  - [Error] {error_code} - {classification} - {dt}"
                    )
                    if msg:
                        lines.append(f"    {msg}")
                additional = len(site["errors"]) - max_errors_per_site
                if additional > 0:
                    lines.append(f"    + additional {additional} errors")
            lines.append("")
    else:
        lines.append("✅ No errors found in all monitored active sites.")

    if failed_sites_count > 0:
        lines.append(f"\n⚠ {failed_sites_count} sites failed to check")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("Productsup Site Error Monitor")
    print("=" * 60)

    config = load_json_file("config.json")
    base_url = config.get("pu_base_url", "https://platform-api.productsup.io/platform/v2")
    errors_limit = config.get("errors_limit", 500)
    timeout = config.get("timeout", 30)
    keyword = config.get("keyword", "")
    time_range_hours = config.get("time_range_hours", 24)
    retry_count = config.get("retry_count", 3)
    max_concurrency = config.get("max_concurrency", 5)

    monitor_data = load_json_file("monitor_projects.json")
    monitored_projects = monitor_data.get("projects", [])
    if not monitored_projects:
        print("[ERROR] No projects in monitor_projects.json")
        sys.exit(1)

    token = get_env("PRODUCTSUP_TOKEN")
    webhook = get_env("DINGTALK_WEBHOOK")
    headers = {"X-Auth-Token": token}

    print(f"[INFO] Monitored projects configured: {len(monitored_projects)}")

    monitored_sites = []
    for proj in monitored_projects:
        project_id = proj.get("project_id")
        project_name = proj.get("project_name", f"Project_{project_id}")
        sites = fetch_sites_for_project(
            base_url, project_id, headers, retry_count, timeout
        )
        active_sites = [s for s in sites if s.get("status") == "active"]
        specified_sites = proj.get("sites", [])

        if specified_sites:
            for s in active_sites:
                if s.get("id") in [x.get("site_id") for x in specified_sites]:
                    monitored_sites.append({
                        "project_id": project_id,
                        "project_name": project_name,
                        "site_id": s.get("id"),
                        "site_name": s.get("title") or s.get("name", ""),
                    })
        else:
            for s in active_sites:
                monitored_sites.append({
                    "project_id": project_id,
                    "project_name": project_name,
                    "site_id": s.get("id"),
                    "site_name": s.get("title") or s.get("name", ""),
                })

    print(f"[INFO] Monitored active sites: {len(monitored_sites)}")

    print("\n[1/2] Fetching errors for active sites...")
    site_errors = {}
    failed_sites = 0
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        future_to_site = {
            executor.submit(
                fetch_errors_for_site,
                base_url,
                s["site_id"],
                headers,
                retry_count,
                timeout,
                errors_limit,
            ): s
            for s in monitored_sites
        }
        for future in as_completed(future_to_site):
            site = future_to_site[future]
            site_id = site["site_id"]
            try:
                all_errors, failed = future.result()
                if failed:
                    failed_sites += 1
                filtered = filter_errors(all_errors, time_range_hours)
                if filtered:
                    site_errors[site_id] = filtered
                    print(f"  Site {site_id}: {len(filtered)} error(s) after filtering")
                else:
                    print(f"  Site {site_id}: 0 errors after filtering")
            except Exception as e:
                failed_sites += 1
                print(f"  Site {site_id}: ERROR {e}")

    print("\n[2/2] Building report...")
    project_active_count = {}
    for s in monitored_sites:
        pname = s["project_name"]
        project_active_count[pname] = project_active_count.get(pname, 0) + 1

    project_error_map = {}
    for s in monitored_sites:
        pname = s["project_name"]
        site_id = s["site_id"]
        if site_id in site_errors:
            if pname not in project_error_map:
                project_error_map[pname] = {"sites": [], "rate": (0, 0)}
            project_error_map[pname]["sites"].append({
                "site_name": s["site_name"],
                "site_id": site_id,
                "errors": site_errors[site_id],
            })

    for pname in project_error_map:
        error_count = len(project_error_map[pname]["sites"])
        total_count = project_active_count.get(pname, 0)
        percent = round((error_count / total_count) * 100, 1) if total_count > 0 else 0
        project_error_map[pname]["rate"] = (error_count, total_count, percent)

    report_date = datetime.now().strftime("%Y-%m-%d")
    markdown_text = build_markdown(
        keyword,
        project_error_map,
        report_date,
        failed_sites,
    )
    print("\n" + markdown_text)

    print("\nSending to DingTalk...")
    if not send_dingtalk(webhook, markdown_text, timeout):
        sys.exit(1)

    print("\n[OK] Site error monitoring completed.")


if __name__ == "__main__":
    main()