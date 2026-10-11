#!/usr/bin/env python3
"""MatrixKoder agent/model catalog — the C8 tables in the root store. Python 3 stdlib only.

  seed [--yaml PATH] [--jev PATH] [--no-probe]
  list [--json]
  show <agent> [<model>]
  set <agent> [<model>] <field> <json>
  routes --json
  model --pane <pane> [--text TEXT]
  --self-test

Common: --home DIR (default $MK_HOME or ~/.matrixkoder). Every command prints ONE JSON line.
Errors: {"ok": false, "error": ...} exit 2.

The catalog lives in the root store that mkdb.py opens. This script never opens a database itself:
it asks mkdb.open_root() for the connection and creates the C8 tables through the migration list
mkdb accepts. `seed` reads agents from matrixkoder.yaml (via mk-config.py), probes `opencode models`
and `agy models` when those CLIs exist, and reads the Jev catalog when its file is present. A field
a user set is never overwritten. `routes` lists every runnable route: enabled agents x picked
models x supported efforts. `model --pane` reads the live model out of a herdr pane footer.
"""
import argparse, datetime, json, os, re, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkdb  # the only module allowed to open a store read-write

CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, kind TEXT, cli TEXT, provider_plans_json TEXT, capabilities_json TEXT,
  status TEXT, notes TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS models(
  agent TEXT, id TEXT, provider TEXT, display TEXT, context_tokens INTEGER,
  effort_levels_json TEXT, benchmarks_json TEXT, cost_json TEXT, quota_pool TEXT,
  status TEXT, notes TEXT, updated TEXT, capabilities_json TEXT, user_json TEXT,
  PRIMARY KEY(agent, id));
