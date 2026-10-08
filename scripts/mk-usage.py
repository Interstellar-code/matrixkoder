#!/usr/bin/env python3
"""MatrixKoder mk-usage: CodexBar -> normalised usage ledger.

Normalises CodexBar rate limit windows into ~/.matrixkoder/usage.json.
Merges with lane-health.py pane-text exhausted_* annotations.

Usage:
  mk-usage.py [--once] [--out PATH] [--providers p1,p2]
  mk-usage.py --watch [--interval 300] [--out PATH] [--providers p1,p2]
  mk-usage.py --self-test
"""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

PROVIDER_MAP = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "zai": "opencode",
    "opencode": "opencode",
    "opencodego": "opencode",
    "ollama": "opencode",
}

# OpenCode providers all feed the same agent but distinct per-plan pools.
PROVIDER_POOL = {
    "zai": "zai",
    "opencodego": "go",
    "ollama": "ollama",
    "opencode": "zen",
}

EXHAUSTED_FIELDS = (
    "exhausted_until",
    "exhausted_window",
    "exhausted_guess",
    "exhausted_source",
    "exhausted_observed_at",
    "exhausted_evidence",
)


def default_ledger():
    return os.path.join(
        os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder"),
        "usage.json",
    )


@contextlib.contextmanager
def usage_lock():
    """# ponytail: the one lock both writers of usage.json take — this poll and lane-health.py's quota
    mark. Without it a poll read the file and its atomic replace then dropped a fresh mark. Fixed under
    <home>, not beside the ledger, so the two serialise even with different --out/--ledger paths."""
    path = os.path.join(
        os.path.expanduser(os.environ.get("MATRIXKODER_HOME") or "~/.matrixkoder"),
        "usage.json.lock",
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def default_codexbar_config():
    return os.path.expanduser("~/.config/codexbar/config.json")


def get_enabled_providers(config_path=None):
    path = config_path or default_codexbar_config()
    if not os.path.isfile(path):
        return ["claude", "antigravity", "codex"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        enabled = [p["id"] for p in cfg.get("providers", []) if p.get("enabled")]
        return enabled if enabled else ["claude", "antigravity", "codex"]
    except Exception:
        return ["claude", "antigravity", "codex"]


def classify_kind(window_minutes):
    if window_minutes == 300:
        return "5h"
    elif window_minutes == 10080:
        return "week"
    elif window_minutes in (43200, 44640):
        return "month"
    else:
        return "other"


def parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


def format_pct(val):
    if val is None:
        return 0
    try:
        f = float(val)
        rounded = round(f, 1)
        if rounded.is_integer():
            return int(rounded)
        return rounded
    except (ValueError, TypeError):
        return 0


def clean_error_message(msg):
    if not msg:
        return ""
    cleaned = re.sub(r"^[A-Za-z0-9_-]+\s+connection failed:\s*", "", str(msg).strip(), flags=re.I)
    return cleaned.strip()


def run_codexbar_provider(provider, timeout=60):
    try:
        proc = subprocess.run(
            ["codexbar", "usage", "--format", "json", "--provider", provider],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timeout": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "returncode": -1,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "timeout": True,
        }
    except FileNotFoundError:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "codexbar binary not found on PATH",
            "timeout": False,
        }
    except Exception as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
            "timeout": False,
        }


# Pool mapping: (agent, substring in window id or lowercase title prefix) -> pool. Extend here.
POOL_RULES = {
    "agy": [("gemini", ("-gemini-",), ("gemini",)), ("claude-gpt", ("-3p-",), ("claude and gpt", "claude/gpt"))],
    "claude": [("fable", ("scoped-fable",), ("fable only",))],
}
PRIMARY_POOL = {"agy": "gemini"}  # agent-level five_hour/weekly come from this pool (default: "default")


def pool_of(agent_key, win_id, title):
    wid, t = str(win_id or "").lower(), str(title or "").lower()
    for name, id_subs, title_prefixes in POOL_RULES.get(agent_key, []):
        if any(s in wid for s in id_subs) or t.startswith(title_prefixes):
            return name
    return "default"


def dedupe_windows(windows):
    """One window per (pool, kind, resets_at); prefer antigravity-quota-summary-* ids."""
    out, seen = [], {}
    for w in windows:
        if not w.get("resets_at"):
            out.append(w)
            continue
        key = (w["pool"], w["kind"], w["resets_at"])
        if key not in seen:
            seen[key] = len(out)
            out.append(w)
        elif str(w["id"]).startswith("antigravity-quota-summary-") and not str(out[seen[key]]["id"]).startswith("antigravity-quota-summary-"):
            out[seen[key]] = w
    return out


def summarize_pool(ws):
    def best(kind):
        c = [w for w in ws if w["kind"] == kind]
        if not c:
            return None
        b = max(c, key=lambda w: (w["used_pct"], w["resets_at"] or ""))
        return {"used_pct": b["used_pct"], "resets_at": b["resets_at"]}

    ex = [w for w in ws if w.get("used_pct", 0) >= 100 and w.get("resets_at")]
    until = None
    if ex:
        until = max(ex, key=lambda w: parse_iso(w["resets_at"]) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))["resets_at"]
    return {"five_hour": best("5h"), "weekly": best("week"), "exhausted_until": until}


