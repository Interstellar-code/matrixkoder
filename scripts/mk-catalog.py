#!/usr/bin/env python3
"""MatrixKoder agent/model catalog — the C8 tables in the root store. Python 3 stdlib only.

  seed [--yaml PATH] [--jev PATH] [--no-probe]
  list [--json]
  show <agent> [<model>]
  set <agent> [<model>] <field> <json>
  routes --json
  model --pane <pane> [--text TEXT]
  --self-test

Common: --home DIR (default $MK_HOME or $MATRIXKODER_HOME or ~/.matrixkoder). Every command prints ONE JSON line.
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
CREATE TABLE IF NOT EXISTS catalog_user(
  agent TEXT, model TEXT, field TEXT, PRIMARY KEY(agent, model, field));
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


def store_path(home):
    """The root store file mkdb.open_root would create. Reads check this before opening."""
    return os.path.join(home, "mk.db")


def open_catalog(home):
    """The root connection from mkdb. The C8 tables are created on it, idempotently.

    mkdb's migration list only applies versions up to its own SCHEMA, so a catalog migration
    passed in would be skipped. The connection is still one mkdb opened; this script never opens a database itself.
    Writers (seed, set) call this and may create the file. Read commands must not: they call
    read_catalog, which returns None when the store is absent.
    """
    con = mkdb.open_root(home)
    con.executescript(CATALOG_DDL)
    _migrate_user_markers(con)
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


def _migrate_user_markers(con):
    """Move a `_user` marker left in agents.notes by round 1 into catalog_user, then drop it."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='agents'").fetchone():
        return
    for row in con.execute("SELECT id, notes FROM agents"):
        bag = _load(row["notes"], None)
        if not isinstance(bag, dict) or "_user" not in bag:
            continue
        for field in bag["_user"]:
            con.execute("INSERT OR IGNORE INTO catalog_user(agent, model, field) VALUES (?,?,?)",
                        (row["id"], "", field))
        bag.pop("_user")
        con.execute("UPDATE agents SET notes=? WHERE id=?", ("" if not bag else _dump(bag), row["id"]))


def _user_fields(con, agent, model=None):
    """Fields a user set, which seed must not touch. The record lives in catalog_user, never a C8 column."""
    key = "" if model is None else model
    return {r["field"] for r in con.execute(
        "SELECT field FROM catalog_user WHERE agent=? AND model=?", (agent, key))}


def _mark_user(con, agent, model, field):
    con.execute("INSERT OR IGNORE INTO catalog_user(agent, model, field) VALUES (?,?,?)",
                (agent, "" if model is None else model, field))


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


def display_of(model, known=None):
    """A display name: a known one, else the whole id, title-cased.

    `provider/model` becomes both parts joined by a space, and `-` and `_` become spaces:
    manifest/auto -> Manifest Auto, zai-coding-plan/glm-5.2 -> Zai Coding Plan Glm 5.2.
    """
    if known:
        return known
    words = model.replace("/", " ").replace("-", " ").replace("_", " ").split()
    return " ".join(w.capitalize() for w in words)


def upsert_model(con, agent, model, efforts, provider, status, protected, display=None):
    have = con.execute("SELECT * FROM models WHERE agent=? AND id=?", (agent, model)).fetchone()
    if not have:
        con.execute("INSERT INTO models(agent, id, provider, display, context_tokens, effort_levels_json,"
                    " benchmarks_json, cost_json, quota_pool, status, notes, updated, capabilities_json,"
                    " user_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (agent, model, provider, display_of(model, display), None, _dump(efforts), _dump({}),
                     _dump({}), None, status, "", now(), None, _dump([])))
        return
    sets = {"updated": now()}
    if "provider" not in protected:
        sets["provider"] = provider
    if "status" not in protected:
        sets["status"] = status
    if "effort_levels_json" not in protected:
        sets["effort_levels_json"] = _dump(efforts)
    if "display" not in protected and display:
        sets["display"] = display
    cols = ", ".join(f"{k}=?" for k in sets)
    con.execute(f"UPDATE models SET {cols} WHERE agent=? AND id=?", (*sets.values(), agent, model))


def known_displays():
    """Display names from an opencode config when one is named, else {}.

    OPENCODE_CONFIG points the test at a fixture; without it the user's own config is read, and a
    self-test sets the variable empty so it never touches ~/.config/opencode.
    """
    if "OPENCODE_CONFIG" in os.environ:
        path = os.environ["OPENCODE_CONFIG"]
    else:
        path = os.path.expanduser("~/.config/opencode/opencode.json")
    try:
        data = json.load(open(path))
    except Exception:
        return {}
    out = {}
    for provider in (data.get("provider") or {}).values():
        for mid, info in (provider.get("models") or {}).items():
            name = info.get("name") if isinstance(info, dict) else None
            if name:
                out[mid] = name
    return out


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


MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
HEADER_WORDS = {"model", "models", "id", "name", "alias", "jev note"}


def _model_id(cell):
    """A cell that looks like a model id, or None. Header words and anything with a space are not."""
    cell = cell.strip().strip("`")
    if cell.lower() in HEADER_WORDS or not MODEL_ID_RE.match(cell):
        return None
    return cell


def jev_models(path):
    """(agent, model id) pairs from the Jev catalog markdown, or [] when the file is absent.

    Header rows and separator rows are skipped, and only cells that look like a model id are kept,
    so a column titled `Model` never becomes a model.
    """
    if not path or not os.path.isfile(path):
        return []
    section, found = "", []
    for line in open(path):
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue
        if all(c.strip("`").strip().lower() in HEADER_WORDS for c in cells if c.strip()):
            continue
        agent = ("claude" if section.startswith("claude") else "codex" if section.startswith("codex")
                 else "agy" if section.startswith("agy") else "")
        if not agent:
            continue
        # only the first cell names the model; later cells are notes and must not become ids
        for part in re.findall(r"`([^`]+)`", cells[0]) or [cells[0]]:
            mid = _model_id(part.split(" / ")[0])
            if mid and (agent, mid) not in found:
                found.append((agent, mid))
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
                displays = known_displays()
                for model in list(dict.fromkeys(picked + probed)):
                    upsert_model(con, name, model, seed_row["efforts"], seed_row["kind"],
                                 "picked" if model in picked else "probed",
                                 _user_fields(con, name, model), displays.get(model))
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


def read_catalog(home):
    """Open the catalog for a read, or None when <home>/mk.db does not exist yet.

    Checked before open_catalog so a read never creates the root store. Writers keep calling
    open_catalog directly.
    """
    if not os.path.isfile(store_path(home)):
        return None
    return open_catalog(home)


def cmd_list(home):
    con = read_catalog(home)
    if con is None:
        return {"ok": True, "agents": [], "models": []}
    try:
        agents = [dict(r) for r in con.execute("SELECT * FROM agents ORDER BY id")]
        models = [dict(r) for r in con.execute("SELECT * FROM models ORDER BY agent, id")]
    finally:
        con.close()
    return {"ok": True, "agents": agents, "models": models}


def cmd_show(home, agent, model):
    con = read_catalog(home)
    if con is None:
        raise Fail(f"no agent {agent!r}")
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


# Codex is never a builder. `routes` is the builder-pick list (Settings reviewer dropdowns keep a
# hand-saved codex route through mk-config's stale-value fallback; they do not read this filter).
NOT_A_BUILDER = frozenset({"codex"})


def routes(home):
    """Every runnable builder route: enabled agents x picked-or-probed models x supported efforts.

    Codex is omitted even when the agent is enabled: it is not a builder route.
    """
    con = read_catalog(home)
    if con is None:
        return {"ok": True, "routes": []}
    try:
        out = []
        for agent in con.execute("SELECT id FROM agents WHERE status='enabled' ORDER BY id"):
            if str(agent["id"]).strip().casefold() in NOT_A_BUILDER:
                continue
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


def _footer_text(pane, text):
    if text is not None:
        return text
    try:
        proc = subprocess.run(["herdr", "pane", "read", pane], capture_output=True, text=True, timeout=20)
    except Exception as e:
        raise Fail(f"herdr pane read failed: {e}")
    if proc.returncode != 0:
        raise Fail(f"herdr pane read exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def match_footer(con, text):
    """The catalog model a pane footer shows: longest id or display match, effort if shown."""
    low = (text or "").lower()
    best = None
    for row in con.execute("SELECT agent, id, display, effort_levels_json FROM models"):
        for needle in (row["id"], row["display"]):
            if not needle:
                continue
            # whole token, and a real id or display: a bare word (no digit or separator) is never a
            # model, so "model" does not match inside "no model here" or "models"
            if " " not in needle and not re.search(r"[0-9./_:-]", needle):
                continue
            if not re.search(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])", text or "", re.I):
                continue
            if best is None or len(needle) > len(best[0]):
                best = (needle, row)
    if best is None:
        hits = FOOTER_RE.findall(text or "")
        if not hits:
            return None
        return {"model": hits[-1], "display": None, "effort": None}
    needle, row = best
    efforts = _load(row["effort_levels_json"], [])
    effort = next((e for e in sorted(efforts, key=len, reverse=True) if re.search(r"\b" + re.escape(e) + r"\b", low)), None)
    return {"agent": row["agent"], "model": row["id"], "display": row["display"], "effort": effort}