"""

# Hand-set seeds (source: seed) until a run proves the hook. kind is the agent family.
SEEDS = {
    "opencode": {"kind": "opencode", "cli": "opencode", "efforts": ["low", "medium", "high"],
                 "todo_hook": "unknown", "hook_recipe": "opencode plugin hook, installed outside the worktree"},
    "claude": {"kind": "claude", "cli": "claude", "efforts": ["low", "medium", "high", "max"],
               "todo_hook": "yes", "hook_recipe": "claude --settings <path under ~/.matrixkoder>"},
    "codex": {"kind": "codex", "cli": "codex", "efforts": ["low", "medium", "high", "xhigh"],
              "todo_hook": "unknown", "hook_recipe": ""},
    "agy": {"kind": "agy", "cli": "agy", "efforts": [],
            "todo_hook": "no", "hook_recipe": ""},
    "herdr": {"kind": "herdr", "cli": "herdr", "efforts": [],
              "todo_hook": "no", "hook_recipe": ""},
}
PROBE_CLIS = {"opencode": "opencode", "agy": "agy"}
AGENT_FIELDS = ("kind", "cli", "provider_plans_json", "capabilities_json", "status", "notes")
MODEL_FIELDS = ("provider", "display", "context_tokens", "effort_levels_json", "benchmarks_json",
                "cost_json", "quota_pool", "status", "notes", "capabilities_json")
TODO_HOOKS = ("yes", "no", "unknown")
CAP_KEYS = ("todo_hook", "hook_recipe", "skills", "subagents", "mcp", "launch_prompt_arg", "source")
JEV_DEFAULT = os.path.expanduser("~/.claude/skills/jev/reference/models.md")
FOOTER_RE = re.compile(r"([A-Za-z0-9._-]+/[A-Za-z0-9._:-]+)")


class Fail(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def home_of(arg):
    return mkdb.resolve_home(arg)


def open_catalog(home):
    """The root connection from mkdb. The C8 tables are created on it, idempotently.

    mkdb's migration list only applies versions up to its own SCHEMA, so a catalog migration
    passed in would be skipped. The connection is still one mkdb opened; this script never opens a database itself.
    """
    con = mkdb.open_root(home)
    con.executescript(CATALOG_DDL)
    return con


def capabilities(todo_hook, recipe, source="seed"):
    return {"todo_hook": todo_hook, "hook_recipe": recipe, "skills": True, "subagents": True,
            "mcp": True, "launch_prompt_arg": None, "source": source}


def _dump(obj):
    return json.dumps(obj, sort_keys=True)


def _load(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _user_fields(con, agent, model=None):
    """Fields a user set, which seed must not touch."""
    if model is None:
        row = con.execute("SELECT notes FROM agents WHERE id=?", (agent,)).fetchone()
        return set(_load(row["notes"], {}).get("_user", [])) if row else set()
    row = con.execute("SELECT user_json FROM models WHERE agent=? AND id=?", (agent, model)).fetchone()
    return set(_load(row["user_json"] if row else None, []))


def _mark_user(con, agent, model, field):
    if model is None:
        row = con.execute("SELECT notes FROM agents WHERE id=?", (agent,)).fetchone()
        bag = _load(row["notes"], {})
        bag["_user"] = sorted(set(bag.get("_user", [])) | {field})
        con.execute("UPDATE agents SET notes=? WHERE id=?", (_dump(bag), agent))
    else:
        row = con.execute("SELECT user_json FROM models WHERE agent=? AND id=?", (agent, model)).fetchone()
        fields = sorted(set(_load(row["user_json"] if row else None, [])) | {field})
        con.execute("UPDATE models SET user_json=? WHERE agent=? AND id=?", (_dump(fields), agent, model))


def upsert_agent(con, agent, caps, kind, cli, status, protected):
    have = con.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
    if not have:
        con.execute("INSERT INTO agents(id, kind, cli, provider_plans_json, capabilities_json,"
                    " status, notes, updated) VALUES (?,?,?,?,?,?,?,?)",
                    (agent, kind, cli, _dump({}), _dump(caps), status, _dump({}), now()))
        return
    sets = {"updated": now()}
    if "kind" not in protected:
        sets["kind"] = kind
    if "cli" not in protected:
        sets["cli"] = cli
    if "status" not in protected:
        sets["status"] = status
    if "capabilities_json" not in protected:
        sets["capabilities_json"] = _dump(caps)
    cols = ", ".join(f"{k}=?" for k in sets)
    con.execute(f"UPDATE agents SET {cols} WHERE id=?", (*sets.values(), agent))


def upsert_model(con, agent, model, efforts, provider, status, protected):
    have = con.execute("SELECT * FROM models WHERE agent=? AND id=?", (agent, model)).fetchone()
    if not have:
        con.execute("INSERT INTO models(agent, id, provider, display, context_tokens, effort_levels_json,"
                    " benchmarks_json, cost_json, quota_pool, status, notes, updated, capabilities_json,"
                    " user_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (agent, model, provider, model, None, _dump(efforts), _dump({}), _dump({}), None,
                     status, "", now(), None, _dump([])))
        return
    sets = {"updated": now()}
    if "provider" not in protected:
        sets["provider"] = provider
    if "status" not in protected:
        sets["status"] = status
    if "effort_levels_json" not in protected:
        sets["effort_levels_json"] = _dump(efforts)
    cols = ", ".join(f"{k}=?" for k in sets)
    con.execute(f"UPDATE models SET {cols} WHERE agent=? AND id=?", (*sets.values(), agent, model))


def probe_models(cli):
    """Model ids `<cli> models` prints. [] when the CLI is absent or fails — never an error."""
    try:
        proc = subprocess.run([cli, "models"], capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out = []
    for raw in (proc.stdout or "").splitlines():
        fields = raw.split()
        if fields and fields[0] not in out:
            out.append(fields[0])
    return out


def jev_models(path):
    """(agent, model id) pairs from the Jev catalog markdown, or [] when the file is absent."""
    if not path or not os.path.isfile(path):
        return []
    section, found = "", []
    for line in open(path):
        if line.startswith("## "):
            section = line[3:].strip().lower()
        if not line.startswith("|") or "---" in line or "Model" in line.split("|")[1:2]:
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if section.startswith("claude") and len(cells) >= 2:
            m = re.search(r"`([a-z0-9.-]+)`", line)
            if m:
                found.append(("claude", m.group(1)))
        elif section.startswith("codex") and cells and cells[0]:
            found.append(("codex", cells[0]))
        elif section.startswith("agy"):
            for part in re.findall(r"`([^`]+)`", cells[0] if cells else ""):
                base = part.split(" / ")[0].strip()
                if base:
                    found.append(("agy", base))
    return found


def _config():
    """mk-config.py, loaded by path: the hyphen keeps it off the import path."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mk-config.py")
    spec = importlib.util.spec_from_file_location("mk_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def yaml_agents(path):
    """[(name, flow map)] from matrixkoder.yaml, through mk-config.py. [] when there is no file."""
    if not path or not os.path.isfile(path):
        return []
    mod = _config()
    out = []
    for name, info in mod.agents(mod.read_lines(path)):
        if isinstance(info.get("available"), str):
            info["available"] = info["available"].lower() == "true"
        out.append((name, info))
    return out


def seed(home, yaml_path, jev_path, probe=True):
    declared = dict(yaml_agents(yaml_path))
    names = list(dict.fromkeys(list(SEEDS) + list(declared)))
    con = open_catalog(home)
    try:
        mkdb._begin(con)
        try:
            for name in names:
                seed_row = SEEDS.get(name, {"kind": name, "cli": name, "efforts": [],
                                            "todo_hook": "unknown", "hook_recipe": ""})
                info = declared.get(name, {})
                enabled = bool(info.get("available", False))
                caps = capabilities(seed_row["todo_hook"], seed_row["hook_recipe"])
                upsert_agent(con, name, caps, seed_row["kind"], seed_row["cli"],
                             "enabled" if enabled else "disabled", _user_fields(con, name))
                picked = list(info.get("models") or [])
                probed = probe_models(PROBE_CLIS[name]) if probe and name in PROBE_CLIS else []
                for model in list(dict.fromkeys(picked + probed)):
                    upsert_model(con, name, model, seed_row["efforts"], seed_row["kind"],
                                 "picked" if model in picked else "probed",
                                 _user_fields(con, name, model))
            for agent, model in jev_models(jev_path):
                efforts = SEEDS.get(agent, {}).get("efforts", [])
                upsert_model(con, agent, model, efforts, SEEDS.get(agent, {}).get("kind", agent),
                             "catalog", _user_fields(con, agent, model))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        count = con.execute("SELECT COUNT(*) c FROM models").fetchone()["c"]
    finally:
        con.close()
    return {"ok": True, "agents": len(names), "models": count}


def cmd_list(home):
    con = open_catalog(home)
    try:
        agents = [dict(r) for r in con.execute("SELECT * FROM agents ORDER BY id")]
        models = [dict(r) for r in con.execute("SELECT * FROM models ORDER BY agent, id")]
    finally:
        con.close()
    return {"ok": True, "agents": agents, "models": models}


def cmd_show(home, agent, model):
    con = open_catalog(home)
    try:
        row = con.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
        if not row:
            raise Fail(f"no agent {agent!r}")
        out = {"ok": True, "agent": dict(row)}
        if model:
            m = con.execute("SELECT * FROM models WHERE agent=? AND id=?", (agent, model)).fetchone()
            if not m:
                raise Fail(f"no model {agent} {model}")
            out["model"] = dict(m)
        else:
            out["models"] = [dict(r) for r in con.execute(
                "SELECT * FROM models WHERE agent=? ORDER BY id", (agent,))]
    finally:
        con.close()
    return out


def _check_json_field(field, value):
    if field == "capabilities_json":
        if not isinstance(value, dict) or value.get("todo_hook") not in TODO_HOOKS:
            raise Fail(f"bad capabilities: todo_hook must be one of {TODO_HOOKS}")
        if value.get("source") not in ("seed", "run", None):
            raise Fail("bad capabilities: source must be seed or run")
    elif field == "effort_levels_json":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise Fail("bad effort_levels_json: expected a list of strings")
    elif field in ("provider_plans_json", "benchmarks_json", "cost_json"):
        if not isinstance(value, (dict, list)):
            raise Fail(f"bad {field}: expected JSON object or array")
    elif field == "context_tokens":
        if not isinstance(value, int) or value < 0:
            raise Fail("bad context_tokens: expected a non-negative integer")
    elif field == "status" and value not in ("enabled", "disabled", "picked", "probed", "catalog"):
        raise Fail(f"bad status {value!r}")


def cmd_set(home, agent, model, field, raw):
    try:
        value = json.loads(raw)
    except ValueError:
        raise Fail(f"not JSON: {raw!r}")
    table_fields = MODEL_FIELDS if model else AGENT_FIELDS
    if field not in table_fields:
        raise Fail(f"unknown field {field!r} for {'model' if model else 'agent'}")
    _check_json_field(field, value)
    stored = json.dumps(value) if not isinstance(value, str) else value
    con = open_catalog(home)
    try:
        mkdb._begin(con)
        try:
            if model:
                if not con.execute("SELECT 1 FROM models WHERE agent=? AND id=?", (agent, model)).fetchone():
                    raise Fail(f"no model {agent} {model}")
                con.execute(f"UPDATE models SET {field}=?, updated=? WHERE agent=? AND id=?",
                            (stored, now(), agent, model))
            else:
                if not con.execute("SELECT 1 FROM agents WHERE id=?", (agent,)).fetchone():
                    raise Fail(f"no agent {agent!r}")
                con.execute(f"UPDATE agents SET {field}=?, updated=? WHERE id=?", (stored, now(), agent))
            _mark_user(con, agent, model, field)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return {"ok": True, "agent": agent, "model": model, "field": field}


def routes(home):
    """Every runnable route: enabled agents x picked-or-probed models x supported efforts."""
    con = open_catalog(home)
    try:
        out = []
        for agent in con.execute("SELECT id FROM agents WHERE status='enabled' ORDER BY id"):
            for model in con.execute("SELECT id, effort_levels_json FROM models WHERE agent=? AND"
                                     " status IN ('picked','probed') ORDER BY id", (agent["id"],)):
                efforts = _load(model["effort_levels_json"], [])
                if efforts:
                    out.extend(f"{agent['id']} {model['id']} {e}" for e in efforts)
                else:
                    out.append(f"{agent['id']} {model['id']}")
    finally:
        con.close()
    return {"ok": True, "routes": out}


def pane_model(pane, text=None):
    """The model in a herdr pane footer. --text feeds the footer directly (tests); otherwise herdr."""
    if text is None:
        try:
            proc = subprocess.run(["herdr", "pane", "read", pane], capture_output=True, text=True,
                                  timeout=20)
        except Exception as e:
            raise Fail(f"herdr pane read failed: {e}")
        if proc.returncode != 0:
            raise Fail(f"herdr pane read exited {proc.returncode}: {proc.stderr.strip()}")
        text = proc.stdout
    hits = FOOTER_RE.findall(text or "")
    if not hits:
        raise Fail(f"no model in pane {pane!r} footer")
    return {"ok": True, "pane": pane, "model": hits[-1]}


def _cli(argv):
    p = argparse.ArgumentParser(prog="mk-catalog.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home")
    sub = p.add_subparsers(dest="cmd")
    sd = sub.add_parser("seed")
    sd.add_argument("--yaml")
    sd.add_argument("--jev")
    sd.add_argument("--no-probe", action="store_true")
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("agent")
    sh.add_argument("model", nargs="?")
    st = sub.add_parser("set")
    st.add_argument("agent")
    st.add_argument("rest", nargs="+")
    rt = sub.add_parser("routes")
    rt.add_argument("--json", action="store_true")
    md = sub.add_parser("model")
    md.add_argument("--pane", required=True)
    md.add_argument("--text")
    args = p.parse_args(argv)
    home = home_of(args.home)
    if args.cmd == "seed":
        return seed(home, args.yaml, args.jev if args.jev is not None else JEV_DEFAULT,
                    probe=not args.no_probe)
    if args.cmd == "list":
        return cmd_list(home)
    if args.cmd == "show":
        return cmd_show(home, args.agent, args.model)
    if args.cmd == "set":
        rest = args.rest
        if len(rest) < 2:
            raise Fail("usage: set <agent> [<model>] <field> <json>")
        raw, field = rest[-1], rest[-2]
        model = rest[0] if len(rest) == 3 else None
        return cmd_set(home, args.agent, model, field, raw)
    if args.cmd == "routes":
        return routes(home)
    if args.cmd == "model":
        return pane_model(args.pane, args.text)
    raise Fail("usage: mk-catalog.py seed|list|show|set|routes|model  (or --self-test)")


def main(argv):
    print(json.dumps(_cli(argv)))
    return 0


# --- self-test ----------------------------------------------------------------

def _check(cond, msg):
    if not cond:
        raise SystemExit(f"SELF-TEST FAIL: {msg}")


def _run(home, *args):
    return _cli(["--home", home, *args])


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        yaml = os.path.join(tmp, "matrixkoder.yaml")
        open(yaml, "w").write(
            "agents:\n"
            "  opencode: { available: true, models: [opencode/alpha, opencode/beta] }\n"
            "  agy: { available: false, models: [gemini-x] }\n"
            "  claude: { available: true }\n")
        jev = os.path.join(tmp, "models.md")
        open(jev, "w").write(
            "## Codex (official)\n\n| Model | Jev note |\n|---|---|\n| `gpt-6-luna` | fast |\n")
        first = _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        second = _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        _check(first["models"] == second["models"], f"seed not idempotent {first} {second}")
        shown = _run(home, "show", "opencode", "opencode/alpha")
        _check(shown["model"]["status"] == "picked", f"show {shown}")
        _run(home, "set", "opencode", "opencode/alpha", "notes", '"hand edited"')
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        shown = _run(home, "show", "opencode", "opencode/alpha")
        _check(shown["model"]["notes"] == "hand edited", f"seed overwrote a set field: {shown['model']['notes']}")
        for bad in (('{"todo_hook":"maybe"}', "capabilities_json", "opencode"), ("[", "notes", "opencode")):
            try:
                _run(home, "set", bad[2], bad[1], bad[0])
            except Fail:
                pass
            else:
                raise SystemExit(f"SELF-TEST FAIL: set accepted {bad}")
        got = routes(home)["routes"]
        expect = [f"opencode opencode/{m} {e}" for m in ("alpha", "beta") for e in ("low", "medium", "high")]
        _check(got == expect, f"routes {got} != {expect}")
        open(yaml, "w").write(
            "agents:\n"
            "  opencode: { available: false, models: [opencode/alpha] }\n"
            "  claude: { available: true, models: [claude-sonnet] }\n")
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        got = routes(home)["routes"]
        _check(all(not r.startswith("opencode ") for r in got), f"disabled agent still routed: {got}")
        _check(any(r.startswith("claude claude-sonnet ") for r in got), f"added agent missing: {got}")
        footer = "builder  manifest/auto  12k ctx\n"
        _check(pane_model("pane-1", footer)["model"] == "manifest/auto", "pane footer")
        try:
            pane_model("pane-1", "no model here")
        except Fail:
            pass
        else:
            raise SystemExit("SELF-TEST FAIL: a footer without a model was accepted")
        here = os.path.abspath(__file__)
        with open(here) as fh:
            bad = [ln for ln in fh if "sqlite3" in ln and "connect" in ln and "mkdb" not in ln
                   and not ln.lstrip().startswith("_check")]
            _check(bad == [], f"this script opens a database itself: {bad}")
    print(json.dumps({"ok": True, "self_test": "pass"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
