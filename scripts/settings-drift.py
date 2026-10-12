#!/usr/bin/env python3
"""settings-drift: keep `hooks/tabs/settings.schema.ts` in sync with the live
schema and userConfig defaults.

The Settings tab draws its rows from `scripts/mk-config.py list --json` and
`.claude-plugin/plugin.json`. Both change over time, and a hand-kept snapshot
in the test file goes stale the moment either source moves. This script
regenerates a single TS module that exports the reduced shape the tests
import; `hooks/settings-io.test.ts` reads it and the test fails if either
source has drifted from the committed file.

Why a TS module (not a JSON file): the plugin's tsconfig does not enable
`resolveJsonModule`, so a static JSON import does not compile. The brief
allows a `.ts` file as a fallback for the same reason.

Usage:
  settings-drift.py --write         regenerate the committed file
  settings-drift.py --check         fail with the differing keys/fields if
                                    the file has drifted from the live
                                    sources (compares the whole shape, not
                                    a subset — every field the test reads
                                    must match)
  settings-drift.py --self-test     prove --check fails on a tampered temp
                                    copy (the committed file is never
                                    touched; a path is passed via an
                                    internal argv)

The script always uses a fresh temp data dir for `mk-config.py`. See MIN_YAML
for why that yaml carries an `agents:` block. The real `.matrixkoder/` is
never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MK_CONFIG = REPO_ROOT / "scripts" / "mk-config.py"
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
OUT_FILE = REPO_ROOT / "hooks" / "tabs" / "settings.schema.ts"

# MIN_YAML is the minimal yaml the schema needs to expose the rows the
# fixture also draws. Why this exists at all: mk-config.py derives some
# schema rows from the active yaml — without an `agents:` block, the
# `agents.<name>.available` rows and the `picks.<agent>` rows (one per agent
# that lists `models`) do not appear. The committed file and
# the TS test both rely on those rows being present, so the schema has to
# be captured with the same minimal yaml. The script writes this yaml into
# a fresh temp data dir; the real `.matrixkoder/` is never touched.
#
# If mk-config.py stops emitting the `agents.*` rows from this yaml (for
# example because the gating logic moves), `self_test` asserts on it and
# the test fails before the broken committed file is shipped.
MIN_YAML = (
    "agents:\n"
    "  opencode: { available: true, models: [a] }\n"
    "  claude: { available: true }\n"
    "  codex: { available: true, models: [c] }\n"
    "  agy: { available: true, models: [g] }\n"
    "  herdr: { available: true }\n"
)


# ---------- shape the live sources ----------

def list_schema() -> list[dict]:
    """The schema `mk-config.py list --json` prints against an empty data dir
    + a minimal agents block, reduced to the columns the tests check."""
    with tempfile.TemporaryDirectory() as data_dir:
        yaml = Path(data_dir) / "matrixkoder.yaml"
        yaml.write_text(MIN_YAML)
        out = subprocess.check_output(
            ["python3", str(MK_CONFIG), "--data", data_dir, "list", "--json"],
            text=True,
        )
    ans = json.loads(out)
    if not ans.get("ok") or not isinstance(ans.get("fields"), list):
        raise RuntimeError(f"mk-config list returned: {ans!r}")
    rows: list[dict] = []
    for f in ans["fields"]:
        if not isinstance(f, dict) or not isinstance(f.get("key"), str):
            continue
        rows.append({
            "key": f["key"],
            "scope": f.get("scope", ""),
            "default": f.get("default"),
            "editable": bool(f.get("editable", True)),
            "type": f.get("type", ""),
        })
    return rows


def user_config_defaults() -> dict[str, object]:
    """Every `userConfig` key in `.claude-plugin/plugin.json` mapped to its
    declared `default` (strings, numbers, and booleans only — the tab draws
    those)."""
    cfg = json.loads(PLUGIN_JSON.read_text())
    out: dict[str, object] = {}
    for k, f in (cfg.get("userConfig") or {}).items():
        if not isinstance(f, dict) or "default" not in f:
            continue
        d = f["default"]
        if isinstance(d, (str, int, float, bool)):
            out[k] = d
    return out


def live() -> dict:
    return {"schema": list_schema(), "userConfig": user_config_defaults()}


# ---------- the committed file ----------

def to_ts(live_obj: dict) -> str:
    """The TS module the tests import. `as const` keeps literal types so
    `editable` is the boolean the assertions expect, not widened to `boolean`."""
    body = json.dumps(live_obj, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        "// AUTO-GENERATED by scripts/settings-drift.py --write. Do not hand-edit.\n"
        "// Re-run `python3 scripts/settings-drift.py --write` after editing\n"
        "// `scripts/mk-config.py` or `.claude-plugin/plugin.json`.\n"
        "export const SETTINGS_SCHEMA = " + body + " as const\n"
    )


def from_ts(text: str) -> dict:
    """Parse the SETTINGS_SCHEMA export back into a dict. The file is
    TS-shaped so a tiny hand-rolled stripper is enough — we do not want a
    TS compiler dependency for one boolean check."""
    needle = "export const SETTINGS_SCHEMA = "
    i = text.find(needle)
    if i < 0:
        raise RuntimeError("settings.schema.ts: no SETTINGS_SCHEMA export")
    tail = text[i + len(needle):].lstrip()
    # Drop a trailing `as const` (possibly followed by `;` and/or whitespace)
    import re as _re
    tail = _re.sub(r"\s+as\s+const\s*;?\s*\Z", "", tail)
    if not (tail.startswith("{") and tail.endswith("}")):
        raise RuntimeError("settings.schema.ts: SETTINGS_SCHEMA is not an object")
    return json.loads(tail)


# ---------- write / check ----------

def cmd_write(live_obj: dict) -> int:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(to_ts(live_obj))
    print(f"wrote {OUT_FILE.relative_to(REPO_ROOT)} ({len(live_obj['schema'])} schema rows, {len(live_obj['userConfig'])} userConfig keys)")
    return 0


def diff_paths(a: object, b: object) -> list[str]:
    """Walk a and b and return dotted paths to every differing leaf, plus
    `shape` lines for type/keyset mismatches. The whole shape is compared
    — every field the test reads (key, scope, default, editable, type)
    must match. A flip of `editable` in mk-config.py is caught here
    because the parsed dicts no longer match."""
    out: list[str] = []

    def walk(x: object, y: object, path: str) -> None:
        if type(x) is not type(y):
            out.append(f"{path}: type differs (live={type(x).__name__} file={type(y).__name__})")
            return
        if isinstance(x, dict):
            x_keys = set(x.keys())
            y_keys = set(y.keys())
            for k in sorted(x_keys - y_keys):
                out.append(f"{path}.{k}: live has it, file does not")
            for k in sorted(y_keys - x_keys):
                out.append(f"{path}.{k}: file has it, live does not")
            for k in sorted(x_keys & y_keys):
                walk(x[k], y[k], f"{path}.{k}" if path else k)
        elif isinstance(x, list):
            if len(x) != len(y):
                out.append(f"{path}: list length differs (live={len(x)} file={len(y)})")
                return
            # Lists of dicts with a `key` field are compared by key so
            # the report names the row, not the index. Other lists are
            # compared positionally.
            if x and y and all(isinstance(v, dict) and "key" in v for v in x) and all(isinstance(v, dict) and "key" in v for v in y):
                x_by = {v["key"]: v for v in x if isinstance(v.get("key"), str)}
                y_by = {v["key"]: v for v in y if isinstance(v.get("key"), str)}
                for k in sorted(set(x_by) - set(y_by)):
                    out.append(f"{path}: live has key {k!r}, file does not")
                for k in sorted(set(y_by) - set(x_by)):
                    out.append(f"{path}: file has key {k!r}, live does not")
                for k in sorted(set(x_by) & set(y_by)):
                    walk(x_by[k], y_by[k], f"{path}[{k!r}]")
            else:
                for i, (xi, yi) in enumerate(zip(x, y)):
                    walk(xi, yi, f"{path}[{i}]")
        else:
            if x != y:
                out.append(f"{path}: differs (live={x!r} file={y!r})")

    walk(a, b, "")
    return out


def cmd_check(live_obj: dict, file_path: Path = OUT_FILE) -> int:
    """Compare the whole parsed shape of `file_path` against the live shape.

    The previous implementation compared only key sets, userConfig defaults
    and schema defaults, so a flip of `editable` / `scope` / `type` in
    mk-config.py could leave the committed file stale with no signal. The
    TS test (`hooks/settings-io.test.ts`, schema test) reads `editable` and
    `scope` from the file, so the test was then checking the wrong rows.
    The whole shape is small and deterministic — comparing it directly
    catches every field the test relies on."""
    if not file_path.exists():
        if file_path == OUT_FILE:
            print(f"{OUT_FILE.relative_to(REPO_ROOT)}: missing — run --write", file=sys.stderr)
        else:
            print(f"{file_path}: missing", file=sys.stderr)
        return 1
    file_obj = from_ts(file_path.read_text())
    problems = diff_paths(live_obj, file_obj)
    if problems:
        label = file_path.relative_to(REPO_ROOT) if file_path.is_absolute() and str(file_path).startswith(str(REPO_ROOT)) else file_path
        print(f"settings-drift: drift detected in {label}", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"  fix: python3 scripts/settings-drift.py --write", file=sys.stderr)
        return 1
    print("settings-drift: clean")
    return 0


# ---------- self-test ----------

def self_test() -> int:
    """Prove --check fails on a tampered copy and the committed file is
    never touched. We write a tampered variant to a temp file and pass
    its path to cmd_check via an internal argv."""
    if not OUT_FILE.exists():
        print("self-test: skipped (no settings.schema.ts yet — run --write first)", file=sys.stderr)
        return 1

    # Guard 1: the schema the live sources emit, with our minimal yaml,
    # must include the `agents.*` rows. If mk-config.py stops gating
    # those rows on the yaml, this fails before any tamper check runs —
    # the committed file would otherwise miss those rows with no signal.
    live_obj = live()
    schema_keys = {row["key"] for row in live_obj["schema"]}
    expected_agent_keys = {
        "agents.opencode.available",
        "agents.claude.available",
        "agents.codex.available",
        "agents.agy.available",
        "agents.herdr.available",
        "picks.opencode",
        "picks.codex",
        "picks.agy",
    }
    missing = sorted(expected_agent_keys - schema_keys)
    if missing:
        print(
            f"self-test FAIL: mk-config stopped emitting agents.*/picks.* rows from MIN_YAML: {missing}",
            file=sys.stderr,
        )
        return 1
    print("self-test: MIN_YAML still emits the agents.* rows ✓")

    # Guard 2: --check catches a tamper on a temp copy. A single byte flip
    # (editable false -> true) is enough to fail the whole-shape compare;
    # --check on the committed file must still pass.
    tampered = from_ts(OUT_FILE.read_text())
    if not tampered["schema"]:
        print("self-test: schema empty, cannot tamper", file=sys.stderr)
        return 1
    target = next(r for r in tampered["schema"] if r.get("editable") is True)
    target["editable"] = not target["editable"]  # toggle, so a False->True flip is just as caught as True->False
    tamper_path = OUT_FILE.with_name(OUT_FILE.name + ".tamper")
    try:
        tamper_path.write_text(to_ts(tampered))
        rc = cmd_check(live_obj, tamper_path)
        if rc == 0:
            print("self-test FAIL: --check did not flag a tampered editable flag", file=sys.stderr)
            return 1
        print("self-test: --check fails on a tampered editable flag ✓")
    finally:
        tamper_path.unlink(missing_ok=True)

    # Guard 3: the committed file is unchanged.
    rc = cmd_check(live_obj)
    if rc != 0:
        print("self-test FAIL: --check did not pass on the committed file", file=sys.stderr)
        return 1
    print("self-test: --check passes on the committed file ✓")
    return 0


# ---------- entry ----------

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate the committed file")
    g.add_argument("--check", action="store_true", help="fail if the file has drifted")
    g.add_argument("--self-test", action="store_true", help="prove --check catches tampering")
    # Internal: cmd_check target. Passed as a positional so selftest can
    # run cmd_check on a temp copy without ever touching the committed
    # file. Undocumented on purpose — users go through --check / --write.
    ap.add_argument("--check-path", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    live_obj = live()
    if args.write:
        return cmd_write(live_obj)
    if args.check_path is not None:
        return cmd_check(live_obj, Path(args.check_path))
    return cmd_check(live_obj)


if __name__ == "__main__":
    raise SystemExit(main())