def pane_model(pane, text=None, home=None):
    """The model in a herdr pane footer, matched against the catalog. --text feeds the footer."""
    text = _footer_text(pane, text)
    con = read_catalog(home) if home else None
    try:
        found = match_footer(con, text) if con else None
    finally:
        if con:
            con.close()
    if not found:
        hits = FOOTER_RE.findall(text or "")
        if not hits:
            raise Fail(f"no known model in pane {pane!r} footer")
        found = {"model": hits[-1]}
    found.update(ok=True, pane=pane)
    return found


def _cli(argv):
    p = argparse.ArgumentParser(prog="mk-catalog.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home")
    sub = p.add_subparsers(dest="cmd")
    sd = sub.add_parser("seed")
    sd.add_argument("--yaml")
    sd.add_argument("--jev")
    sd.add_argument("--no-probe", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
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
        return pane_model(args.pane, args.text, home)
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


def _real_jev_check():
    """Finding 1 against the real default Jev file, when this machine has one. Skips otherwise."""
    if not os.path.isfile(JEV_DEFAULT):
        print(json.dumps({"ok": True, "real_jev": "skipped", "reason": "no default Jev file"}))
        return
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        seed(home, None, JEV_DEFAULT, probe=False)
        con = open_catalog(home)
        try:
            bad = [r["id"] for r in con.execute(
                "SELECT id FROM models WHERE id IN ('Model','Models','ID','Name')")]
        finally:
            con.close()
        _check(bad == [], f"real Jev file seeded header words: {bad}")


def self_test():
    os.environ["OPENCODE_CONFIG"] = ""  # never read this machine's ~/.config/opencode
    _real_jev_check()
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        saved = {k: os.environ.get(k) for k in ("MK_HOME", "MATRIXKODER_HOME", "MK_SELFTEST")}
        os.environ["MK_HOME"] = home
        os.environ["MATRIXKODER_HOME"] = home
        os.environ["MK_SELFTEST"] = "1"
        yaml = os.path.join(tmp, "matrixkoder.yaml")
        open(yaml, "w").write(
            "agents:\n"
            "  opencode: { available: true, models: [opencode/alpha, opencode/beta] }\n"
            "  agy: { available: false, models: [gemini-x] }\n"
            "  claude: { available: true }\n")
        jev = os.path.join(tmp, "models.md")
        open(jev, "w").write(
            "## Codex (official)\n\n| Model | Jev note |\n|---|---|\n| `gpt-6-luna` | fast |\n"
            "## AGY\n\n| Model id | Jev note |\n|---|---|\n| `gemini-3.8-flash-medium` | bulk |\n")
        ids = [m for _a, m in jev_models(jev)]
        _check("Model" not in ids and "gpt-6-luna" in ids and "gemini-3.8-flash-medium" in ids,
               f"jev parser kept a header: {ids}")
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
        # codex enabled is still not a builder route
        open(yaml, "w").write(
            "agents:\n"
            "  codex: { available: true, models: [gpt-6-luna] }\n"
            "  opencode: { available: true, models: [opencode/alpha] }\n")
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        got = routes(home)["routes"]
        _check(got and all(not r.startswith("codex ") for r in got), f"codex builder route: {got}")
        _check(any(r.startswith("opencode ") for r in got), f"opencode dropped: {got}")
        _run(home, "set", "opencode", "notes", '"my custom note"')
        shown = _run(home, "show", "opencode")
        _check(shown["agent"]["notes"] == "my custom note", f"notes destroyed: {shown['agent']['notes']}")
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        shown = _run(home, "show", "opencode")
        _check(shown["agent"]["notes"] == "my custom note", "seed overwrote agent notes")
        _run(home, "set", "opencode", "status", '"disabled"')
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        shown = _run(home, "show", "opencode")
        _check(shown["agent"]["status"] == "disabled", "seed overwrote a set status")
        listed = _run(home, "list", "--json")
        _check(listed["ok"] and listed["agents"], "list --json rejected")
        footers = {
            "Build · Manifest Auto Manifest · high": ("manifest/auto", "high"),
            "▣ Sisyphus - Ultraworker · Manifest Auto": ("manifest/auto", None),
            "gemini-3.8-flash-medium  12k ctx": ("gemini-3.8-flash-medium", None),
            "claude-sonnet-5-5 · medium": ("claude-sonnet-5-5", "medium"),
        }
        # seeded, not hand-inserted: the derived display of manifest/auto must be "Manifest Auto"
        open(yaml, "w").write(
            "agents:\n"
            "  opencode: { available: true, models: [manifest/auto, opencode/alpha] }\n"
            "  agy: { available: true, models: [gemini-3.8-flash-medium] }\n"
            "  claude: { available: true, models: [claude-sonnet-5-5] }\n")
        _run(home, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        shown = _run(home, "show", "opencode", "manifest/auto")
        _check(shown["model"]["display"] == "Manifest Auto", f"derived display {shown['model']['display']}")
        for footer, (model, effort) in footers.items():
            got = pane_model("pane-1", footer, home)
            _check(got["model"] == model and got.get("effort") == effort, f"footer {footer!r} -> {got}")
        # a model whose id is a bare word must not match inside "no model here" or "models"
        con = open_catalog(home)
        try:
            upsert_model(con, "codex", "model", [], "codex", "catalog", set())
            con.commit()
        finally:
            con.close()
        for text in ("no model here", "models"):
            try:
                pane_model("pane-1", text, home)
            except Fail:
                pass
            else:
                raise SystemExit(f"SELF-TEST FAIL: {text!r} matched a model")
        try:
            pane_model("pane-1", "no model here", home)
        except Fail as e:
            _check("no known model" in str(e), f"unclear error: {e}")
        else:
            raise SystemExit("SELF-TEST FAIL: a footer without a model was accepted")
        here = os.path.abspath(__file__)
        with open(here) as fh:
            bad = [ln for ln in fh if "sqlite3" in ln and "connect" in ln and "mkdb" not in ln
                   and not ln.lstrip().startswith("_check")]
            _check(bad == [], f"this script opens a database itself: {bad}")
    # a read against an empty home must not create mk.db (todo 49)
    with tempfile.TemporaryDirectory() as empty:
        before = sorted(os.listdir(empty))
        listed = _run(empty, "list", "--json")
        _check(listed == {"ok": True, "agents": [], "models": []}, f"empty list {listed}")
        _check(sorted(os.listdir(empty)) == before, f"list created {os.listdir(empty)}")
        routed = _run(empty, "routes", "--json")
        _check(routed == {"ok": True, "routes": []}, f"empty routes {routed}")
        _check(os.listdir(empty) == [], f"routes created {os.listdir(empty)}")
        try:
            _run(empty, "show", "opencode")
        except Fail as e:
            _check("no agent" in str(e), f"empty show {e}")
        else:
            raise SystemExit("SELF-TEST FAIL: show on an empty home succeeded")
        _check(os.listdir(empty) == [], f"show created {os.listdir(empty)}")
        try:
            _run(empty, "model", "--pane", "pane-1", "--text", "no model here")
        except Fail as e:
            _check("no known model" in str(e), f"empty model {e}")
        else:
            raise SystemExit("SELF-TEST FAIL: model on an empty home succeeded")
        _check(os.listdir(empty) == [], f"model created {os.listdir(empty)}")
        # writers still create the store; fixtures live beside the home, not inside it
        side = tempfile.mkdtemp(dir=os.path.dirname(empty))
        yaml = os.path.join(side, "matrixkoder.yaml")
        open(yaml, "w").write("agents:\n  opencode: { available: true, models: [opencode/alpha] }\n")
        jev = os.path.join(side, "models.md")
        open(jev, "w").write("## Codex (official)\n\n| Model | Jev note |\n|---|---|\n| `gpt-6-luna` | fast |\n")
        seeded = _run(empty, "seed", "--yaml", yaml, "--jev", jev, "--no-probe")
        _check(os.path.isfile(store_path(empty)), "seed did not create the store")
        _check(seeded["ok"] is True and seeded["models"] >= 1, f"seed {seeded}")
        shown = _run(empty, "show", "opencode", "opencode/alpha")
        _check(shown["ok"] and shown["model"]["status"] == "picked", f"seeded show {shown}")
        got = _run(empty, "routes", "--json")["routes"]
        _check(got == [f"opencode opencode/alpha {e}" for e in ("low", "medium", "high")], f"seeded routes {got}")
        # R1.2: the mkdb write guard refuses the real home from this suite too.
        mkdb.selftest_guard_probe(home)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(json.dumps({"ok": True, "self_test": "pass"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main(sys.argv[1:]))
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(e.code)