def carry_pane_mark(dst, existing, prev_future, prev_until, cb_until=None):
    """Keep lane-health's future pane-text mark on `dst`, the entry the router reads back. CodexBar's
    own later exhausted window wins; it is None when CodexBar reports no windows at all, which is the
    normal case for the OpenCode plans — that silence must not clear the mark. -> kept?"""
    if not prev_future:
        return False
    for k in EXHAUSTED_FIELDS:
        if k in existing:
            dst[k] = existing[k]
    if cb_until:
        cb_dt, prev_dt = parse_iso(cb_until), parse_iso(prev_until)
        if cb_dt and prev_dt and cb_dt > prev_dt:
            dst["exhausted_until"] = cb_until
    return True


def normalize_provider_entry(provider, raw_result_or_json, existing_entry=None, now=None):
    now = now or datetime.datetime.now().astimezone()
    existing_entry = existing_entry or {}

    agent_key = PROVIDER_MAP.get(provider, provider)
    provider_pool = PROVIDER_POOL.get(provider)

    raw_error = None
    is_timeout = False
    data_list = None

    if isinstance(raw_result_or_json, dict) and "stdout" in raw_result_or_json and "timeout" in raw_result_or_json:
        if raw_result_or_json["timeout"]:
            is_timeout = True
            raw_error = "timeout after 60s"
        else:
            try:
                data_list = json.loads(raw_result_or_json["stdout"])
            except Exception:
                raw_error = (
                    raw_result_or_json["stderr"].strip()
                    or raw_result_or_json["stdout"].strip()
                    or f"exit code {raw_result_or_json['returncode']}"
                )
    elif isinstance(raw_result_or_json, list):
        data_list = raw_result_or_json
    elif isinstance(raw_result_or_json, dict):
        data_list = [raw_result_or_json]
    else:
        raw_error = "invalid input"

    # Check existing entry for lane-health exhausted_* fields
    # only lane-health's pane-text evidence is kept; an exhausted_until this script wrote
    # itself is recomputed from codexbar every poll
    prev_until = existing_entry.get("exhausted_until") if existing_entry.get("exhausted_source") == "pane-text" else None
    prev_dt = parse_iso(prev_until) if prev_until else None
    prev_future = bool(prev_dt is not None and prev_dt > now)

    if is_timeout:
        entry = {
            "status": "error",
            "error": "timeout after 60s",
            "windows": [],
        }
        if prev_future:
            for k in EXHAUSTED_FIELDS:
                if k in existing_entry:
                    entry[k] = existing_entry[k]
        return entry

    if data_list is None:
        entry = {
            "status": "error",
            "error": raw_error or "unknown error",
            "windows": [],
        }
        if prev_future:
            for k in EXHAUSTED_FIELDS:
                if k in existing_entry:
                    entry[k] = existing_entry[k]
        return entry

    if not data_list or not isinstance(data_list, list):
        entry = {
            "status": "error",
            "error": "empty output from codexbar",
            "windows": [],
        }
        if prev_future:
            for k in EXHAUSTED_FIELDS:
                if k in existing_entry:
                    entry[k] = existing_entry[k]
        return entry

    item = data_list[0]
    if not isinstance(item, dict):
        entry = {
            "status": "error",
            "error": "unexpected output shape",
            "windows": [],
        }
        if prev_future:
            for k in EXHAUSTED_FIELDS:
                if k in existing_entry:
                    entry[k] = existing_entry[k]
        return entry

    if item.get("error"):
        err = item["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        entry = {
            "status": "n/a",
            "error": clean_error_message(msg),
            "windows": [],
        }
        if agent_key == "opencode" and provider_pool is not None:
            entry["pools"] = {provider_pool: {"five_hour": None, "weekly": None, "exhausted_until": None}}
        if prev_future:
            for k in EXHAUSTED_FIELDS:
                if k in existing_entry:
                    entry[k] = existing_entry[k]
            if agent_key == "opencode" and provider_pool is not None:
                carry_pane_mark(entry["pools"][provider_pool], entry, True, prev_until)
        return entry

    # Success case
    usage_data = item.get("usage") or {}
    labels = item.get("rateWindowLabels") or {}

    observed_at = None
    if "updatedAt" in usage_data and usage_data["updatedAt"]:
        dt_up = parse_iso(usage_data["updatedAt"])
        if dt_up:
            observed_at = dt_up.astimezone().isoformat(timespec="seconds")
    if not observed_at:
        observed_at = now.isoformat(timespec="seconds")

    windows = []

    def add_window(win_id, win_dict, custom_title=None, label_key=None):
        if not win_dict or not isinstance(win_dict, dict):
            return
        win_min = win_dict.get("windowMinutes")
        kind = classify_kind(win_min)

        if custom_title:
            title = custom_title
        elif label_key and labels.get(label_key):
            lbl = labels[label_key]
            if lbl.lower() == "session":
                title = "5h"
            elif lbl.lower() == "weekly":
                title = "week"
            else:
                title = lbl
        else:
            title = kind

        resets_at = win_dict.get("resetsAt")
        reset_text = win_dict.get("resetDescription")
        raw_pct = win_dict.get("usedPercent", 0)

        dt_reset = parse_iso(resets_at)
        is_past = bool(dt_reset is not None and dt_reset <= now)

        if is_past:
            used_pct = 0
        else:
            used_pct = format_pct(raw_pct)

        w_entry = {
            "id": win_id,
            "title": title,
            "kind": kind,
            "used_pct": used_pct,
            "resets_at": resets_at,
            "reset_text": reset_text,
        }
        if is_past:
            w_entry["reset_passed"] = True
        windows.append(w_entry)

    if usage_data.get("primary"):
        add_window("primary", usage_data["primary"], label_key="primary")
    if usage_data.get("secondary"):
        add_window("secondary", usage_data["secondary"], label_key="secondary")
    for ew in usage_data.get("extraRateWindows") or []:
        if isinstance(ew, dict):
            add_window(ew.get("id"), ew.get("window") or {}, custom_title=ew.get("title"))

    if agent_key == "opencode" and provider_pool is not None:
        for w in windows:
            w["pool"] = provider_pool
        if windows:
            pools = {provider_pool: summarize_pool(windows)}
        else:
            pools = {}
        pool_entry = {
            "status": "ok",
            "source": "codexbar",
            "observed_at": observed_at,
            "windows": windows,
            "pools": pools,
        }
        if provider_pool in pools:
            pool_entry["five_hour"] = pools[provider_pool].get("five_hour")
            pool_entry["weekly"] = pools[provider_pool].get("weekly")
            pool_entry["available_pools"] = [provider_pool] if not pools[provider_pool].get("exhausted_until") else []
            pool_entry["exhausted_until"] = pools[provider_pool].get("exhausted_until")
        else:
            pool_entry["five_hour"] = None
            pool_entry["weekly"] = None
            pool_entry["available_pools"] = []
            pool_entry["exhausted_until"] = None
        # the mark lands inside pools.<pool>, the dict available_pools and the router actually read;
        # a CodexBar silent about that plan (every OpenCode one) must not hand it back as available
        pool_view = pool_entry["pools"].get(provider_pool) if provider_pool else None
        if pool_view is not None and carry_pane_mark(pool_view, existing_entry, prev_future, prev_until,
                                                     pool_view.get("exhausted_until")):
            pool_entry["available_pools"] = [] if pool_view.get("exhausted_until") else [provider_pool]
            for k in EXHAUSTED_FIELDS:
                if k in pool_view and k not in pool_entry:
                    pool_entry[k] = pool_view[k]
        return pool_entry

    for w in windows:
        w["pool"] = pool_of(agent_key, w["id"], w["title"])
    windows = dedupe_windows(windows)

    pools = {}
    for w in windows:
        pools.setdefault(w["pool"], []).append(w)
    pools = {name: summarize_pool(ws) for name, ws in pools.items()}
    available_pools = [n for n, pl in pools.items() if not pl["exhausted_until"]]

    primary_name = PRIMARY_POOL.get(agent_key, "default")
    if primary_name not in pools and pools:
        primary_name = next(iter(pools))
    prim = pools.get(primary_name) or {}
    five_hour = prim.get("five_hour")
    weekly = prim.get("weekly")

    # agent exhausted = its primary pool exhausted (claude's `fable` is a sub-scope, never enough alone)
    cb_exhausted_until = prim.get("exhausted_until")

    entry = {
        "status": "ok",
        "source": "codexbar",
        "observed_at": observed_at,
        "windows": windows,
        "five_hour": five_hour,
        "weekly": weekly,
        "pools": pools,
        "available_pools": available_pools,
    }

    if prev_future:
        entry["exhausted_until"] = prev_until
        for k in ("exhausted_window", "exhausted_guess", "exhausted_source", "exhausted_observed_at", "exhausted_evidence"):
            if k in existing_entry:
                entry[k] = existing_entry[k]
        if cb_exhausted_until:
            cb_dt = parse_iso(cb_exhausted_until)
            if cb_dt and cb_dt > prev_dt:
                entry["exhausted_until"] = cb_exhausted_until
    else:
        entry["exhausted_until"] = cb_exhausted_until

    return entry


def load_ledger(path):
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(abs_path):
        return {"updated_at": None, "agents": {}}
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"updated_at": None, "agents": {}}
            if "agents" not in data or not isinstance(data["agents"], dict):
                data["agents"] = {}
            return data
    except Exception:
        return {"updated_at": None, "agents": {}}


def atomic_write_ledger(path, data):
    abs_path = os.path.abspath(os.path.expanduser(path))
    d = os.path.dirname(abs_path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".usage-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, abs_path)


def merge_opencode_entries(entries):
    """Merge per-provider OpenCode entries into one agent record with distinct pools."""
    merged = {
        "status": "ok",
        "source": "codexbar",
        "windows": [],
        "pools": {},
        "available_pools": [],
        "exhausted_until": None,
    }
    observed_at = None
    any_na = False
    first_ok_pool = None
    for e in entries:
        st = e.get("status")
        if st == "ok":
            merged["windows"].extend(e.get("windows") or [])
            for name, pl in (e.get("pools") or {}).items():
                merged["pools"][name] = pl
            if e.get("observed_at"):
                observed_at = e["observed_at"]
            if first_ok_pool is None:
                for name, pl in (e.get("pools") or {}).items():
                    first_ok_pool = (name, pl)
                    break
        elif st == "n/a":
            any_na = True
            for name, pl in (e.get("pools") or {}).items():
                merged["pools"][name] = pl
        for k in EXHAUSTED_FIELDS:
            if k in e and k not in merged:
                merged[k] = e[k]
    if first_ok_pool:
        merged["observed_at"] = observed_at or e.get("observed_at")
        merged["five_hour"] = first_ok_pool[1].get("five_hour")
        merged["weekly"] = first_ok_pool[1].get("weekly")
        merged["available_pools"] = [n for n, pl in merged["pools"].items() if (pl.get("five_hour") is not None or pl.get("weekly") is not None) and not pl.get("exhausted_until")]
        exhausted = [pl.get("exhausted_until") for pl in merged["pools"].values() if pl.get("exhausted_until")]
        usable = [pl for pl in merged["pools"].values() if pl.get("five_hour") is not None or pl.get("weekly") is not None]
        if exhausted and len(exhausted) == len(usable):
            merged["exhausted_until"] = max(exhausted, key=lambda iso: parse_iso(iso) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
    elif any_na:
        merged["status"] = "n/a" if not any(pl.get("five_hour") is not None or pl.get("weekly") is not None for pl in merged["pools"].values()) else "ok"
        if not merged.get("observed_at"):
            merged["observed_at"] = entries[0].get("observed_at") if entries else None
    else:
        if not merged.get("observed_at"):
            merged["observed_at"] = entries[0].get("observed_at") if entries else None
    return merged


def poll_and_update(providers, out_path, now=None):
    now = now or datetime.datetime.now().astimezone()

    # Every CodexBar fetch first, outside the lock: it is a slow external call, and holding an
    # exclusive lock across it would make every other writer (a lane-health quota mark) queue behind
    # the network. Nothing below depends on the fetch order, so this is the same read-modify-write.
    raw = [(pid, run_codexbar_provider(pid, timeout=60)) for pid in providers]

    with usage_lock():
        ledger_data = load_ledger(out_path)
        agents = ledger_data.get("agents", {})

        ok_agents = []
        na_agents = []
        error_agents = []

        opencode_entries = []
        for provider_id, raw_res in raw:
            agent_key = PROVIDER_MAP.get(provider_id, provider_id)
            provider_pool = PROVIDER_POOL.get(provider_id)
            existing_agent = agents.get(agent_key, {})
            if agent_key == "opencode":
                existing_pool_entry = existing_agent.get("pools", {}).get(provider_pool, {}) if provider_pool and isinstance(existing_agent.get("pools"), dict) else {}
            else:
                existing_pool_entry = existing_agent
            norm_entry = normalize_provider_entry(provider_id, raw_res, existing_entry=existing_pool_entry, now=now)

            if agent_key == "opencode":
                opencode_entries.append(norm_entry)
                continue

            agents[agent_key] = norm_entry

            st = norm_entry.get("status")
            if st == "ok":
                ok_agents.append(agent_key)
            elif st == "n/a":
                na_agents.append(agent_key)
            else:
                error_agents.append(agent_key)

        if opencode_entries:
            merged = merge_opencode_entries(opencode_entries)
            agents["opencode"] = merged
            st = merged.get("status")
            if st == "ok":
                ok_agents.append("opencode")
            elif st == "n/a":
                na_agents.append("opencode")
            else:
                error_agents.append("opencode")

        ledger_data["updated_at"] = now.isoformat(timespec="seconds")
        ledger_data["agents"] = agents

        atomic_write_ledger(out_path, ledger_data)

    return {"ok": ok_agents, "na": na_agents, "error": error_agents,
            "out": os.path.abspath(os.path.expanduser(out_path))}


def self_test():
    # usage_lock() resolves <home> independently of --out, so pin it to a temp dir before the first
    # poll: a run must never take a lock (or fall back to the ledger) inside the real ~/.matrixkoder
    mk_home = tempfile.mkdtemp(prefix="mk-usage-home-")
    os.environ["MATRIXKODER_HOME"] = mk_home
    try:
        self_test_body()
    finally:
        shutil.rmtree(mk_home, ignore_errors=True)


def self_test_body():
    # 1. Kind by windowMinutes
    assert classify_kind(300) == "5h"
    assert classify_kind(10080) == "week"
    assert classify_kind(43200) == "month"
    assert classify_kind(44640) == "month"
    assert classify_kind(123) == "other"
    assert classify_kind(None) == "other"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    fixtures_dir = os.path.join(script_dir, "fixtures")

    # 2. Antigravity fixture: primary is weekly, highest-weekly pick, exhausted_until
    agy_fixture_path = os.path.join(fixtures_dir, "codexbar-antigravity.json")
    with open(agy_fixture_path, "r", encoding="utf-8") as f:
        agy_raw = json.load(f)

    # Use a reference time before the 5h reset (20:26:09Z) to test future window
    ref_now_future = datetime.datetime(2026, 10, 7, 19, 5, 0, tzinfo=datetime.timezone.utc)
    agy_norm = normalize_provider_entry("antigravity", agy_raw, now=ref_now_future)

    assert agy_norm["status"] == "ok"
    assert agy_norm["source"] == "codexbar"
    # Duplicates collapsed: primary == gemini-weekly, secondary == 3p-weekly (prefer quota-summary ids)
    assert not any(w["id"] in ("primary", "secondary") for w in agy_norm["windows"])
    gem_wk = [w for w in agy_norm["windows"] if w["pool"] == "gemini" and w["kind"] == "week"]
    assert len(gem_wk) == 1 and gem_wk[0]["id"] == "antigravity-quota-summary-gemini-weekly"
    assert gem_wk[0]["used_pct"] == 51.9 and gem_wk[0]["kind"] == "week"  # AGY primary was weekly, never 5h
    cg_wk = [w for w in agy_norm["windows"] if w["pool"] == "claude-gpt" and w["kind"] == "week"]
    assert len(cg_wk) == 1 and cg_wk[0]["used_pct"] == 100

    # Pools: claude-gpt exhausted, gemini still usable -> agent NOT exhausted
    assert agy_norm["pools"]["claude-gpt"]["exhausted_until"] == "2026-10-13T06:00:45Z"
    assert agy_norm["pools"]["gemini"]["exhausted_until"] is None
    assert agy_norm["available_pools"] == ["gemini"]

    # A stale exhausted_until this script wrote itself (no pane-text source) is recomputed, not kept
    stale = {"status": "ok", "source": "codexbar", "exhausted_until": "2026-10-13T06:00:45Z"}
    agy_stale = normalize_provider_entry("antigravity", agy_raw, existing_entry=stale, now=ref_now_future)
    assert agy_stale["exhausted_until"] is None
    # ...while lane-health's pane-text evidence is kept
    pane = {"exhausted_until": "2026-10-13T06:00:45Z", "exhausted_source": "pane-text"}
    agy_pane = normalize_provider_entry("antigravity", agy_raw, existing_entry=pane, now=ref_now_future)
    assert agy_pane["exhausted_until"] == "2026-10-13T06:00:45Z"
    assert agy_norm["exhausted_until"] is None
    assert agy_norm["weekly"] == {"used_pct": 51.9, "resets_at": "2026-10-11T12:32:16Z"}
    assert agy_norm["five_hour"]["used_pct"] == 10.5
    assert agy_norm["five_hour"]["resets_at"] == "2026-10-07T20:26:09Z"

    # Both pools exhausted -> agent exhausted until the PRIMARY (gemini) pool reset
    both = json.loads(json.dumps(agy_raw))
    for ew in both[0]["usage"]["extraRateWindows"]:
        if ew["id"] == "antigravity-quota-summary-gemini-weekly":
            ew["window"]["usedPercent"] = 100
    both_norm = normalize_provider_entry("antigravity", both, now=ref_now_future)
    assert both_norm["available_pools"] == []
    assert both_norm["exhausted_until"] == "2026-10-11T12:32:16Z"

    # 3. Claude fixture: kind 5h for primary, week for secondary, Fable only in windows
    claude_fixture_path = os.path.join(fixtures_dir, "codexbar-claude.json")
    with open(claude_fixture_path, "r", encoding="utf-8") as f:
        claude_raw = json.load(f)

    claude_norm = normalize_provider_entry("claude", claude_raw, now=ref_now_future)
    assert claude_norm["status"] == "ok"
    c_primary = next(w for w in claude_norm["windows"] if w["id"] == "primary")
    assert c_primary["kind"] == "5h"
    assert c_primary["title"] == "5h"
    assert c_primary["used_pct"] == 17

    c_secondary = next(w for w in claude_norm["windows"] if w["id"] == "secondary")
    assert c_secondary["kind"] == "week"
    assert c_secondary["title"] == "week"
    assert c_secondary["used_pct"] == 38

    c_fable = next(w for w in claude_norm["windows"] if w["id"] == "claude-weekly-scoped-fable")
    assert c_fable["kind"] == "week"
    assert c_fable["title"] == "Fable only"
    assert c_fable["used_pct"] == 14

    assert claude_norm["five_hour"] == {"used_pct": 17, "resets_at": "2026-10-07T20:30:00Z"}
    assert claude_norm["weekly"] == {"used_pct": 38, "resets_at": "2026-10-13T05:00:00Z"}
    assert claude_norm["exhausted_until"] is None
    assert c_fable["pool"] == "fable" and c_primary["pool"] == "default"
    assert claude_norm["pools"]["fable"]["weekly"]["used_pct"] == 14
    assert claude_norm["available_pools"] == ["default", "fable"]

    # Claude default pool at 100%, fable free -> agent exhausted (primary pool rules), fable still listed
    cl_full = json.loads(json.dumps(claude_raw))
    cl_full[0]["usage"]["secondary"]["usedPercent"] = 100
    cl_full_norm = normalize_provider_entry("claude", cl_full, now=ref_now_future)
    assert cl_full_norm["available_pools"] == ["fable"]
    assert cl_full_norm["exhausted_until"] == "2026-10-13T05:00:00Z"

    # 4. Reset-passed -> 0 and reset_passed: true
    # Use a reference time AFTER 20:26:09Z (e.g. 21:00:00Z)
    ref_now_past = datetime.datetime(2026, 10, 7, 21, 0, 0, tzinfo=datetime.timezone.utc)
    agy_past_norm = normalize_provider_entry("antigravity", agy_raw, now=ref_now_past)
    gemini_5h = next(w for w in agy_past_norm["windows"] if w["id"] == "antigravity-quota-summary-gemini-5h")
    assert gemini_5h["used_pct"] == 0
    assert gemini_5h.get("reset_passed") is True

    # Check that a window at 100% whose reset has passed resets to 0 and clears exhausted_until
    test_expired_100 = [
        {
            "usage": {
                "primary": {
                    "windowMinutes": 300,
                    "usedPercent": 100,
                    "resetsAt": "2026-10-07T18:00:00Z",
                    "resetDescription": "Reset an hour ago",
                }
            }
        }
    ]
    expired_norm = normalize_provider_entry("claude", test_expired_100, now=ref_now_future)
    assert expired_norm["windows"][0]["used_pct"] == 0
    assert expired_norm["windows"][0].get("reset_passed") is True
    assert expired_norm["five_hour"]["used_pct"] == 0
    assert expired_norm["exhausted_until"] is None

    # 5. Provider error -> status: "n/a", clean error message, windows: []
    codex_fixture_path = os.path.join(fixtures_dir, "codexbar-codex.json")
    with open(codex_fixture_path, "r", encoding="utf-8") as f:
        codex_raw = json.load(f)

    codex_norm = normalize_provider_entry("codex", codex_raw, now=ref_now_future)
    assert codex_norm["status"] == "n/a"
    assert codex_norm["error"] == "chatgpt authentication required to read rate limits"
    assert codex_norm["windows"] == []
    assert "five_hour" not in codex_norm
    assert "weekly" not in codex_norm

    # 6. Timeout -> status: "error"
    timeout_raw = {"returncode": -1, "stdout": "", "stderr": "", "timeout": True}
    timeout_norm = normalize_provider_entry("claude", timeout_raw, now=ref_now_future)
    assert timeout_norm["status"] == "error"
    assert timeout_norm["error"] == "timeout after 60s"
    assert timeout_norm["windows"] == []

    # 7. Merge keeps unpolled agents, lane-health exhausted_* contract, atomic write
    tmp_dir = tempfile.mkdtemp(prefix="mk-usage-test-")
    try:
        ledger_path = os.path.join(tmp_dir, "usage.json")
        initial_data = {
            "updated_at": "2026-10-07T12:00:00+02:00",
            "agents": {
                "opencode": {
                    "status": "ok",
                    "source": "codexbar",
                    "windows": [{"id": "primary", "title": "5h", "kind": "5h", "used_pct": 5}],
                },
                "claude": {
                    "status": "ok",
                    "source": "codexbar",
                    "exhausted_until": "2026-10-07T23:00:00+02:00",
                    "exhausted_window": "5h",
                    "exhausted_guess": True,
                    "exhausted_source": "pane-text",
                    "exhausted_observed_at": "2026-10-07T18:00:00+02:00",
                    "exhausted_evidence": ["Claude usage limit reached"],
                },
            },
        }
        atomic_write_ledger(ledger_path, initial_data)
        assert os.path.isfile(ledger_path)
        assert os.stat(ledger_path).st_mode & 0o777 == 0o644
        # Verify no temporary files left in directory
        assert os.listdir(tmp_dir) == ["usage.json"]

        # Simulate update for claude (merging into ledger with unpolled opencode)
        now_test = datetime.datetime(2026, 10, 7, 20, 0, 0, tzinfo=datetime.timezone.utc)
        cur_ledger = load_ledger(ledger_path)
        claude_existing = cur_ledger["agents"]["claude"]
        new_claude = normalize_provider_entry("claude", claude_raw, existing_entry=claude_existing, now=now_test)
        cur_ledger["agents"]["claude"] = new_claude
        cur_ledger["updated_at"] = now_test.isoformat(timespec="seconds")
        atomic_write_ledger(ledger_path, cur_ledger)

        # Reload and check assertions
        reloaded = load_ledger(ledger_path)
        # Unpolled agent is preserved
        assert "opencode" in reloaded["agents"]
        assert reloaded["agents"]["opencode"]["windows"][0]["used_pct"] == 5

        # Claude entry preserved future exhausted_* from lane-health
        r_claude = reloaded["agents"]["claude"]
        assert r_claude["status"] == "ok"
        assert r_claude["exhausted_until"] == "2026-10-07T23:00:00+02:00"
        assert r_claude["exhausted_window"] == "5h"
        assert r_claude["exhausted_guess"] is True
        assert r_claude["exhausted_source"] == "pane-text"
        assert r_claude["exhausted_evidence"] == ["Claude usage limit reached"]

        # Now test that when exhausted_until is in the past, all exhausted_* fields are cleared
        now_past_exhaust = datetime.datetime(2026, 10, 8, 1, 0, 0, tzinfo=datetime.timezone.utc)
        cleared_claude = normalize_provider_entry("claude", claude_raw, existing_entry=r_claude, now=now_past_exhaust)
        assert cleared_claude["exhausted_until"] is None
        for k in ("exhausted_window", "exhausted_guess", "exhausted_source", "exhausted_observed_at", "exhausted_evidence"):
            assert k not in cleared_claude

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 8. Provider polling passes CodexBar ids through unchanged (opencode polls opencode, zai polls zai)
    polled = []

    def fake_run(provider, timeout=60):
        polled.append(provider)
        return {"returncode": 0, "stdout": json.dumps([{"usage": {}}]), "stderr": "", "timeout": False}

    # patch through globals(): a bare assignment here would shadow the name for the whole function
    mod = sys.modules[__name__]
    real_run = mod.run_codexbar_provider
    mod.run_codexbar_provider = fake_run
    try:
        map_dir = tempfile.mkdtemp(prefix="mk-usage-map-")
        try:
            summary = poll_and_update(["opencode", "zai"], os.path.join(map_dir, "usage.json"), now=ref_now_future)
            assert polled == ["opencode", "zai"]
            assert summary["ok"] == ["opencode"]
            assert load_ledger(os.path.join(map_dir, "usage.json"))["agents"]["opencode"]["status"] == "ok"
        finally:
            shutil.rmtree(map_dir, ignore_errors=True)
    finally:
        mod.run_codexbar_provider = real_run

    # 9. OpenCode multi-pool merge: three providers become one agent with distinct pools
    def fake_run_oc(provider, timeout=60):
        polled.append(provider)
        if provider == "zai":
            return {
                "returncode": 1,
                "stdout": json.dumps([{"error": {"message": "Error: No available fetch strategy for zai.", "kind": "provider"}}]),
                "stderr": "",
                "timeout": False,
            }
        elif provider == "ollama":
            return {
                "returncode": 1,
                "stdout": json.dumps([{"error": {"message": "Error: No Ollama session cookie found; sign in at ollama.com", "kind": "provider"}}]),
                "stderr": "",
                "timeout": False,
            }
        return {
            "returncode": 0,
            "stdout": json.dumps([{
                "usage": {
                    "primary": {
                        "windowMinutes": 300,
                        "usedPercent": 0,
                        "resetsAt": "2026-10-07T20:00:00Z",
                        "resetDescription": "",
                    },
                    "secondary": {
                        "windowMinutes": 10080,
                        "usedPercent": 0,
                        "resetsAt": "2026-10-14T00:00:00Z",
                        "resetDescription": "",
                    },
                }
            }]),
            "stderr": "",
            "timeout": False,
        }

    mod.run_codexbar_provider = fake_run_oc
    try:
        map_dir = tempfile.mkdtemp(prefix="mk-usage-oc-")
        try:
            for order in (["zai", "opencodego", "ollama"], ["ollama", "opencodego", "zai"]):
                polled = []
                summary = poll_and_update(order, os.path.join(map_dir, "usage.json"), now=ref_now_future)
                assert set(polled) == {"zai", "opencodego", "ollama"}
                assert summary["ok"] == ["opencode"]
                ledger = load_ledger(os.path.join(map_dir, "usage.json"))
                oc = ledger["agents"]["opencode"]
                assert oc["status"] == "ok"
                assert set(oc["pools"].keys()) == {"zai", "go", "ollama"}
                assert oc["pools"]["zai"]["five_hour"] is None
                assert oc["pools"]["zai"].get("exhausted_until") is None
                assert oc["pools"]["go"]["five_hour"] == {"used_pct": 0, "resets_at": "2026-10-07T20:00:00Z"}
                assert oc["pools"]["ollama"]["five_hour"] is None
                assert oc["available_pools"] == ["go"]
                assert oc["five_hour"] == {"used_pct": 0, "resets_at": "2026-10-07T20:00:00Z"}
                assert oc["weekly"] == {"used_pct": 0, "resets_at": "2026-10-14T00:00:00Z"}
        finally:
            shutil.rmtree(map_dir, ignore_errors=True)
    finally:
        mod.run_codexbar_provider = real_run

    # 10. lane-health's pane-text mark survives a poll, on an agent and on an OpenCode pool. CodexBar
    # reports no windows for the OpenCode plans, so a poll that recomputed the pool would hand an
    # exhausted plan straight back to the router.
    mod.run_codexbar_provider = fake_run_oc
    try:
        keep_dir = tempfile.mkdtemp(prefix="mk-usage-keep-")
        try:
            keep_ledger = os.path.join(keep_dir, "usage.json")
            pane_mark = {"exhausted_until": "2026-10-08T16:16:57+02:00",
                         "exhausted_window": "5h", "exhausted_guess": False,
                         "exhausted_source": "pane-text",
                         "exhausted_observed_at": "2026-10-08T11:50:00+02:00",
                         "exhausted_evidence": ["Usage limit reached for 5 hour"]}
            json.dump({"agents": {"opencode": {"status": "ok", "source": "codexbar",
                                               "pools": {"zai": {"five_hour": None, "weekly": None,
                                                                 "exhausted_until": None,
                                                                 **pane_mark}}}}},
                      open(keep_ledger, "w"))
            keep_now = datetime.datetime(2026, 10, 8, 11, 50, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
            summary = poll_and_update(["zai"], keep_ledger, now=keep_now)
            assert summary["na"] == ["opencode"], summary
            oc = load_ledger(keep_ledger)["agents"]["opencode"]
            zai_pool = oc["pools"]["zai"]
            for k, v in pane_mark.items():
                assert zai_pool.get(k) == v, (k, zai_pool)
            assert oc["available_pools"] == [], oc  # the marked plan is not offered to the router
            assert oc["exhausted_source"] == "pane-text" and oc["exhausted_window"] == "5h", oc
            assert oc["exhausted_until"] is None, oc  # one plan down is not the whole agent down
            # once the marked window has passed, the mark is gone and the plan is live again
            poll_and_update(["zai"], keep_ledger, now=keep_now + datetime.timedelta(hours=6))
            zai_after = load_ledger(keep_ledger)["agents"]["opencode"]["pools"]["zai"]
            assert "exhausted_source" not in zai_after, zai_after
            assert zai_after["exhausted_until"] is None, zai_after

            # same mark on a pool CodexBar *does* report numbers for: the mark survives the poll
            go_ledger = os.path.join(keep_dir, "go.json")
            json.dump({"agents": {"opencode": {"status": "ok", "source": "codexbar",
                                               "pools": {"go": {"five_hour": None, "weekly": None,
                                                               "exhausted_until": None, **pane_mark}}}}},
                      open(go_ledger, "w"))
            poll_and_update(["opencodego"], go_ledger, now=keep_now)
            go_pool = load_ledger(go_ledger)["agents"]["opencode"]["pools"]["go"]
            assert go_pool["five_hour"] == {"used_pct": 0, "resets_at": "2026-10-07T20:00:00Z"}, go_pool
            for k, v in pane_mark.items():
                assert go_pool.get(k) == v, (k, go_pool)
            assert load_ledger(go_ledger)["agents"]["opencode"]["available_pools"] == []
        finally:
            shutil.rmtree(keep_dir, ignore_errors=True)
    finally:
        mod.run_codexbar_provider = real_run

    # 11. Two writers, one lock: mk-usage's poll and lane-health's quota mark must not lose each
    # other's fields. Driven as two real processes against one <home>, so it fails without the flock.
    race_dir = tempfile.mkdtemp(prefix="mk-usage-race-")
    try:
        race_home = os.path.join(race_dir, "home")
        os.makedirs(race_home)
        race_ledger = os.path.join(race_home, "usage.json")
        race_bin = os.path.join(race_dir, "bin")
        os.makedirs(race_bin)
        entered, release = os.path.join(race_dir, "cb.entered"), os.path.join(race_dir, "cb.release")
        # the fake codexbar parks inside the poll's fetch window, so the mark lands exactly between the
        # poll's read and its write — the interleaving the lock has to make impossible
        with open(os.path.join(race_bin, "codexbar"), "w") as f:
            f.write("#!/usr/bin/env python3\n"
                    "import os, sys, time\n"
                    "base = os.environ['RACE_DIR']\n"
                    "open(os.path.join(base, 'cb.entered'), 'w').write('1')\n"
                    "for _ in range(2000):\n"
                    "    if os.path.exists(os.path.join(base, 'cb.release')):\n"
                    "        break\n"
                    "    time.sleep(0.01)\n"
                    "sys.stdout.write('[{\"usage\": {\"primary\": {\"windowMinutes\": 300,"
                    " \"usedPercent\": 12, \"resetsAt\": \"2026-10-09T20:00:00Z\"}}}]')\n")
        os.chmod(os.path.join(race_bin, "codexbar"), 0o755)
        json.dump({"agents": {}}, open(race_ledger, "w"))
        env = dict(os.environ, MATRIXKODER_HOME=race_home, RACE_DIR=race_dir,
                   PATH=race_bin + os.pathsep + os.environ["PATH"])
        poll_proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--providers", "zai",
                                      "--out", race_ledger], env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        for _ in range(2000):
            if os.path.exists(entered):
                break
            time.sleep(0.01)
        assert os.path.exists(entered), "fake codexbar never ran: the poll did not reach its fetch"
        # lane-health's record_quota, as its own process, holding the same <home>/usage.json.lock
        marker = subprocess.run([sys.executable, "-c",
                                 "import importlib.util, sys\n"
                                 "spec = importlib.util.spec_from_file_location('lh', sys.argv[1])\n"
                                 "lh = importlib.util.module_from_spec(spec)\n"
                                 "spec.loader.exec_module(lh)\n"
                                 "lh.record_quota(sys.argv[2], 'opencode',"
                                 " 'Usage limit reached for 5 hour. Your limit will reset at 2026-10-08 22:16:57',"
                                 " pool='zai')\n",
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "lane-health.py"),
                                 race_ledger],
                                env=env, capture_output=True, text=True)
        assert marker.returncode == 0, marker.stdout + marker.stderr
        open(release, "w").write("1")
        poll_out = poll_proc.communicate(timeout=120)
        assert poll_proc.returncode == 0, poll_out
        final = load_ledger(race_ledger)["agents"]["opencode"]
        assert final["pools"]["zai"]["exhausted_source"] == "pane-text", final  # the mark survived the poll
        assert final["pools"]["zai"]["five_hour"] == {"used_pct": 12, "resets_at": "2026-10-09T20:00:00Z"}, final
        assert os.path.exists(os.path.join(race_home, "usage.json.lock"))
    finally:
        shutil.rmtree(race_dir, ignore_errors=True)

    print("mk-usage self-test ok")


def main():
    if "--self-test" in sys.argv:
        self_test()
        return 0

    parser = argparse.ArgumentParser(description="MatrixKoder CodexBar usage normaliser")
    parser.add_argument("--providers", help="Comma-separated providers (e.g. claude,antigravity,codex)")
    parser.add_argument("--out", help="Output path (default: ~/.matrixkoder/usage.json)")
    parser.add_argument("--once", action="store_true", default=True, help="Run once and exit (default)")
    parser.add_argument("--watch", action="store_true", help="Loop indefinitely")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds for --watch (default: 300)")

    args = parser.parse_args()

    if args.providers:
        providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    else:
        providers = get_enabled_providers()

    out_path = args.out or default_ledger()

    if args.watch:
        try:
            while True:
                summary = poll_and_update(providers, out_path)
                print(json.dumps(summary, separators=(",", ":")), flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    else:
        summary = poll_and_update(providers, out_path)
        print(json.dumps(summary, separators=(",", ":")), flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
