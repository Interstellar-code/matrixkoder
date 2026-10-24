#!/usr/bin/env python3
"""Self-tests for mk-plan.py."""
import datetime, glob, hashlib, importlib.util, json, os, re, shutil, sqlite3, stat, subprocess, sys, tempfile, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
MK_PLAN_PY = os.path.join(HERE, 'mk-plan.py')

_spec = importlib.util.spec_from_file_location('mk_plan', MK_PLAN_PY)
mk_plan = importlib.util.module_from_spec(_spec)
sys.modules['mk_plan'] = mk_plan
_spec.loader.exec_module(mk_plan)

BODY_SECTIONS = mk_plan.BODY_SECTIONS
HEADER_PARTS = mk_plan.HEADER_PARTS
PURGE_BY_RUN = mk_plan.PURGE_BY_RUN
RESERVED_PLAN_ERR = mk_plan.RESERVED_PLAN_ERR
TPL_ADVICE_RE = mk_plan.TPL_ADVICE_RE
_mkdb = mk_plan._mkdb
f2_approve_self_test = mk_plan.f2_approve_self_test
f2_check_self_test = mk_plan.f2_check_self_test
header_json_of = mk_plan.header_json_of
store_self_test = mk_plan.store_self_test
mk_config = mk_plan.mk_config
mkpaths = mk_plan.mkpaths

def self_test():
    import shutil, sqlite3, subprocess
    tmp = tempfile.mkdtemp(prefix="mk-plan-test-")
    # every parse reads session.subagents, which resolves the data dir: pin it here so the run can
    # never read the main checkout's .matrixkoder/ (and fail if it would)
    restore_data = mk_config.pin_tmp_data(tmp)
    root = os.path.dirname(os.path.dirname(MK_PLAN_PY))
    real_plan = os.path.join(root, "scripts", "fixtures", "plan-link.md")  # a real plan, kept as a fixture
    template = os.path.join(root, "skills", "matrixkoder", "templates", "plan.md")

    def mk(*args, code=0):
        r = subprocess.run([sys.executable, MK_PLAN_PY, *args],
                           capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, (args, r.stdout, r.stderr)
        assert r.returncode == code, (args, r.returncode, r.stdout, r.stderr)
        return json.loads(lines[0])

    def write(name, text):
        p = os.path.join(tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def plan_text(phases="", status='status = "draft"', runs="runs = []", plan='plan = "t1"',
                  top="", head_extra=""):
        return (f"+++\n{plan}\n{status}\ncreated = 2026-10-08\n{runs}\n{top}\n"
                f"[gates]\ntest = \"true\"\n{head_extra}\n{phases}+++\n\n# body\nmarkdown stays\n")

    def phase(pid, wave=1, deps="", owned="", route="", size="standard", risk="reversible",
              review="standard", subagents=False, lane_group="", extra="", tasks=""):
        ow = f'owned = [{owned}]' if owned else "owned = []"
        rt = f'route = "{route}"' if route else 'route = ""'
        lg = f'lane_group = "{lane_group}"' if lane_group else 'lane_group = ""'
        sa = "true" if subagents else "false"
        dp = f"deps = [{deps}]" if deps else "deps = []"
        return (f'[[phase]]\nid = "{pid}"\ntitle = "phase {pid}"\nwave = {wave}\n{dp}\n{ow}\n'
                f'forbidden = []\n{rt}\nsize = "{size}"\nrisk = "{risk}"\nreview = "{review}"\n'
                f"qa = []\ncommit = \"\"\nsubagents = {sa}\n{lg}\n{extra}{tasks}")

    def task(tid, title="t", acceptance="a", refs="", subagent=False, owned="", extra=""):
        sa = "true" if subagent else "false"
        rf = f'refs = [{refs}]' if refs else "refs = []"
        ow = f'owned = [{owned}]' if owned else "owned = []"
        return (f"  [[phase.task]]\n  id = \"{tid}\"\n  title = \"{title}\"\n"
                f"  acceptance = \"{acceptance}\"\n  {rf}\n  subagent = {sa}\n  {ow}\n{extra}")

    def f2_warnings(warnings):
        """F.2: the two advisory warnings a pre-F plan now carries (no plan-level final gate, an
        item with neither gate nor manual). The assertions below compare against the warnings
        that are NOT these, so an old plan still has to be otherwise quiet."""
        # TPL.2 added advisories (missing description, Summary/Goal, no acceptance, a line no gate
        # proves, a missing required key on an old plan) are filtered the same way: an old plan
        # must still be otherwise quiet
        return [w for w in warnings if "no final_gate in the plan" not in w
                and not w.endswith(": neither gate nor manual line")
                and not TPL_ADVICE_RE.search(w)]

    try:
        # P1.1 parse — the two real files (read-only) must give the C2 JSON
        out = mk("parse", template)
        with open(template, "rb") as f:
            assert out["ok"] and out["sha"] == hashlib.sha256(f.read()).hexdigest()
        assert out["plan"] == "feature-id" and out["status"] == "draft" and out["runs"] == []
        assert set(out["gates"]) == {"test", "typecheck"}
        p0 = out["phases"][0]
        assert p0["id"] == "P1" and p0["wave"] == 1 and p0["lane_group"] == "" and p0["subagents"] is True  # template default (user 2026-10-09)
        assert p0["reviewer"] == ""  # template's reviewer example passes through
        assert p0["tasks"][0]["id"] == "P1.1" and p0["tasks"][0]["subagent"] is False
        assert out["created"] == "2026-10-24"  # new template created date

        out = mk("parse", real_plan)
        assert out["ok"] and out["plan"] == "plan-link" and out["status"] == "approved"
        assert [p["id"] for p in out["phases"]] == ["P1", "P2", "P3"]
        assert [t["id"] for t in out["phases"][0]["tasks"]] == ["P1.1", "P1.2", "P1.3"]
        assert all(p["wave"] == 1 and p["lane_group"] == "A" and p["deps"] == [] for p in out["phases"])
        assert all(p["reviewer"] == "" for p in out["phases"])  # optional key defaults to ""
        assert out["phases"][0]["review"] == "data-loss" and out["phases"][0]["size"] == "standard"

        # P1.2 check — both real files are clean (ok:true, exit 0): neither carries a real
        # problem. F.2 adds the two advisories a plan written before F earns (no final gate, an
        # item with neither gate nor manual), and the shipped template carries the F.2 fields, so
        # only the fixture plan-link.md — which is NOT part of this change and stays as it is —
        # is allowed to carry them.
        out = mk("check", template)
        assert out["ok"] and f2_warnings(out["warnings"]) == [] and out["sha"], out.get("warnings")
        out = mk("check", real_plan)
        assert out["ok"] and out["sha"] and f2_warnings(out["warnings"]) == [], out["plan"]
        assert any("no final_gate in the plan" in w for w in out["warnings"])  # the F.2 advisory

        # a phase without its own subagents key inherits session.subagents (yaml), else the
        # schema default true; a phase that sets it keeps its own value
        def parse_env(p, datadir):
            r = subprocess.run([sys.executable, MK_PLAN_PY, "parse", p],
                               capture_output=True, text=True,
                               env=dict(os.environ, MATRIXKODER_DATA=datadir))
            lines = [x for x in r.stdout.strip().splitlines() if x]
            assert r.returncode == 0 and len(lines) == 1, (p, r.stdout, r.stderr)
            return json.loads(lines[0])

        nokey = ('[[phase]]\nid = "P1"\ntitle = "t"\nwave = 1\ndeps = []\nowned = []\n'
                 'forbidden = []\nroute = ""\nsize = "standard"\nrisk = "read_only"\n'
                 'review = "light"\nqa = []\ncommit = ""\n\n')
        dsub, dempty = os.path.join(tmp, "data-sub"), os.path.join(tmp, "data-empty")
        os.makedirs(dsub), os.makedirs(dempty)
        with open(mkpaths.legacy_yaml(dsub), "w") as f:
            f.write("session:\n  subagents: false\n")
        p = write("inherit-nokey.md", plan_text(phases=nokey))
        assert parse_env(p, dsub)["phases"][0]["subagents"] is False  # inherited from the yaml
        assert parse_env(p, dempty)["phases"][0]["subagents"] is True  # the schema default
        p = write("inherit-own.md", plan_text(phases=phase("P1", subagents=True) + "\n"))
        assert parse_env(p, dsub)["phases"][0]["subagents"] is True  # its own key wins

        # a done/parked plan may have no phases
        for st in ("done", "parked"):
            p = write(f"done-{st}.md", plan_text(status=f'status = "{st}"'))
            out = mk("parse", p)
            assert out["ok"] and out["phases"] == []
            assert mk("check", p)["ok"]

        # unknown keys pass through (top, phase, task)
        p = write("unknown.md", plan_text(
            top='extra_top = "y"', phases=phase("P1", extra='foo = "bar"\n',
                                                tasks=task("P1.1", extra="  zip = 1\n")) + "\n"))
        out = mk("parse", p)
        assert out["extra_top"] == "y" and out["phases"][0]["foo"] == "bar"
        assert out["phases"][0]["tasks"][0]["zip"] == 1
        assert mk("check", p)["ok"] is True

        # errors: no leading +++, bad TOML (line number inside the file), missing file
        p = write("noopen.md", 'plan = "x"\n+++\n+++\n')
        out = mk("parse", p, code=2)
        assert out["ok"] is False and out["errors"][0].startswith("line 1:"), out
        mk("check", p, code=2)
        p = write("noclose.md", '+++\nplan = "x"\n')
        assert mk("parse", p, code=2)["errors"][0].startswith("line 3:")
        p = write("badtoml.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated == 2026-10-08\n+++\nbody\n')
        out = mk("parse", p, code=2)
        assert out["ok"] is False and out["errors"][0].startswith("line 4:"), out
        mk("check", os.path.join(tmp, "nope.md"), code=3)

        # CRLF plans parse like their LF twin; set/add-run keep the file's own endings
        p = write("crlf.md", plan_text(phases=phase("P1") + "\n").replace("\n", "\r\n"))
        with open(p, "rb") as f:
            raw = f.read()
        out = mk("parse", p)
        assert out["ok"] and out["sha"] == hashlib.sha256(raw).hexdigest()
        assert mk("check", p)["ok"] is True
        assert mk("set", p, "--status", "approved")["status"] == "approved"
        crlf = open(p, "rb").read()
        assert crlf.replace(b"\r\n", b"\n") == raw.replace(b"\r\n", b"\n").replace(
            b'status = "draft"', b'status = "approved"')
        assert b"\n" not in crlf.replace(b"\r\n", b"")  # every newline still a \r\n
        assert mk("parse", p)["status"] == "approved"
        assert mk("add-run", p, "--run", "r1")["runs"] == ["r1"]
        crlf = open(p, "rb").read()
        assert 'runs = ["r1"]\r' in crlf.decode("utf-8") and b"\n" not in crlf.replace(b"\r\n", b"")

        # the eight P1.2 error rules — each violates exactly one, parse stays ok
        def bad(name, phases, needle):
            p = write(f"{name}.md", plan_text(phases=phases + "\n"))
            assert mk("parse", p)["ok"] is True, name
            out = mk("check", p, code=2)
            assert out["ok"] is False and any(needle in e for e in out["errors"]), (name, out)

        bad("dup", phase("P1") + "\n" + phase("P1"), "duplicate phase id P1")
        bad("dup-task", phase("P1", tasks=task("P1.1") + task("P1.1")), "duplicate task id P1.1")
        bad("unknown-dep", phase("P1", deps='"PX"'), "unknown dep PX")
        bad("cycle", phase("P1", deps='"P2"') + "\n" + phase("P2", wave=2, deps='"P1"'),
            "dep cycle: P1 -> P2 -> P1")
        bad("self-dep", phase("P1", deps='"P1"'), "dep cycle: P1 -> P1")
        bad("later-wave", phase("P1", deps='"P2"') + "\n" + phase("P2", wave=2),
            "dep P2 is in a later wave")
        bad("size", phase("P1", size="huge"), "bad size 'huge'")
        bad("risk", phase("P1", risk="wild"), "bad risk 'wild'")
        bad("review", phase("P1", review="deep"), "bad review 'deep'")
        bad("route-1", phase("P1", route="claude"), "bad route")
        bad("route-4", phase("P1", route="a b c d"), "bad route")
        bad("reviewer", phase("P1", extra='reviewer = 5\n'), "reviewer 5 is not a string")
        bad("overlap", phase("P1", owned='"src/"') + "\n" + phase("P2", owned='"src/x.py"'),
            "P1 and P2 both own")
        bad("sub-share", phase("P1", subagents=True,
                               tasks=task("P1.1", subagent=True, owned='"a.py"')
                               + task("P1.2", subagent=True, owned='"a.py"')),
            "subagent tasks P1.1 and P1.2 share a file")
        bad("sub-no-owned", phase("P1", subagents=True, tasks=task("P1.1", subagent=True)),
            "P1.1: subagent task with no owned")
        bad("lane-waves", phase("P1", lane_group="A") + "\n" + phase("P2", wave=2, lane_group="A"),
            "lane_group A: phases in different waves")

        # reviewer: a string value passes through parse verbatim and checks clean
        p = write("reviewer.md", plan_text(
            phases=phase("P1", extra='reviewer = "opencode opencode-go/kimi-k3"\n') + "\n"))
        out = mk("parse", p)
        assert out["phases"][0]["reviewer"] == "opencode opencode-go/kimi-k3"
        assert f2_warnings(mk("check", p)["warnings"]) == []

        # warnings: advisories never fail the check
        p = write("warn.md", plan_text(
            plan='plan = "Bad_ID"', phases=phase("P1", deps='"r9:T1"',
                                                 tasks=task("X.9")) + "\n"))
        out = mk("check", p)
        assert out["ok"] is True and out["warnings"], out
        assert any("cross-run" in w and "r9:T1" in w for w in out["warnings"])
        assert any("plan id" in w for w in out["warnings"])
        assert any("<phase id>.<n>" in w for w in out["warnings"])
        p = write("warn-keys.md", plan_text(
            top="", phases='[[phase]]\nid = "P1"\ntitle = "t"\nwave = 1\ndeps = []\nowned = []\n'
                           'route = ""\nsize = "standard"\nrisk = "read_only"\nreview = "light"\n'
                           'qa = []\ncommit = ""\nsubagents = false\n\n'))
        out = mk("check", p)
        assert out["ok"] is True and f2_warnings(out["warnings"]) == [], out  # no optional keys: still quiet

        # T1 — the header keys that fill a plans column: a missing one warns, never errors.
        # each case is ok: true; on the pre-T1 code the warnings list is empty and these fail.
        base = phase("P1", tasks=task("P1.1", acceptance="a")) + "\n"

        p = write("no-created.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = []\n'
                                   '[gates]\ntest = "true"\n' + base + '+++\n\n# body\nmarkdown stays\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("created" in w and "plans.created" in w for w in out["warnings"]), out
        assert not any("status" in w for w in out["warnings"]), out  # only the missing one

        p = write("no-status.md", '+++\nplan = "t1"\ncreated = 2026-10-08\nruns = []\n'
                                  '[gates]\ntest = "true"\n' + base + '+++\n\n# body\nmarkdown stays\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("status" in w and "plans.status" in w for w in out["warnings"]), out
        assert not any("created" in w for w in out["warnings"]), out

        p = write("no-h1.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated = 2026-10-08\nruns = []\n'
                               '[gates]\ntest = "true"\n' + base + '+++\n\n## Goal\nbody with no H1\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("H1" in w and "plans.title" in w for w in out["warnings"]), out
        assert not any("status" in w or "created" in w for w in out["warnings"]), out

        # a '#' inside a fenced block is not an H1; the fenced code is not a heading
        p = write("fenced-h1.md", '+++\nplan = "t1"\nstatus = "draft"\ncreated = 2026-10-08\nruns = []\n'
                                  '[gates]\ntest = "true"\n' + base + '+++\n\n```\n# not a heading\n```\n')
        assert any("H1" in w for w in mk("check", p)["warnings"])

        # both keys missing: both warnings, still ok: true (warnings never fail the check)
        p = write("neither.md", '+++\nplan = "t1"\nruns = []\n[gates]\ntest = "true"\n' + base
                               + '+++\n\nbody\n')
        out = mk("check", p)
        assert out["ok"] is True, out
        assert any("created" in w for w in out["warnings"]) and any("status" in w for w in out["warnings"])
        assert any("H1" in w for w in out["warnings"]), out

        # ---------- TPL.2: per-section result, fit class, --classify, date strictness ----------
        sections_md = "".join(f"## {n}\ntext\n\n" for n in BODY_SECTIONS)
        fin = '[[final_gate]]\nid = "F1"\ncmd = "true"\ncovers = ["P1.1"]\n'
        ph_head = ('[[phase]]\nid = "P1"\ntitle = "phase P1"\ndescription = "d"\nwave = 1\n'
                   'owned = []\n{commit}subagents = false\n')
        ph_task = ('  [[phase.task]]\n  id = "P1.1"\n  title = "t"\n  acceptance = ["a"]\n'
                   '  gate = { cmd = "true", expect = 0, timeout = 60, lines = ["P1.1.a1"] }\n')

        def tpl_plan(name, created="2026-10-08", head=ph_head.format(commit='commit = "c"\n'),
                     task_md=ph_task, final=fin, body=None, status='status = "draft"'):
            cr = f"created = {created}\n" if created else ""
            return write(name, f'+++\nplan = "t1"\n{status}\n{cr}runs = []\ndescription = "d"\n{final}\n'
                               f'{head}{task_md}+++\n\n# t1 — title\n\n'
                               f'{sections_md if body is None else body}')

        # template class: all seven sections and four parts found, no warning (TPL.2.a1)
        out = mk("check", tpl_plan("tpl-full.md"))
        assert out["ok"] and out["fit"] == "template" and out["missing"] == [], out
        assert out["sections_found"] == 7 and out["sections_total"] == 7 and out["warnings"] == [], out
        assert set(out["sections"]) == set(BODY_SECTIONS) and set(out["sections"].values()) == {"found"}
        assert set(out["parts"]) == set(HEADER_PARTS) and set(out["parts"].values()) == {"found"}
        # the shipped template and the repo's real plan fixture class by the same rule
        assert mk("check", template)["fit"] == "template"
        assert mk("check", real_plan)["fit"] in ("template", "partial")

        # partial class: a valid header with no task lists tasks and acceptance (TPL.2.a2)
        out = mk("check", tpl_plan("tpl-notask.md", task_md="", final=""))
        assert out["ok"] and out["fit"] == "partial", out
        assert out["missing"] == ["tasks", "acceptance", "final gate"], out
        assert out["parts"]["header phases"] == "found" and out["parts"]["tasks"] == "missing"
        assert out["sections_found"] == 7
        # a missing body section is named and counted (TPL.2.a9)
        out = mk("check", tpl_plan("tpl-norisks.md", body=sections_md.replace("## Risks\ntext\n\n", "")
                                   + "### Risks\nonly a ### heading\n"))
        assert out["fit"] == "partial" and out["missing"] == ["Risks"], out
        assert out["sections_found"] == 6 and out["sections"]["Risks"] == "missing", out
        # "header phases" means [[phase]] blocks, never the Phases body section
        out = mk("check", write("tpl-nophase.md", '+++\nplan = "t1"\nstatus = "draft"\n'
                                                 'created = 2026-10-08\nruns = []\n+++\n\n# t1\n\n'
                                + sections_md))
        assert out["fit"] == "not_a_plan" and out["sections"]["Phases"] == "found", out
        assert out["parts"]["header phases"] == "missing" and out["sections_found"] == 7, out
        # a section heading inside a comment or a fence is not a section
        out = mk("check", tpl_plan("tpl-hidden.md", body=sections_md.replace("## Log\ntext\n\n", "")
                                   + "<!-- ## Log -->\n```\n## Log\n```\n"))
        assert out["missing"] == ["Log"], out

        # not a plan: no +++ first line. --classify returns a class with exit 0; without it the
        # old error and exit code stay (TPL.2.a3)
        np = write("not-a-plan.md", "# just notes\n\n## Summary\nhello\n")
        out = mk("check", np, "--classify")
        assert out["ok"] is True and out["fit"] == "not_a_plan" and out["sections_found"] == 1, out
        assert out["sections_total"] == 7 and "Goal" in out["missing"] and "tasks" in out["missing"], out
        out = mk("check", np, code=2)
        assert out["ok"] is False and out["errors"] == ["line 1: first line must be '+++'"], out
        assert "fit" not in out
        mk("check", os.path.join(tmp, "no-such-plan.md"), "--classify", code=3)  # a missing file stays an error

        # date strictness: the same phase without commit is an error for a new plan and a warning
        # for an older or undated one (TPL.2.a4, a5)
        nocommit = ph_head.format(commit="")
        out = mk("check", tpl_plan("tpl-new.md", created="2026-10-20", head=nocommit), code=2)
        assert out["errors"] == ["P1: no commit key (required in a plan created on or after 2026-10-20)"], out
        assert "fit" not in out  # an error reply carries the class only under --classify
        out = mk("check", tpl_plan("tpl-new2.md", created="2026-10-20", head=nocommit), "--classify", code=2)
        assert out["fit"] == "template" and out["missing"] == [], out  # the class rides on the error reply
        for name, created in (("tpl-old.md", "2026-10-19"), ("tpl-undated.md", "")):
            out = mk("check", tpl_plan(name, created=created, head=nocommit))
            assert out["ok"] and any("P1: no commit key" in w for w in out["warnings"]), out
        out = mk("check", tpl_plan("tpl-notitle.md", created="2026-10-21",
                                   task_md=ph_task.replace('  title = "t"\n', "")), code=2)
        assert any("P1.1: no title key" in e for e in out["errors"]), out
        out = mk("check", tpl_plan("tpl-notitle-old.md", task_md=ph_task.replace('  title = "t"\n', "")))
        assert out["ok"] and any("P1.1: no title key" in w for w in out["warnings"]), out
        # a task with no acceptance / a line no gate proves is a warning for a new plan too
        out = mk("check", tpl_plan("tpl-noacc.md", created="2026-10-21",
                                   task_md=ph_task.replace('  acceptance = ["a"]\n', "")
                                   .replace(', lines = ["P1.1.a1"]', "")))
        assert out["ok"] and "P1.1: no acceptance" in out["warnings"], out
        out = mk("check", tpl_plan("tpl-loose.md", created="2026-10-21",
                                   task_md=ph_task.replace('acceptance = ["a"]', 'acceptance = ["a", "b"]')))
        assert out["ok"] and "P1.1: acceptance line(s) P1.1.a2 proved by no gate" in out["warnings"], out
        # a missing description is only ever a warning, at the top and phase level
        out = mk("check", write("tpl-nodesc.md", open(os.path.join(tmp, "tpl-full.md")).read()
                                .replace('description = "d"\n', "")), )
        assert out["ok"] and "no description key in the header; advised: two to five plain sentences " \
            "(plans.header_json only)" in out["warnings"], out
        assert "P1: no description; advised: two to five plain sentences" in out["warnings"], out
        # description at the top, phase and task level: accepted, passed through, no warning (TPL.1.a4).
        # A regression guard: the old check never warned on an unknown key, so this passes on the old code too
        dp = tpl_plan("tpl-desc.md", created="2026-10-21",
                      task_md=ph_task + '  description = "what the task is"\n')
        out = mk("check", dp)
        assert out["ok"] and not [w for w in out["warnings"] if "description" in w], out
        parsed = mk("parse", dp)
        assert parsed["description"] == "d" and parsed["phases"][0]["description"] == "d"
        assert parsed["phases"][0]["tasks"][0]["description"] == "what the task is"

        # Summary drift warns, never errors (TPL.2.a7); matching lines are quiet
        summ = ("## Summary\n- Phases: 1 — P1\n- Method: single lane · subagents off\n"
                "- Lanes: P1 is its own lane\n- Delivery: main · pushed: no\n- Gates: F1 = true\n\n")
        quiet = sections_md.replace("## Summary\ntext\n\n", summ)
        out = mk("check", tpl_plan("tpl-sum-ok.md", created="2026-10-21", body=quiet))
        assert out["ok"] and out["warnings"] == [], out
        drift = quiet.replace("Phases: 1", "Phases: 3").replace("single lane · subagents off", "waves · subagents on") \
            .replace("F1 = true", "G9 = other")
        out = mk("check", tpl_plan("tpl-sum-drift.md", created="2026-10-21", body=drift))
        assert out["ok"] is True, out
        assert "Summary says 3 phases; the header has 1" in out["warnings"], out
        assert any(w.startswith("Summary Method says waves · subagents on") for w in out["warnings"]), out
        assert "Summary Gates does not list final gate F1 as id = cmd" in out["warnings"], out
        out = mk("check", tpl_plan("tpl-sum-lane.md", created="2026-10-21", body=quiet.replace(
            "P1 is its own lane", "each phase alone"), head=ph_head.format(commit='commit = "c"\n')
            + 'lane_group = "A"\n'))
        assert "Summary Lanes does not name lane_group A" in out["warnings"], out
        # a missing Summary or Goal is a warning, not an error
        out = mk("check", tpl_plan("tpl-nosum.md", created="2026-10-21", body=sections_md.replace(
            "## Summary\ntext\n\n", "").replace("## Goal\ntext\n\n", "")))
        assert out["ok"] and "body has no '## Summary' section (see templates/plan.md)" in out["warnings"], out
        assert "body has no '## Goal' section (see templates/plan.md)" in out["warnings"], out

        # approve warns "N unticked open questions" and still approves; check never errors on them (a8)
        oq = sections_md.replace("## Open questions\ntext\n", "## Open questions\n- [ ] one — decides a\n"
                                 "- [x] two — decided\n- [ ] three — decides b\n")
        p = tpl_plan("tpl-oq.md", body=oq)
        assert mk("check", p)["ok"] is True and not any("unticked" in w for w in mk("check", p)["warnings"])
        tdata, thome = os.path.join(tmp, "tpl-data"), os.path.join(tmp, "tpl-home")
        out = mk("approve", p, "--data", tdata, "--home", thome)
        assert out["ok"] and out["status"] == "approved" and "2 unticked open questions" in out["warnings"], out
        p = tpl_plan("tpl-oq0.md", body=oq.replace("- [ ] ", "- [x] "))
        out = mk("approve", p, "--data", tdata, "--home", thome)
        assert out["ok"] and not any("unticked" in w for w in out["warnings"]), out

        # regression guard (TPL.2.a6, passes on the old code too): the old errors and exit code are
        # unchanged by the new fields
        out = mk("check", write("tpl-guard.md", plan_text(phases=phase("P1", route="opencode") + "\n")), code=2)
        assert out["ok"] is False and any("bad route" in e for e in out["errors"]), out
        assert "fit" not in out and "sections" not in out

        # the shipped template is the contract: it names every key's home and checks with
        # no new warning (the whole set above is empty for it)
        out = mk("check", template)
        assert out["ok"] and f2_warnings(out["warnings"]) == [], out
        with open(template, encoding="utf-8") as fh:
            tmpl = fh.read()
        assert tmpl.startswith("+++ 1 Top block")
        for needle in ("+++ 1 Top block", "+++ 2 Summary", "+++ 3 Goal", "+++ 4 Design / context",
                       "+++ 5 Phases", "+++ 6 Gates", "+++ 7 Risks", "+++ 8 Open questions",
                       "+++ 9 Log", "covers", "Serves:", "proves"):
            assert needle in tmpl, needle
        p0 = out["phases"][0]
        assert p0["tasks"][0]["gate"] and p0["tasks"][0]["acceptance_lines"] \
            and len(p0["tasks"][0]["acceptance_lines"]) == 2, p0["tasks"][0]
        assert out.get("use_gates") or (out.get("final_gates") and out["final_gates"][0]["covers"]), out
        if out.get("use_gates"):
            assert [g["id"] for g in out["use_gates"]] == ["test", "typecheck"]

        # F0-BLOCK-BEGIN — F.0: a table gate is runnable, a plain string gate stays manual
        p = write("gate-table.md", plan_text(head_extra=(
            '\n[gates.slow]\ncmd = "sleep 5"\ntimeout = 1\n'
            '\n[gates.strict]\ncmd = "true"\nexpect = 3\n')))
        out = mk("parse", p)
        assert out["gates"]["test"] == "true", out  # a plain string gate stays a string (manual)
        assert out["gates"]["slow"] == {"cmd": "sleep 5", "expect": 0, "timeout": 1}, out
        assert out["gates"]["strict"] == {"cmd": "true", "expect": 3, "timeout": 600}, out
        assert mk("check", p)["ok"] is True, "a well-formed table gate must check clean"

        # check rejects a table gate without cmd, and a non-int expect / timeout
        for name, body, why in (("gate-nocmd.md", '[gates.x]\nexpect = 1\n', "needs cmd"),
                                ("gate-badexpect.md", '[gates.x]\ncmd = "true"\nexpect = "zero"\n',
                                 "expect must be an int"),
                                ("gate-badtimeout.md", '[gates.x]\ncmd = "true"\ntimeout = "fast"\n',
                                 "timeout must be an int")):
            p = write(name, plan_text(head_extra="\n" + body))
            e = mk("check", p, code=2)
            assert e["ok"] is False, e
            assert any("gate x" in x and why in x for x in e["errors"]), (name, e)
        # F0-BLOCK-END

        # F.2 — the plan contract: acceptance lines, per-task gate OR manual, critical/risk,
        # header [[final_gate]] with covers, and the frozen-gate rules in approve.
        f2_check_self_test(tmp, mk, write, plan_text, phase, task)

        # P1.3 set — body byte-identical, only the status line changes
        import shutil as sh
        target = os.path.join(tmp, "set-target.md")
        sh.copy(real_plan, target)
        orig = open(target, "rb").read()
        out = mk("set", target, "--status", "running")
        assert out["ok"] and out["status"] == "running"
        new = open(target, "rb").read()
        ol, nl = orig.decode().split("\n"), new.decode().split("\n")
        assert ol[2] == 'status = "approved"' and nl[2] == 'status = "running"'
        assert "\n".join(ol[3:]) == "\n".join(nl[3:])  # body byte-identical
        assert [l for i, l in enumerate(ol) if i != 2] == [l for i, l in enumerate(nl) if i != 2]
        assert mk("parse", target)["status"] == "running"
        assert os.listdir(tmp).count("set-target.md") == 1  # atomic: no temp files left
        assert not [f for f in os.listdir(tmp) if f.startswith(".tmp-")]
        mk("set", target, "--status", "bogus", code=2)
        assert open(target, "rb").read() == new  # refused write changed nothing
        p = write("set-comment.md", '+++\nplan = "t1"\nstatus = "draft"  # keep me\nruns = []\n+++\nbody\n')
        mk("set", p, "--status", "approved")
        got = open(p).read().split("\n")
        assert got[2] == 'status = "approved"  # keep me', got[2]
        assert got[3] == "runs = []" and got[4] == "+++" and "\n".join(got[5:]) == "body\n"
        p = write("set-missing.md", '+++\nplan = "t1"\nruns = []\n+++\nbody\n')
        before = open(p, "rb").read()
        mk("set", p, "--status", "approved", code=2)
        assert open(p, "rb").read() == before

        # F3.1 — set --status approved runs check first: errors refuse the write
        p = write("approve-bad.md", plan_text(phases=phase("P1", route="opencode") + "\n"))
        assert mk("check", p, code=2)["errors"], "route 'opencode' (no model) fails check"
        before = open(p, "rb").read()
        out = mk("set", p, "--status", "approved", code=2)
        assert out["ok"] is False and any("bad route" in e for e in out["errors"]), out
        assert open(p, "rb").read() == before  # refused approval: file byte-identical
        assert mk("set", p, "--status", "running")["status"] == "running"  # no check, unchanged
        assert mk("parse", p)["status"] == "running"
        # warnings alone never block an approval
        p = write("approve-warn.md", plan_text(
            plan='plan = "Bad_ID"', phases=phase("P1", deps='"r9:T1"') + "\n"))
        out = mk("check", p)
        assert out["ok"] is True and out["warnings"], out
        assert mk("set", p, "--status", "approved")["status"] == "approved"
        assert mk("parse", p)["status"] == "approved"

        # write_atomic keeps the file's own mode; the temp + os.replace lands in a writable dir
        p = write("mode.md", plan_text())
        os.chmod(p, 0o444)
        mk("set", p, "--status", "approved")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o444, oct(os.stat(p).st_mode)
        assert mk("parse", p)["status"] == "approved"
        os.chmod(p, 0o644)  # cleanup deletes it either way

        # P1.3 add-run — appends, idempotent, comments and body untouched
        target = os.path.join(tmp, "run-target.md")
        sh.copy(real_plan, target)
        orig = open(target, "rb").read()
        out = mk("add-run", target, "--run", "mk-1")
        assert out["ok"] and out["added"] is True and out["runs"] == ["mk-1"]
        new = open(target, "rb").read()
        ol, nl = orig.decode().split("\n"), new.decode().split("\n")
        assert nl[4] == 'runs = ["mk-1"]' and ol[4] == "runs = []"
        assert [l for i, l in enumerate(ol) if i != 4] == [l for i, l in enumerate(nl) if i != 4]
        assert mk("parse", target)["runs"] == ["mk-1"]
        out = mk("add-run", target, "--run", "r2")
        assert out["runs"] == ["mk-1", "r2"] and out["added"] is True
        twice = open(target, "rb").read()
        out = mk("add-run", target, "--run", "r2")  # idempotent: no rewrite at all
        assert out["added"] is False and out["runs"] == ["mk-1", "r2"]
        assert open(target, "rb").read() == twice
        mk("add-run", target, "--run", "bad id", code=2)
        assert open(target, "rb").read() == twice
        p = write("run-comment.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = []  # ids\n+++\nbody\n')
        mk("add-run", p, "--run", "r1")
        assert open(p).read().split("\n")[3] == 'runs = ["r1"]  # ids'
        p = write("run-multi.md", '+++\nplan = "t1"\nstatus = "draft"\nruns = [\n  "r1",\n]  # end\n+++\nbody\n')
        mk("add-run", p, "--run", "r2")
        got = open(p).read().split("\n")
        assert got[3] == 'runs = ["r1", "r2"]  # end', got[3]
        assert got[4] == "+++" and "\n".join(got[5:]) == "body\n"  # span collapsed, body intact
        assert mk("parse", p)["runs"] == ["r1", "r2"]

        # D4 — nested subtasks parse; a plan with none is unchanged
        p = write("nested.md", plan_text(phases=phase(
            "P1", tasks=task("P1.1", extra=(
                '  deps = "P1"\n  parallel = true\n  route = "opencode model high"\n'
                '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "sub"\n'
                '  acceptance = "done"\n  deps = ["P1.1"]\n  parallel = false\n'
                '  route = { agent = "opencode", model = "m", effort = "low" }\n  owned = ["a.py"]\n'
            ))) + "\n"))
        out = mk("parse", p)
        task0 = out["phases"][0]["tasks"][0]
        sub = task0["subtasks"][0]
        assert sub["id"] == "P1.1.1" and sub["deps"] == ["P1.1"] and sub["parallel"] is False
        assert sub["route"] == {"agent": "opencode", "model": "m", "effort": "low"}
        assert task0["deps"] == ["P1"] and task0["parallel"] is True
        assert mk("check", p)["ok"] is True

        # no store: approve validates and marks approved, and creates no mk.db
        p = write("nostore.md", plan_text(phases=phase("P1", tasks=task("P1.1")) + "\n"))
        empty_data = os.path.join(tmp, "empty-data")
        empty_home = os.path.join(tmp, "empty-home")
        out = mk("approve", p, "--data", empty_data, "--home", empty_home)
        assert out["ok"] and out["store"] is False and out["status"] == "approved"
        assert not os.path.exists(mkpaths.db_in(empty_data))
        assert not os.path.exists(mkpaths.db_in(empty_home))
        assert mk("parse", p)["status"] == "approved"
        assert mk("approve", p, "--data", empty_data, "--home", empty_home).get("noop") is True
        # a failing check refuses the write and still creates no store
        badp = write("nostore-bad.md", plan_text(phases=phase("P1", route="opencode") + "\n"))
        before = open(badp, "rb").read()
        refused = mk("approve", badp, "--data", empty_data, "--home", empty_home, code=2)
        assert any("bad route" in e for e in refused["errors"])
        assert open(badp, "rb").read() == before
        assert not os.path.exists(mkpaths.db_in(empty_data))

        # with a store: import, archive, export; a failed approve leaves file and data
        data = os.path.join(tmp, "store")
        home = os.path.join(data, ".mk-home")
        os.makedirs(data)
        os.makedirs(home)
        db = _mkdb()
        db.open_project(home, data_dir=mkpaths.project_folder(data, home)).close()
        good = write("sample.md", plan_text(
            plan='plan = "sample"',
            phases=phase("P1", tasks=task("P1.1", extra=(
                '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "leaf"\n'
                '  acceptance = "leaf done"\n  deps = "P1.1"\n  parallel = true\n  route = ""\n  owned = []\n'
            ))) + "\n"))
        good_before = open(good, "rb").read()
        out = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out["ok"] and out["store"] is True and out["version"] == 1, out
        # C13 + STORE.2: approve copies; the original file stays, byte for byte (the store row and
        # the archive carry the approved status, the file's status line is not rewritten)
        assert os.path.isfile(good), "approve must leave the original in place"
        assert open(good, "rb").read() == good_before
        assert b'status = "approved"' in open(out["archived"], "rb").read()
        assert "run" not in out and out["tasks_run"] == "r1", out
        arch = out["archived"]
        assert os.path.isfile(arch)
        archived_bytes = open(arch, "rb").read()
        exp = mk("export", os.path.join(tmp, "sample.md"), "--data", data, "--home", home)
        assert exp["ok"] and open(exp["path"], "rb").read() == archived_bytes
        # header bytes of the round trip equal the archived header
        def header_bytes(blob):
            text = blob.decode("utf-8")
            lines = text.split("\n")
            close = next(i for i in range(1, len(lines)) if lines[i].strip() == "+++")
            return "\n".join(lines[:close + 1]).encode()
        assert header_bytes(open(exp["path"], "rb").read()) == header_bytes(archived_bytes)
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        rows = {r["id"]: (r["type"], r["parent"], r["deps"]) for r in con.execute(
            "SELECT id, type, parent, deps FROM tasks WHERE run='r1'")}
        con.close()
        assert rows["P1"] == ("phase", None, "")
        assert rows["P1.1"] == ("task", "P1", "")
        assert rows["P1.1.1"] == ("subtask", "P1.1", "P1.1")
        # same bytes again: no-op, archive kept
        shutil.copy(arch, good)
        out2 = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out2.get("noop") is True and out2["version"] == 1, out2
        assert os.path.isfile(arch)
        # a changed body is a new version; the old archive file stays
        changed = open(good).read().replace("# body", "# body changed")
        open(good, "w").write(changed)
        out3 = mk("approve", good, "--data", data, "--home", home, "--run", "r1")
        assert out3["version"] == 2 and os.path.isfile(arch) and os.path.isfile(out3["archived"])
        assert out3["archived"] != arch and os.path.isfile(good)  # still a copy, not a move

        # a named cycle leaves the file and the store unchanged
        cyc = write("cyc.md", plan_text(plan='plan = "cyc"', phases=phase(
            "P1", tasks=task("P1.1", extra=(
                '  deps = "P1.1.1"\n  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "s"\n'
                '  acceptance = "a"\n  deps = "P1.1"\n  parallel = false\n  route = ""\n  owned = []\n'
            ))) + "\n"))
        blob = open(cyc, "rb").read()
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc, "--data", data, "--home", home, "--run", "r1", code=2)
        # check and approve share the graph, so the refusal is check's "dep cycle:" (ids named)
        assert any("cycle:" in e and "P1.1" in e and "P1.1.1" in e for e in bad["errors"]), bad
        assert open(cyc, "rb").read() == blob
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        assert con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == n_before
        assert con.execute("SELECT id FROM plans WHERE id='cyc'").fetchone() is None
        con.close()

        # <run>:<id> is validated: present passes, missing is named and changes nothing
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        db.add_task(con, "other", "T9", "task", title="ext")
        con.close()
        xref = write("xref.md", plan_text(plan='plan = "xref"', phases=phase("P1", deps='"other:T9"') + "\n"))
        assert mk("approve", xref, "--data", data, "--home", home, "--run", "rx")["ok"]
        missing = write("xref-bad.md", plan_text(
            plan='plan = "xrefbad"', phases=phase("P1", deps='"other:NOPE"') + "\n"))
        blob = open(missing, "rb").read()
        bad = mk("approve", missing, "--data", data, "--home", home, "--run", "ry", code=2)
        assert any("missing dep" in e and "other:NOPE" in e for e in bad["errors"]), bad
        assert open(missing, "rb").read() == blob

        # R3 — a forward dep (phase A on a later phase C, a task on a later sibling) approves
        fwd = write("fwd.md", plan_text(plan='plan = "fwd"', phases=(
            phase("PA", wave=1, deps='"PC"', tasks=task("PA.1", extra='  deps = "PA.2"\n')
                  + task("PA.2"))
            + "\n" + phase("PC", wave=1) + "\n")))
        out = mk("approve", fwd, "--data", data, "--home", home, "--run", "rf")
        assert out["ok"] and out["version"] == 1, out
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        deps = {r["id"]: r["deps"] for r in con.execute("SELECT id, deps FROM tasks WHERE run='rf'")}
        con.close()
        assert deps["PA"] == "PC" and deps["PA.1"] == "PA.2" and deps["PA.2"] == "", deps

        # a cycle across two later items is refused and names both ids; file and store stay
        cyc2 = write("cyc2.md", plan_text(plan='plan = "cyc2"', phases=(
            phase("PA", wave=1, deps='"PC"') + "\n" + phase("PC", wave=1, deps='"PA"') + "\n")))
        blob = open(cyc2, "rb").read()
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        n_before = con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        con.close()
        bad = mk("approve", cyc2, "--data", data, "--home", home, "--run", "rc", code=2)
        assert any("cycle:" in e and "PA" in e and "PC" in e for e in bad["errors"]), bad
        assert open(cyc2, "rb").read() == blob
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        assert con.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == n_before
        assert con.execute("SELECT id FROM plans WHERE id='cyc2'").fetchone() is None
        con.close()

        # a copy of the real mk-store plan approves; every phase and task lands with its deps
        real = "/Users/rohits/Development/matrixkoder/.matrixkoder/plans/mk-store.md"
        copy = os.path.join(tmp, "mk-store-copy.md")
        shutil.copy(real, copy)
        parsed = mk("parse", copy)
        want = {}
        for ph in parsed["phases"]:
            want[ph["id"]] = ",".join(str(d) for d in ph["deps"])
            for task_row in ph["tasks"]:
                want[task_row["id"]] = ",".join(str(d) for d in task_row["deps"])
        out = mk("approve", copy, "--data", data, "--home", home, "--run", "mkstore")
        assert out["ok"] and os.path.isfile(copy), out  # copied, not moved
        assert os.path.isfile(real)  # the real plan was never touched
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        got = {r["id"]: r["deps"] for r in con.execute(
            "SELECT id, deps FROM tasks WHERE run='mkstore'")}
        con.close()
        assert set(got) == set(want), (sorted(set(want) - set(got)), sorted(set(got) - set(want)))
        assert got == want

        # CRLF export is byte-equal to the archived file
        crlf = write("crlf-exp.md", plan_text(plan='plan = "crlfexp"', phases=phase("P1") + "\n").replace("\n", "\r\n"))
        original = open(crlf, "rb").read()
        out = mk("approve", crlf, "--data", data, "--home", home, "--run", "rcrlf")
        assert out["ok"], out
        archived = open(out["archived"], "rb").read()
        dest = os.path.join(tmp, "crlfexp.md")
        exp = mk("export", dest, "--data", data, "--home", home)
        assert exp["ok"] and open(dest, "rb").read() == archived
        assert b"\r\n" in archived and archived != original  # status line changed, endings did not
        assert archived.replace(b"\r\n", b"") .count(b"\n") == 0

        # a task or subtask with no id is a check error
        noid = write("noid.md", plan_text(phases=phase("P1", tasks=(
            '  [[phase.task]]\n  title = "missing"\n  acceptance = "a"\n'
            '  [[phase.task.subtask]]\n  title = "also missing"\n  acceptance = "b"\n')) + "\n"))
        bad = mk("check", noid, code=2)
        assert any("task has no id" in e for e in bad["errors"]), bad
        assert any("subtask has no id" in e for e in bad["errors"]), bad

        # FB — a task-level cycle and a subtask self-dep are check errors, so set cannot approve them
        tcyc = write("task-cycle.md", plan_text(phases=phase("P1", tasks=(
            task("P1.1", extra='  deps = "P1.2"\n') + task("P1.2", extra='  deps = "P1.1"\n'))) + "\n"))
        bad = mk("check", tcyc, code=2)
        assert any(e.startswith("dep cycle:") and "P1.1" in e and "P1.2" in e for e in bad["errors"]), bad
        before = open(tcyc, "rb").read()
        refused = mk("set", tcyc, "--status", "approved", code=2)
        assert any("dep cycle:" in e for e in refused["errors"])
        assert open(tcyc, "rb").read() == before
        refused = mk("approve", tcyc, "--data", empty_data, "--home", empty_home, code=2)
        assert any("dep cycle:" in e for e in refused["errors"])
        assert open(tcyc, "rb").read() == before
        selfdep = write("self-sub.md", plan_text(phases=phase("P1", tasks=task("P1.1", extra=(
            '  [[phase.task.subtask]]\n  id = "P1.1.1"\n  title = "s"\n  acceptance = "a"\n'
            '  deps = "P1.1.1"\n  parallel = false\n  route = ""\n  owned = []\n'))) + "\n"))
        bad = mk("check", selfdep, code=2)
        assert any("dep cycle:" in e and "P1.1.1" in e for e in bad["errors"]), bad
        before = open(selfdep, "rb").read()
        mk("set", selfdep, "--status", "approved", code=2)
        assert open(selfdep, "rb").read() == before

        # FB — set --status approved names an unresolved cross-run dep instead of staying silent
        xrefw = write("xref-warn.md", plan_text(phases=phase("P1", deps='"other-run:T1"') + "\n"))
        warned = mk("set", xrefw, "--status", "approved")
        assert warned["status"] == "approved", warned
        assert any("cross-run" in w and "other-run:T1" in w for w in warned.get("warnings") or []), warned

        # FB — a UTF-8 BOM survives check -> approve -> export byte for byte (status flip aside)
        bom_text = plan_text(plan='plan = "bomplan"', phases=phase("P1", tasks=task("P1.1")) + "\n").replace("\n", "\r\n")
        bom = write("bom.md", bom_text)
        raw_bom = b"\xef\xbb\xbf" + open(bom, "rb").read()
        open(bom, "wb").write(raw_bom)
        assert mk("check", bom)["ok"] is True
        out = mk("approve", bom, "--data", data, "--home", home, "--run", "rbom")
        assert out["ok"] and os.path.isfile(bom), out  # the BOM file stays too
        archived_bom = open(out["archived"], "rb").read()
        assert open(bom, "rb").read() == raw_bom  # STORE.2: the file is not rewritten, the archive holds the approved bytes
        assert archived_bom.startswith(b"\xef\xbb\xbf") and b"\r\n" in archived_bom
        assert b'status = "approved"' in archived_bom
        dest = os.path.join(tmp, "bomplan.md")
        exp = mk("export", dest, "--data", data, "--home", home)
        assert open(dest, "rb").read() == archived_bom

        # FB — kill seam: 20 trials after the commit and 20 after the archive; the next approve converges
        for step in ("commit", "archive"):
            for n in range(20):
                sdata = os.path.join(tmp, f"seam-{step}-{n}")
                shome = os.path.join(sdata, ".mk-home")
                os.makedirs(shome)
                db.open_project(shome, data_dir=sdata).close()
                src = write(f"seam-{step}-{n}.md", plan_text(
                    plan=f'plan = "seam{step}{n}"', phases=phase("P1", tasks=task("P1.1")) + "\n"))
                src_before = open(src, "rb").read()
                env = dict(os.environ, MATRIXKODER_DATA=sdata, MK_PLAN_KILL_AFTER=step)
                r = subprocess.run([sys.executable, MK_PLAN_PY, "approve", src,
                                    "--data", sdata, "--home", shome, "--run", "s1"],
                                   capture_output=True, text=True, env=env)
                assert r.returncode != 0, (step, n, r.returncode, r.stdout, r.stderr)
                env.pop("MK_PLAN_KILL_AFTER", None)
                r2 = subprocess.run([sys.executable, MK_PLAN_PY, "approve", src,
                                     "--data", sdata, "--home", shome, "--run", "s1"],
                                    capture_output=True, text=True, env=env)
                assert r2.returncode == 0, (step, n, r2.returncode, r2.stdout, r2.stderr)
                got = json.loads(r2.stdout.strip().splitlines()[-1])
                # commit-kill: the next approve finishes the missing archive (same version).
                # archive-kill: the archive already matches, so the next approve is a no-op
                # and the original stays, now on the approved bytes.
                assert got["ok"] and got["version"] == 1, got
                if step == "archive":
                    assert got.get("noop") is True, got
                # C13 + STORE.2: a kill leaves the original in place and the converging approve
                # does not rewrite it; the archive holds the approved bytes
                assert os.path.isfile(src), step
                assert open(src, "rb").read() == src_before
                assert os.path.isfile(got["archived"])
                assert b'status = "approved"' in open(got["archived"], "rb").read()


        # the run's plan path still resolves through mk-state after the file is archived
        state = os.path.join(os.path.dirname(MK_PLAN_PY), "mk-state.py")
        link = os.path.join(tmp, "link.md")
        open(link, "w").write(plan_text(plan='plan = "link"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, state, "--data", data, "run", "init", "--force",
                            "--id", "lk", "--feature", "L", "--branch", "bl", "--plan", link],
                           capture_output=True, text=True,  # an old-shape run: it starts from the plan FILE
                           env=dict(os.environ, MK_LEGACY_RUN_INIT="1"))
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        ap = mk("approve", link, "--data", data, "--home", home, "--run", "lk")
        # the original path still resolves after the approve (the hermes-switchui issue): C13
        # approve copies, so the file mk-state was pointed at is still there
        assert ap["ok"] and os.path.isfile(link), ap
        # the run is live (P1 is planned, not terminal): T1's rule refuses a bare sync,
        # so this mechanics test passes --from-file ("the plan file wins")
        r = subprocess.run([sys.executable, state, "--data", data, "plan", "sync", "--run", "lk",
                            "--from-file"], capture_output=True, text=True)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["ok"] and got.get("plan") == "link", got

        # D1.2 — approve copies (C13), data-dir fallback, tasks_run, import, delete, pause
        def plans_rows(pid, datadir=data, homedir=home):
            con = db.open_project(homedir, data_dir=datadir)
            try:
                n = con.execute("SELECT COUNT(*) c FROM plans WHERE id=?", (pid,)).fetchone()["c"]
                row = con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
            finally:
                con.close()
            return n, (dict(row) if row is not None else None)

        # 1. a good file imports into exactly one plans row and touches no file byte.
        # the stored version is 1 for a fresh id (STORE.1: an import is a version) and the reply reports it.
        imp1 = write("imp1.md", plan_text(plan='plan = "imp1"', status='status = "draft"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "# imp1 — Sample Title"))
        blob1 = open(imp1, "rb").read()
        out = mk("import", imp1, "--data", data, "--home", home)
        assert out["ok"] is True and out["store"] is True and out["plan"] == "imp1", out
        assert out["title"] == "Sample Title", out  # the H1 text after the em dash
        n, row1 = plans_rows("imp1")
        assert n == 1, n
        assert row1["version"] == 1 and out["version"] == row1["version"], (row1, out)
        assert row1["title"] == "Sample Title"
        con = db.open_project(home, data_dir=mkpaths.project_folder(data, home))
        n_tasks = con.execute(
            "SELECT COUNT(*) c FROM tasks WHERE id IN ('P1','P1.1') AND run='imp1'").fetchone()["c"]
        con.close()
        assert n_tasks == 0, n_tasks  # import loads the plan, it does not approve the work
        assert open(imp1, "rb").read() == blob1

        # 2. the same bytes again: the upsert lands on the same row, byte for byte
        out2 = mk("import", imp1, "--data", data, "--home", home)
        assert out2["ok"] is True and out2["plan"] == "imp1", out2
        n, row2 = plans_rows("imp1")
        assert n == 1 and row2 == row1, (row1, row2)
        assert out2["version"] == row1["version"]

        # 3. no H1 in the body: the plan id is the title
        imp3 = write("imp3.md", plan_text(plan='plan = "imp3"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "## only a subheading\nbody text\n"))
        out = mk("import", imp3, "--data", data, "--home", home)
        assert out["ok"] is True and out["plan"] == "imp3" and out["title"] == "imp3", out
        n, row3 = plans_rows("imp3")
        assert n == 1 and row3["title"] == "imp3", (n, row3)

        # 4. a file that is not a plan, and one whose header does not parse: both refused
        # with ok:false and a reason, exit 0, and neither leaves a plans row behind
        noopen = write("imp4a.md", 'plan = "imp4a"\n+++\n+++\nbody\n')
        out = mk("import", noopen, "--data", data, "--home", home)
        assert out["ok"] is False and isinstance(out.get("reason"), str) and out["reason"], out
        badtoml = write("imp4b.md", '+++\nplan = "imp4b"\nstatus = "draft"\ncreated == 2026-10-08\n'
                                    '+++\n\n# body\n')
        out = mk("import", badtoml, "--data", data, "--home", home)
        assert out["ok"] is False and isinstance(out.get("reason"), str) and out["reason"], out
        assert plans_rows("imp4a")[0] == 0 and plans_rows("imp4b")[0] == 0

        # 5. --dry-run reports exactly what a real import would, and writes nothing
        imp5 = write("imp5.md", plan_text(plan='plan = "imp5"', status='status = "draft"',
                                          phases=phase("P1", tasks=task("P1.1")) + "\n").replace(
            "# body", "# imp5 — Dry Title"))
        blob5 = open(imp5, "rb").read()
        out = mk("import", imp5, "--dry-run", "--data", data, "--home", home)
        assert out["ok"] is True and out["dry_run"] is True, out
        assert out["plan"] == "imp5" and out["title"] == "Dry Title", out
        assert plans_rows("imp5")[0] == 0
        assert open(imp5, "rb").read() == blob5

        # L10.6: inbox plans move into the project. The home is the one the import above used.
        def inbox_plan(pid, header_id=None, docs=("notes.md", "mock/a.png")):
            d = os.path.join(mkpaths.inbox_dir(home), pid)
            for rel in docs:
                os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
                with open(os.path.join(d, rel), "w") as f:
                    f.write(f"doc {rel}\n")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "plan.md"), "w") as f:
                f.write(plan_text(plan=f'plan = "{header_id or pid}"',
                                  phases=phase("P1", tasks=task("P1.1")) + "\n"))
            return d
        ib = inbox_plan("ibx1")
        plan_md_bytes = open(os.path.join(ib, "plan.md"), "rb").read()
        tgt_plan, tgt_assets = mkpaths.plan_file(data, "ibx1"), os.path.join(mkpaths.plan_dir(data, "ibx1"), "assets")
        # dry-run: lists every move, changes nothing
        out = mk("import", ib, "--dry-run", "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 3 and out["moved"][0]["to"] == tgt_plan, out
        assert os.path.isdir(ib) and not os.path.exists(tgt_plan) and plans_rows("ibx1")[0] == 0
        # a target plan.md with other bytes: refused, nothing moved, no row
        os.makedirs(os.path.dirname(tgt_plan), exist_ok=True)
        with open(tgt_plan, "w") as f:
            f.write("someone else's plan\n")
        out = mk("import", ib, "--data", data, "--home", home)
        assert out["ok"] is False and out["reason"] == "target exists", out
        assert os.path.isfile(os.path.join(ib, "notes.md")) and plans_rows("ibx1")[0] == 0
        os.unlink(tgt_plan)
        # a taken asset name refuses too
        os.makedirs(tgt_assets, exist_ok=True)
        with open(os.path.join(tgt_assets, "notes.md"), "w") as f:
            f.write("other\n")
        out = mk("import", os.path.join(ib, "plan.md"), "--data", data, "--home", home)
        assert out["ok"] is False and out["reason"] == "target exists", out
        assert not os.path.exists(tgt_plan)
        os.unlink(os.path.join(tgt_assets, "notes.md"))
        # the real move: row recorded, files at targets, source bytes kept, inbox folder gone
        out = mk("import", ib, "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 3 and plans_rows("ibx1")[0] == 1, out
        assert open(tgt_plan, "rb").read() == plan_md_bytes
        assert open(os.path.join(tgt_assets, "notes.md")).read() == "doc notes.md\n"
        assert open(os.path.join(tgt_assets, "mock", "a.png")).read() == "doc mock/a.png\n"
        assert not os.path.exists(ib), "the emptied inbox folder was not removed"
        # header id differs from the folder name: refused, nothing moved
        bad = inbox_plan("ibx2", header_id="other-id")
        out = mk("import", bad, "--data", data, "--home", home)
        assert out["ok"] is False and "plan id" in out["reason"], out
        assert os.path.isfile(os.path.join(bad, "notes.md")) and plans_rows("other-id")[0] == 0
        # outside the inbox: unchanged behaviour, moved is empty, the file stays
        out = mk("import", imp1, "--data", data, "--home", home)
        assert out["ok"] and out["moved"] == [] and os.path.isfile(imp1), out

        # 6. delete takes an ARCHIVED plan only; an unmigrated plan has no folder, so archive just
        # flips the status. Backup first, then the rows go; an unarchived plan is refused.
        plans_dir = os.path.join(data, "plans")
        os.makedirs(plans_dir, exist_ok=True)
        live = os.path.join(plans_dir, "imp6.md")
        open(live, "w").write(plan_text(plan='plan = "imp6"', phases=phase("P1") + "\n"))
        assert mk("import", live, "--data", data, "--home", home)["ok"]
        out = mk("delete", "imp6", "--data", data, "--home", home, code=2)
        assert "archive it first" in " ".join(out["errors"]) and plans_rows("imp6")[0] == 1, out
        out = mk("archive", "imp6", "--data", data, "--home", home)
        assert out["ok"] and len(out["moved"]) == 1 and not os.path.exists(live), out  # legacy plan file moved
        out = mk("delete", "imp6", "--data", data, "--home", home)
        assert out["ok"] and out["plan"] == "imp6" and out["purged"]["plans"] == 1, out
        assert os.path.isfile(out["backup"]) and "imp6-pre-delete-" in out["backup"], out
        assert os.path.isfile(mkpaths.archive_dir(data, "imp6", home) + "/plans/imp6.md")  # never unlinked
        assert plans_rows("imp6")[0] == 0

        # 7. live run refused (archive and delete); a held lane slot refuses delete; MK_ARCHIVE_FAIL
        # leaves every row; a full purge leaves no row naming the plan and another plan's rows stay.
        def project_con():
            return db.open_project(home, data_dir=mkpaths.project_folder(data, home))

        def named(con, plan_id, run_ids):
            n = con.execute("SELECT COUNT(*) FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
            n += con.execute("SELECT COUNT(*) FROM runs WHERE plan=?", (plan_id,)).fetchone()[0]
            for t in PURGE_BY_RUN:
                n += con.execute(f"SELECT COUNT(*) FROM {t} WHERE run IN ({','.join('?' * len(run_ids))})",
                                 run_ids).fetchone()[0]
            return n

        for pid in ("imp7", "imp8"):
            f = os.path.join(plans_dir, pid + ".md")
            open(f, "w").write(plan_text(plan=f'plan = "{pid}"', phases=phase("P1") + "\n"))
            assert mk("import", f, "--data", data, "--home", home)["ok"]
        con = project_con()
        for pid, run in (("imp7", "imp7run"), ("imp8", "imp8run")):
            con.execute("INSERT OR REPLACE INTO runs(id, plan, plan_version, branch, status, stage)"
                        " VALUES (?,?,0,'br','active','build')", (run, pid))
        con.commit()
        con.close()
        out = mk("archive", "imp7", "--data", data, "--home", home, code=2)
        said = " ".join(out["errors"])
        assert "imp7run" in said and "live run" in said and plans_rows("imp7")[0] == 1, out
        con = project_con()
        con.execute("UPDATE runs SET status='done'")
        con.commit()
        for run in ("imp7run", "imp8run", "imp7", "imp7-b"):  # imp7: import-time rows keyed by the plan id
            db.add_task(con, run, "P1", "phase", title="kept")
        for pid, run in (("imp7", "imp7run"), ("imp8", "imp8run")):
            con.execute("INSERT INTO events(run, task, t, chan, kind, text) VALUES (?,'P1','t','c','k','x')", (run,))
            con.execute("INSERT INTO artifacts(run, task, kind, text) VALUES (?,'P1','k','x')", (run,))
            con.execute("INSERT INTO acks(run, msg, lane) VALUES (?,'m','l')", (run,))
            con.execute("INSERT INTO waits(run, question) VALUES (?,'q')", (run,))
            con.execute("INSERT INTO event_src(run, src) VALUES (?,'s')", (run,))
        con.execute("INSERT INTO lane_slots(slot, run, state) VALUES ('s1','imp7run','busy')")
        con.commit()
        con.close()
        assert mk("archive", "imp7", "--data", data, "--home", home)["ok"]
        assert mk("archive", "imp8", "--data", data, "--home", home)["ok"]
        out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        assert "lane slot" in " ".join(out["errors"]) and plans_rows("imp7")[0] == 1, out
        con = project_con()
        con.execute("DELETE FROM lane_slots")
        con.commit()
        con.close()
        os.environ["MK_DELETE_BACKUP_FAIL"] = "1"
        try:
            out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        finally:
            os.environ.pop("MK_DELETE_BACKUP_FAIL", None)
        assert "backup failed" in " ".join(out["errors"]), out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 8, named(con, "imp7", ["imp7run"])
        con.close()
        arch = mkpaths.archive_dir(data, None, home)
        assert not [n for n in os.listdir(arch) if n.endswith(".tmp")], os.listdir(arch)
        # a child row written between the backup and the purge aborts delete; nothing is deleted
        os.environ["MK_DELETE_RACE_SQL"] = ("INSERT INTO events(run, task, t, chan, kind, text)"
                                            " VALUES ('imp7run','P1','t','c','k','late')")
        try:
            out = mk("delete", "imp7", "--data", data, "--home", home, code=2)
        finally:
            os.environ.pop("MK_DELETE_RACE_SQL", None)
        said = " ".join(out["errors"])
        assert "events went from 1 to 2" in said and "backup kept at" in said, out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 9, "the aborted delete changed rows"
        con.execute("DELETE FROM events WHERE text='late'")
        con.commit()
        con.close()
        # a failure mid-purge rolls the whole transaction back
        os.environ["MK_PURGE_FAIL"] = "1"
        try:
            r = subprocess.run([sys.executable, MK_PLAN_PY, "delete", "imp7",
                                "--data", data, "--home", home], capture_output=True, text=True)
        finally:
            os.environ.pop("MK_PURGE_FAIL", None)
        assert r.returncode != 0 and "injected purge failure" in r.stderr, (r.stdout, r.stderr)
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 8, "a mid-purge failure left a partial delete"
        con.close()
        out = mk("delete", "imp7", "--data", data, "--home", home)
        assert out["purged"]["event_src"] == 1, out
        assert out["ok"] and out["purged"]["tasks"] == 2 and out["purged"]["runs"] == 1, out
        con = project_con()
        assert named(con, "imp7", ["imp7run"]) == 0
        assert named(con, "imp8", ["imp8run"]) == 8, "another plan's rows were touched"
        left = con.execute("SELECT run FROM tasks WHERE run IN ('imp7','imp7-b','imp7run','imp8run') ORDER BY run").fetchall()
        assert [r[0] for r in left] == ["imp7-b", "imp8run"], [r[0] for r in left]  # plan-id rows gone, prefix plan kept
        con.close()
        chk = sqlite3.connect("file:" + urllib.parse.quote(out["backup"]) + "?mode=ro", uri=True)
        assert chk.execute("SELECT COUNT(*) FROM runs WHERE plan='imp7'").fetchone()[0] == 1
        chk.close()

        # 8. asset set: archive moves the C2 folder, plans/<id>.md, plans/archive/<id>-v<n>.md and the
        # run folders (layout kept) and nothing of an id-prefix plan; unarchive restores them
        # byte-identical; a completed archive is refused; a taken archive name gets a stamp.
        def put(path, text):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(text)

        def snap(paths):
            out = {}
            for q in paths:
                for dp, _, fs in os.walk(q) if os.path.isdir(q) else [(os.path.dirname(q), 0, [os.path.basename(q)])]:
                    for f in fs:
                        out[os.path.join(dp, f)] = open(os.path.join(dp, f), "rb").read()
            return out

        def seed(pid, status=None):
            f = os.path.join(plans_dir, pid + ".md")
            put(f, plan_text(plan=f'plan = "{pid}"', phases=phase("P1") + "\n"))
            assert mk("import", f, "--data", data, "--home", home)["ok"]
            con = project_con()
            con.execute("UPDATE plans SET status=? WHERE id=?", (status, pid))
            con.execute("INSERT INTO runs(id, plan, plan_version, branch, status, stage) VALUES (?,?,0,'b','done','x')",
                        (pid + "run", pid))
            con.commit()
            con.close()

        seed("imp9", "parked")
        seed("imp9-extra", "done")  # shares the id prefix: must stay untouched
        pdir = mkpaths.plan_dir(data, "imp9")
        put(os.path.join(pdir, "assets", "a.txt"), "asset\n")
        put(os.path.join(pdir, "plan.md"), "x\n")
        v1, v2 = (os.path.join(plans_dir, "archive", f"imp9-v{n}.md") for n in (1, 2))
        put(v1, "v1\n"), put(v2, "v2\n")
        put(os.path.join(plans_dir, "archive", "imp9-extra-v1.md"), "decoy\n")
        put(os.path.join(data, mkpaths.RUNS_LEGACY_NAME, "imp9run", "r.txt"), "legacy run\n")
        put(os.path.join(data, "runs", "imp9run", "y.txt"), "run\n")
        con = project_con()
        con.execute("UPDATE plans SET archived_path=? WHERE id='imp9'", (v2,))
        con.commit()
        con.close()
        decoys = [os.path.join(plans_dir, "imp9-extra.md"), os.path.join(plans_dir, "archive", "imp9-extra-v1.md")]
        assets = [pdir, os.path.join(plans_dir, "imp9.md"), v1, v2,
                  os.path.join(data, mkpaths.RUNS_LEGACY_NAME, "imp9run"), os.path.join(data, "runs", "imp9run")]
        before_assets, before_decoys = snap(assets), snap(decoys)
        out = mk("archive", "imp9", "--data", data, "--home", home)
        base = mkpaths.archive_dir(data, "imp9", home)
        assert len(out["moved"]) == 6 and all(m.startswith(base + os.sep) for m in out["moved"]), out
        assert not any(os.path.exists(q) for q in assets), "an asset stayed behind"
        assert snap(decoys) == before_decoys, "an id-prefix plan's files were touched"
        assert os.path.isfile(os.path.join(base, "folder", "assets", "a.txt"))
        assert os.path.isfile(os.path.join(base, "plans", "archive", "imp9-v2.md"))
        assert os.path.isfile(os.path.join(base, mkpaths.RUNS_LEGACY_NAME, "imp9run", "r.txt"))
        con = project_con()
        r9 = con.execute("SELECT status, header_json, archived_path FROM plans WHERE id='imp9'").fetchone()
        con.close()
        h9 = json.loads(r9["header_json"])
        assert r9["status"] == "archived" and h9["archived_from"] == "parked" and h9["archived_to"] == base
        assert len(h9["archived_files"]) == 6 and r9["archived_path"] == os.path.join(base, "plans", "archive", "imp9-v2.md")
        out = mk("archive", "imp9", "--data", data, "--home", home, code=2)  # completed: refused
        assert "already archived at" in " ".join(out["errors"]), out
        put(os.path.join(plans_dir, "imp9.md"), "squatter\n")
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # original occupied
        assert "already exists" in " ".join(out["errors"]) and "imp9.md" in " ".join(out["errors"]), out
        os.remove(os.path.join(plans_dir, "imp9.md"))
        os.rename(os.path.join(base, "plans", "archive", "imp9-v1.md"), os.path.join(base, "away"))
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # archived item gone
        assert "missing" in " ".join(out["errors"]) and "imp9-v1.md" in " ".join(out["errors"]), out
        os.rename(os.path.join(base, "away"), os.path.join(base, "plans", "archive", "imp9-v1.md"))
        out = mk("unarchive", "imp9", "--data", data, "--home", home)
        assert len(out["moved"]) == 6 and not os.path.exists(base), out
        assert snap(assets) == before_assets, "unarchive did not restore the files byte-identical"
        assert snap(decoys) == before_decoys
        con = project_con()
        r9 = con.execute("SELECT status, header_json, archived_path FROM plans WHERE id='imp9'").fetchone()
        con.close()
        h9 = json.loads(r9["header_json"])
        assert r9["status"] == "parked" and not {"archived_from", "archived_to", "archived_files"} & set(h9)
        assert r9["archived_path"] == v2
        out = mk("unarchive", "imp9", "--data", data, "--home", home, code=2)  # not archived now
        assert "not archived" in " ".join(out["errors"]), out

        # crash recovery: status written, archived_to recorded, folder never moved -> unarchive
        # just restores the status
        seed("impc", "done")
        con = project_con()
        con.execute("UPDATE plans SET status='archived', header_json=? WHERE id='impc'",
                    (json.dumps({"archived_from": "done", "archived_to": os.path.join(data, "nowhere")}),))
        con.commit()
        con.close()
        out = mk("unarchive", "imp" + "c", "--data", data, "--home", home)
        assert out["ok"] and os.path.isfile(os.path.join(plans_dir, "impc.md")), out
        con = project_con()
        assert con.execute("SELECT status FROM plans WHERE id='impc'").fetchone()[0] == "done"
        con.close()

        # repair: already 'archived' with no record and the legacy files still in place -> the move
        # happens; a second archive then refuses; a taken archive name gets a stamp, never overwritten
        seed("impb", "archived")
        taken = mkpaths.archive_dir(data, "impb", home)
        put(os.path.join(taken, "old.md"), "older\n")
        out = mk("archive", "impb", "--data", data, "--home", home)
        assert len(out["moved"]) == 1 and out["moved"][0].startswith(taken + "-"), out  # plan file only
        assert open(os.path.join(taken, "old.md")).read() == "older\n"
        assert not os.path.exists(os.path.join(plans_dir, "impb.md"))
        con = project_con()
        hb = json.loads(con.execute("SELECT header_json FROM plans WHERE id='impb'").fetchone()[0])
        con.close()
        assert out["moved"][0].startswith(hb["archived_to"] + os.sep) and hb["archived_to"].startswith(taken + "-")
        out = mk("archive", "impb", "--data", data, "--home", home, code=2)
        assert "already archived at" in " ".join(out["errors"]), out

        # D1.1 columns: filled when present, harmless when absent
        cols_data = os.path.join(tmp, "store-cols")
        cols_home = os.path.join(cols_data, ".mk-home")
        os.makedirs(cols_home)
        db.open_project(cols_home, data_dir=cols_data).close()
        con = db.open_project(cols_home, data_dir=cols_data)
        have = {r[1] for r in con.execute("PRAGMA table_info(plans)")}
        for col, decl in (("status", "TEXT"), ("created", "TEXT"),
                          ("design", "TEXT"), ("integration", "TEXT")):
            if col not in have:  # a v4 store (D1.1) already has them
                con.execute(f"ALTER TABLE plans ADD COLUMN {col} {decl}")
        con.commit()
        con.close()
        colp = write("cols.md", plan_text(
            plan='plan = "cols"', top='design = ["a.py"]\nintegration = ["b.py"]',
            phases=phase("P1", tasks=task("P1.1")) + "\n").replace("# body", "# cols — Col Title"))
        out = mk("import", colp, "--data", cols_data, "--home", cols_home)
        assert out["ok"] and out["title"] == "Col Title", out
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert n == 1 and crow["status"] == "draft" and crow["created"] == "2026-10-08", crow
        assert json.loads(crow["design"]) == ["a.py"] and json.loads(crow["integration"]) == ["b.py"]
        out = mk("approve", colp, "--data", cols_data, "--home", cols_home, "--run", "colsrun")
        assert out["ok"] and os.path.isfile(colp), out
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "approved" and crow["version"] == 1, crow
        # the shared store has none of the four columns: approve and import still succeed
        plain = write("plain.md", plan_text(plan='plan = "plain"', phases=phase("P1") + "\n"))
        assert mk("import", plain, "--data", data, "--home", home)["ok"]
        assert mk("approve", plain, "--data", data, "--home", home, "--run", "plainrun")["ok"]

        # pause and back (STORE.2): plans.status changes in the store and the file's bytes stay; parse
        # and check answer with the store's status once the plan is stored
        col_before = open(colp, "rb").read()
        parked = mk("set", colp, "--status", "parked", "--data", cols_data, "--home", cols_home)
        assert parked["status"] == "parked" and parked.get("store") is True, parked
        assert open(colp, "rb").read() == col_before
        assert mk("parse", colp, "--data", cols_data, "--home", cols_home)["status"] == "parked"
        assert mk("parse", colp)["status"] == "draft"  # no store resolved: the file answers as before
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "parked", crow
        back = mk("set", colp, "--status", "approved", "--data", cols_data, "--home", cols_home)
        assert back["status"] == "approved", back
        n, crow = plans_rows("cols", cols_data, cols_home)
        assert crow["status"] == "approved", crow
        # the plan is in the store: set changes the store row and never the file
        plain_before = open(plain, "rb").read()
        assert mk("set", plain, "--status", "parked", "--data", data, "--home", home)["status"] == "parked"
        assert mk("parse", plain, "--data", data, "--home", home)["status"] == "parked"
        assert mk("set", plain, "--status", "approved", "--data", data, "--home", home)["status"] == "approved"
        assert open(plain, "rb").read() == plain_before

        # no --data, cwd = a temp repo whose .matrixkoder/mk.db exists -> store: true
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".matrixkoder"))
        subprocess.run(["git", "init", "-q", repo], check=True)
        db.open_project(os.path.join(repo, ".mk-home"),
                        data_dir=os.path.join(repo, ".matrixkoder")).close()
        rp = os.path.join(repo, "repoplan.md")
        open(rp, "w").write(plan_text(plan='plan = "repoplan"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, MK_PLAN_PY, "approve", rp,
                            "--home", os.path.join(repo, ".mk-home")],
                           capture_output=True, text=True, cwd=repo,
                           env={k: v for k, v in os.environ.items() if k != "MATRIXKODER_DATA"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["ok"] and got["store"] is True, got
        assert os.path.isfile(rp) and os.path.isfile(got["archived"])
        assert "tasks_run" in got and "run" not in got, got

        # file-only next to an existing mk.db names that store in a warning
        side = os.path.join(tmp, "side")
        os.makedirs(side)
        open(mkpaths.db_in(side), "wb").close()
        sp = os.path.join(side, "side.md")
        open(sp, "w").write(plan_text(plan='plan = "sideplan"', phases=phase("P1") + "\n"))
        r = subprocess.run([sys.executable, MK_PLAN_PY, "approve", sp],
                           capture_output=True, text=True, cwd=tmp,
                           env={k: v for k, v in os.environ.items() if k != "MATRIXKODER_DATA"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["store"] is False and any(os.path.basename(mkpaths.db_in(side)) in w for w in got.get("warnings") or []), got

        # C2 (L3.2): on a MIGRATED project (root db_path set by L2's mk-migrate --switch) import,
        # approve and delete write plans and archives under projects/<slug>/plans/<id>/ and leave
        # the repo's .matrixkoder byte-identical.
        def tree_hash(top):
            h = hashlib.sha256()
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames.sort()
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    h.update(os.path.relpath(full, top).encode())
                    with open(full, "rb") as fh:
                        h.update(fh.read())
            return h.hexdigest()

        mig_repo = os.path.join(tmp, "mig-repo")
        mig_data = os.path.join(mig_repo, ".matrixkoder")
        os.makedirs(os.path.join(mig_data, "plans", "archive"), exist_ok=True)
        subprocess.run(["git", "init", "-q", mig_repo], check=True)
        pre = os.path.join(mig_data, "plans", "old.md")
        open(pre, "w").write(plan_text(plan='plan = "old"', phases=phase("P1") + "\n"))
        open(mkpaths.legacy_yaml(mig_data), "w").write("data_dir: .matrixkoder/\n")
        home = os.environ["MK_HOME"]
        db.open_project(home, "migs", data_dir=mig_data).close()
        con = db.open_root(home)
        con.execute("INSERT OR REPLACE INTO projects(slug, name, repo, data_dir, mode, db_path)"
                    " VALUES (?,?,?,?,?,NULL)", ("migs", "migs", mig_repo, os.path.realpath(mig_data), "home"))
        con.commit()
        con.close()
        r = subprocess.run([sys.executable, os.path.join(root, "scripts", "mk-migrate.py"),
                            "--switch", "--slug", "migs", "--data", mig_data, "--home", home,
                            "--repo", mig_repo],
                           capture_output=True, text=True, env=dict(os.environ, MK_SELFTEST="1"))
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        proj = os.path.join(home, "projects", "migs")
        before = tree_hash(mig_data)

        mp = write("migplan.md", plan_text(plan='plan = "migplan"',
                                           phases=phase("P1", tasks=task("P1.1")) + "\n"))
        out = mk("import", mp, "--data", mig_data, "--home", home)
        assert out["ok"], out
        # Z15 (R02): import on a migrated project ensures the plan folder's assets/ too
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "a migrated import did not create the plan folder's assets/"
        # the dashboard List reads the root plan_summary: an imported draft must be in it
        con = db.open_root(home)
        n_sum = con.execute("SELECT COUNT(*) c FROM plan_summary WHERE project='migs' AND plan='migplan'").fetchone()["c"]
        con.close()
        assert n_sum == 1 and "warnings" not in out, (n_sum, out)
        out = mk("approve", mp, "--data", mig_data, "--home", home, "--run", "migrun")
        assert out["ok"] and out["version"] == 1, out
        assert out["archived"] == os.path.join(proj, "plans", "migplan", "archive", "migplan-v1.md"), out
        assert os.path.isfile(os.path.join(proj, "plans", "migplan", "archive", "migplan-v1.md"))
        # Z15 (R02): approve keeps the plan.md copy AND ensures assets/ exists (C2). This FAILS on
        # e83e6e9 (proof in the report): assets/ was never created.
        assert os.path.isfile(os.path.join(proj, "plans", "migplan", "plan.md")), \
            "the approved plan.md is missing from the migrated plan folder"
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "a migrated approve did not create the plan folder's assets/ (R02)"
        # a second approve on the same bytes stays a no-op and keeps the folder intact
        out = mk("approve", mp, "--data", mig_data, "--home", home, "--run", "migrun")
        assert out["ok"] and out.get("noop") is True, out
        assert os.path.isdir(os.path.join(proj, "plans", "migplan", "assets")), \
            "the no-op approve dropped the plan folder's assets/"
        out = mk("archive", "migplan", "--data", mig_data, "--home", home)
        arch = os.path.join(proj, "archive", "plans", "migplan")
        assert out["ok"] and out["moved"] == [os.path.join(arch, "folder")], out
        assert os.path.isfile(os.path.join(arch, "folder", "plan.md")) and os.path.isdir(os.path.join(arch, "folder", "assets"))
        assert not os.path.exists(os.path.join(proj, "plans", "migplan"))
        out = mk("delete", "migplan", "--data", mig_data, "--home", home)
        assert out["ok"] and os.path.dirname(out["backup"]) == os.path.join(proj, "archive"), out
        assert os.path.isfile(os.path.join(arch, "folder", "plan.md")), "delete unlinked the archived folder"
        # nothing was written under the repo's .matrixkoder
        assert tree_hash(mig_data) == before, "a migrated mk-plan wrote under the repo .matrixkoder"

        # F.2 approve — counts, the frozen gate fields (a change is a new version naming the field)
        # and the --user-ack refusal. db and mkpaths are already in scope here.
        f2_approve_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths)

        # C1: the plan-level `description` rides header_json when it is a non-empty string, else
        # the key is absent (a plan without one is unchanged)
        base = {"runs": [], "gates": {}, "final_gates": [], "phases": []}
        assert json.loads(header_json_of(dict(base, description="Why this plan"), "s", "b", "draft")
                          )["description"] == "Why this plan"
        for none in ({}, {"description": ""}, {"description": "  "}, {"description": 5}):
            assert "description" not in json.loads(header_json_of(dict(base, **none), "s", "b", "draft"))

        # STORE: versioned plan_items, status/runs in the store, item set
        store_self_test(tmp, mk, write, plan_text, phase, task, db, mkpaths)

        # bugs-tab: plan id `bugs` is reserved for the permanent bugs run — check and import refuse it
        bp = write("bugs-plan.md", plan_text(plan='plan = "bugs"', phases=phase("P1", tasks=task("P1.1")) + "\n"))
        got = mk("check", bp, code=2)
        assert RESERVED_PLAN_ERR in got["errors"], got
        bdata = os.path.join(tmp, "bugs-plan-data")
        db.open_project(home, data_dir=bdata).close()  # a real store: the refusal is not "no store"
        got = mk("import", bp, "--data", bdata, "--home", home, code=2)
        assert RESERVED_PLAN_ERR in got["errors"], got
        bcon = db.open_project(home, data_dir=bdata)
        try:
            assert bcon.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0, "bugs plan imported"
        finally:
            bcon.close()

        # T1 — the plan file above parses to its 9 phases, 19 tasks, 5 goals, 12 risks, 8 questions and 2 final gates
        plan_path = os.path.join(root, "scripts", "fixtures", "plan-blocks.md")
        out_plan = mk("parse", plan_path)
        assert out_plan["ok"] and out_plan["plan"] == "plan-format-refactor"
        assert len(out_plan["phases"]) == 9
        total_tasks = sum(len(p["tasks"]) for p in out_plan["phases"])
        assert total_tasks == 19, total_tasks
        expected_task_ids = [
            ["PARSE.1", "PARSE.2", "PARSE.3"],
            ["GATEKEY.1", "GATEKEY.2"],
            ["CHECK.1", "CHECK.2", "CHECK.3"],
            ["STORE.1", "STORE.2", "STORE.3"],
            ["CONSUM.1", "CONSUM.2"],
            ["TSFIX.1"],
            ["RUN.1", "RUN.2"],
            ["TSNEW.1"],
            ["DOCS.1", "DOCS.2"],
        ]
        assert [[t["id"] for t in p["tasks"]] for p in out_plan["phases"]] == expected_task_ids
        assert len(out_plan["goals"]) == 5
        assert len(out_plan["risks"]) == 12
        assert len(out_plan["questions"]) == 8
        assert len(out_plan["final_gates"]) == 2
        assert len(out_plan["use_gates"]) == 2
        assert out_plan["serves"]["PARSE"] == ["G1", "G2"]
        assert out_plan["covers"]["PARSE.2"] == ["C1", "T1"]
        assert out_plan["covers"]["PARSE.3"] == ["T2"]
        assert out_plan["proves"]["test"] == ["G1", "G2", "G3"]
        assert out_plan["proves"]["typecheck"] == ["G4"]

        # MEDIUM 6: add a case with a 5.n.m.k subtask
        sub_plan_text = """+++ 1 Top block [toml]
plan = "sub-plan"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "Phase 1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "Task 1"
acceptance = "acc1"
/+++

+++ 5.1.1.1 P1.1.1 subtask [toml]
id = "P1.1.1"
title = "Subtask 1"
acceptance = "subacc1"
/+++
"""
        sub_p = write("subtask-test.md", sub_plan_text)
        out_sub = mk("parse", sub_p)
        assert out_sub["ok"]
        p0_t0 = out_sub["phases"][0]["tasks"][0]
        assert len(p0_t0["subtasks"]) == 1
        assert p0_t0["subtasks"][0]["id"] == "P1.1.1"
        assert p0_t0["subtasks"][0]["title"] == "Subtask 1"
        assert p0_t0["subtasks"][0]["acceptance"] == "subacc1"

        # test set --status and add-run edit block 1 only on a block-format plan
        bp_path = os.path.join(tmp, "block-plan-test.md")
        with open(plan_path) as f:
            bp_orig = f.read()
        with open(bp_path, "w") as f:
            f.write(bp_orig)
        bp_set = mk("set", bp_path, "--status", "running")
        assert bp_set["ok"] and bp_set["status"] == "running"
        bp_run = mk("add-run", bp_path, "--run", "run_test1")
        assert bp_run["ok"] and "run_test1" in bp_run["runs"]
        with open(bp_path) as f:
            bp_after = f.read()
        lines_orig = bp_orig.split("\n")
        lines_after = bp_after.split("\n")
        close_orig = [i for i, l in enumerate(lines_orig) if l.strip() == "/+++"][0]
        close_after = [i for i, l in enumerate(lines_after) if l.strip() == "/+++"][0]
        assert lines_orig[close_orig:] == lines_after[close_after:]

        # Test HIGH 1: final_gates only from 6.0 [extra], use_gates carries use gates with cmd=None, legend names not added
        bp_g = write("test-high1.md", """+++ 1 Top block [toml]
plan = "high1"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++

+++ 6 Gates
## Gates
- legend_only — proves G1 — legend gate description
- test — proves G1 — test proves
/+++

+++ 6.0 Gate settings [toml]
use = ["test"]
timeout = 300
[extra]
custom_gate = "echo custom"
/+++
""")
        out_g = mk("parse", bp_g)
        assert out_g["ok"]
        assert "use_gates" in out_g, "use_gates key must exist"
        assert out_g["use_gates"] == [{"id": "test", "cmd": None, "use": True, "every": "run_end"}]
        assert [g["id"] for g in out_g["final_gates"]] == ["custom_gate"]
        assert out_g["final_gates"][0]["cmd"] == "echo custom"
        assert not any(g["id"] == "legend_only" for g in out_g["final_gates"])
        assert not any(g["id"] == "legend_only" for g in out_g["use_gates"])
        chk_g = mk("check", bp_g)
        assert chk_g["ok"] is True, chk_g

        # Test HIGH 2: header passed to check_plan built from raw toml, so missing wave or commit is an error
        bp_no_wave = write("test-high2-wave.md", """+++ 1 Top block [toml]
plan = "high2-wave"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++
""")
        out_no_wave = mk("check", bp_no_wave, code=2)
        assert out_no_wave["ok"] is False
        assert any("no wave key" in e for e in out_no_wave["errors"]), out_no_wave

        bp_no_commit = write("test-high2-commit.md", """+++ 1 Top block [toml]
plan = "high2-commit"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++
""")
        out_no_commit = mk("check", bp_no_commit, code=2)
        assert out_no_commit["ok"] is False
        assert any("no commit key" in e for e in out_no_commit["errors"]), out_no_commit

        # Test MEDIUM 3: blocks looked up by name, not numbers (numbers order/nest 5.x blocks)
        bp_renum = write("test-med3.md", """+++ 10 Top block [toml]
plan = "med3-renum"
status = "draft"
created = "2026-10-24"
/+++

+++ 20 Summary
# med3 title
- Phases: 1
/+++

+++ 30 Goal
## Goal
- G1 — goal one
/+++

+++ 40 Design / context
C1 — contract one
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++

+++ 60 Gates
## Gates
- test — proves G1 — test
/+++

+++ 60.0 Gate settings [toml]
use = ["test"]
/+++

+++ 70 Risks
## Risks
- K1 — risk one — level 2 — guard: test — P1.1
/+++

+++ 80 Open questions
## Open questions
- [ ] Q1 — question one — blocks: none — pick: yes
/+++

+++ 90 Log
- 2026-10-24 — drafted — log one — user
/+++
""")
        out_renum = mk("parse", bp_renum)
        assert out_renum["ok"] is True
        assert len(out_renum["goals"]) == 1 and out_renum["goals"][0]["id"] == "G1"
        assert len(out_renum["items"]) == 1 and out_renum["items"][0]["id"] == "C1"
        assert len(out_renum["risks"]) == 1 and out_renum["risks"][0]["id"] == "K1"
        assert len(out_renum["questions"]) == 1 and out_renum["questions"][0]["id"] == "Q1"
        assert len(out_renum["log"]) == 1 and out_renum["log"][0]["kind"] == "drafted"

        # Test MEDIUM 4: a +++ header before previous /+++, or missing /+++ at EOF, is an error naming line number
        bp_unclosed = write("test-med4-unclosed.md", """+++ 1 Top block [toml]
plan = "unclosed"
+++ 2 Summary
# title
/+++
""")
        out_unclosed = mk("parse", bp_unclosed, code=2)
        assert out_unclosed["ok"] is False
        assert any("line 3:" in e for e in out_unclosed["errors"]), out_unclosed

        bp_eof = write("test-med4-eof.md", """+++ 1 Top block [toml]
plan = "eof"
status = "draft"
""")
        out_eof = mk("parse", bp_eof, code=2)
        assert out_eof["ok"] is False
        assert any("line 4:" in e for e in out_eof["errors"]), out_eof

        # Test MEDIUM 5: phase without subagents uses session_subagents(), not False
        bp_nosub = write("test-med5.md", """+++ 1 Top block [toml]
plan = "med5"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++
""")
        out_nosub = mk("parse", bp_nosub)
        assert out_nosub["phases"][0]["subagents"] is True

        # Test LOW 8: new-format equivalents for dropped/loosened asserts (task gate string -> C1 table, risk value)
        bp_low8 = write("test-low8.md", """+++ 1 Top block [toml]
plan = "low8"
status = "draft"
created = "2026-10-24"
design = []
/+++

+++ 4 Design / context
## Design / context
C1 — Contract for low8
T1 — Test for low8
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
gate = "echo test-gate"
risk = 3
/+++

+++ 5.1.2 P1.2 task [toml]
id = "P1.2"
title = "T2"
acceptance = "a2"
manual = "tested by hand"
/+++
""")
        out_low8 = mk("parse", bp_low8)
        assert len(out_low8["contracts"]) == 1 and out_low8["contracts"][0]["id"] == "C1"
        assert len(out_low8["tests"]) == 1 and out_low8["tests"][0]["id"] == "T1"
        assert out_low8["design"] == []
        t0 = out_low8["phases"][0]["tasks"][0]
        # task gate string -> C1 table equivalent
        assert t0["gate"] == [{"cmd": "echo test-gate", "expect": 0, "timeout": 600, "lines": []}]
        # risk value equivalent
        assert t0["risk"] == 3
        # manual equivalent
        assert any(t["manual"] for t in out_low8["phases"][0]["tasks"])
        # warnings and use_gates equivalents (which fail on fde91aa)
        assert out_low8["warnings"] == []
        assert [g["id"] for g in out_low8["use_gates"]] == ["test", "typecheck"]

        # Test LOW 9: task with no gate and no manual warns 'neither gate nor manual line' regardless of risk level
        bp_low9 = write("test-low9.md", """+++ 1 Top block [toml]
plan = "low9"
status = "draft"
created = "2026-10-24"
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
risk = 1
/+++
""")
        out_low9 = mk("check", bp_low9)
        assert any("neither gate nor manual line" in w for w in out_low9["warnings"]), out_low9["warnings"]

        # Test LOW 10: none in Hazards/Carries/blocks -> []; gate_lines key; phase raw text; warn on patternless lines
        bp_low10 = write("test-low10.md", """+++ 1 Top block [toml]
plan = "low10"
status = "draft"
created = "2026-10-24"
/+++

+++ 3 Goal
## Goal
- G1 — goal
This is an unparseable goal line!
/+++

+++ 5 Phases
## Phases
/+++

+++ 5.1 P1
- Goal: P1
- Hazards: none
- Carries: none
/+++

+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++

+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++

+++ 6 Gates
## Gates
- g1 — proves G1 — line for g1
/+++

+++ 7 Risks
## Risks
- K1 — risk — level 1 — guard: g — P1.1
This is an unparseable risk line!
/+++

+++ 8 Open questions
## Open questions
- [ ] Q1 — question — blocks: none — pick: a
This is an unparseable question line!
/+++
""")
        out_low10 = mk("parse", bp_low10)
        p1 = out_low10["phases"][0]
        assert p1["hazards"] == [], p1["hazards"]
        assert p1["carries"] == [], p1["carries"]
        assert "raw" in p1 and len(p1["raw"]) > 0
        q1 = out_low10["questions"][0]
        assert q1["blocks"] == [], q1["blocks"]
        assert "gate_lines" in out_low10
        assert out_low10["gate_lines"][0] == {"name": "g1", "proves": ["G1"], "text": "line for g1", "raw": "- g1 — proves G1 — line for g1"}
        warns = out_low10["warnings"]
        assert any("Goal block" in w and "unparseable goal line" in w for w in warns), warns
        assert any("Risks block" in w and "unparseable risk line" in w for w in warns), warns
        assert any("Open questions block" in w and "unparseable question line" in w for w in warns), warns

        # Test Round 3 Item 2: fail with line number on bad +++ header, stray /+++, and duplicate block name
        bp_bad_hdr = write("test-round3-item2-badheader.md", """+++ 1 Top block [toml]
plan = "badhdr"
status = "draft"
created = "2026-10-24"
/+++
+++ not-a-valid-header
/+++
""")
        out_bad_hdr = mk("parse", bp_bad_hdr, code=2)
        assert out_bad_hdr["ok"] is False
        assert any("line 6:" in e and "does not match block pattern" in e for e in out_bad_hdr["errors"]), out_bad_hdr

        bp_stray = write("test-round3-item2-strayclose.md", """+++ 1 Top block [toml]
plan = "stray"
status = "draft"
created = "2026-10-24"
/+++
/+++
""")
        out_stray = mk("parse", bp_stray, code=2)
        assert out_stray["ok"] is False
        assert any("line 6:" in e and "stray '/+++'" in e for e in out_stray["errors"]), out_stray

        bp_dup = write("test-round3-item2-dupblock.md", """+++ 1 Top block [toml]
plan = "dup"
status = "draft"
created = "2026-10-24"
/+++
+++ 2 Summary
summary 1
/+++
+++ 3 Summary
summary 2
/+++
""")
        out_dup = mk("parse", bp_dup, code=2)
        assert out_dup["ok"] is False
        assert any("line 9:" in e and "duplicate block name 'Summary'" in e for e in out_dup["errors"]), out_dup

        # Test Round 3 Item 3: by-name fallback lookups must skip 5.x blocks (GATEKEY phase never taken as Gates block)
        bp_gatekey = write("test-round3-item3-gatekey.md", """+++ 1 Top block [toml]
plan = "gatekey-test"
status = "draft"
created = "2026-10-24"
/+++
+++ 5 Phases
/+++
+++ 5.1 P1
- Goal: P1
/+++
+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++
+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++
+++ 5.2 GATEKEY — Gate names agree
- g1 — proves G1 — gatekey phase line
/+++
+++ 5.2.0 GATEKEY settings [toml]
id = "GATEKEY"
title = "GATEKEY"
wave = 1
owned = ["b.py"]
commit = "feat: gatekey"
/+++
+++ 5.2.1 GATEKEY.1 task [toml]
id = "GATEKEY.1"
title = "GK1"
acceptance = "a2"
/+++
""")
        out_gatekey = mk("parse", bp_gatekey)
        assert out_gatekey["ok"] is True
        assert out_gatekey["gate_lines"] == [], out_gatekey["gate_lines"]

        # Test Round 3 Item 4: a gate named in both use and [extra] appears only in final_gates, not in use_gates
        bp_use_extra = write("test-round3-item4-use-extra.md", """+++ 1 Top block [toml]
plan = "use-extra"
status = "draft"
created = "2026-10-24"
/+++
+++ 5 Phases
/+++
+++ 5.1 P1
- Goal: P1
/+++
+++ 5.1.0 P1 settings [toml]
id = "P1"
title = "P1"
wave = 1
owned = ["a.py"]
commit = "feat: p1"
/+++
+++ 5.1.1 P1.1 task [toml]
id = "P1.1"
title = "T1"
acceptance = "a1"
/+++
+++ 6 Gates
/+++
+++ 6.0 Gate settings [toml]
use = ["test", "typecheck"]
[extra]
test = "pytest plugins"
/+++
""")
        out_use_extra = mk("parse", bp_use_extra)
        assert out_use_extra["ok"] is True
        assert "test" in [g["id"] for g in out_use_extra["final_gates"]]
        assert "test" not in [g["id"] for g in out_use_extra["use_gates"]]
        assert [g["id"] for g in out_use_extra["use_gates"]] == ["typecheck"]

        # T2 — copy the three project DBs read-only and assert every stored plan's file or archived version still parses
        import sqlite3, glob
        dbs = sorted(glob.glob(os.path.expanduser("~/.matrixkoder/projects/*/mk-project.db")))
        if not dbs:
            print("note: no project DB exists, skipping T2")
        else:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tmp_main_dir = tempfile.mkdtemp(prefix="main-mk-plan-")
            try:
                main_py = os.path.join(tmp_main_dir, "mk-plan.py")
                with open(main_py, "wb") as f:
                    subprocess.check_call(["git", "show", "main:scripts/mk-plan.py"], cwd=repo_root, stdout=f)
                scripts_dir = os.path.dirname(MK_PLAN_PY)
                for mod in ["mk-config.py", "mkpaths.py", "mkdb.py", "mk-state.py"]:
                    src_mod = os.path.join(scripts_dir, mod)
                    if os.path.exists(src_mod):
                        shutil.copy(src_mod, tmp_main_dir)
                total_stored = 0
                stored_parsed = 0
                fallback_count = 0
                for project_db in dbs:
                    proj_dir = os.path.dirname(project_db)
                    con = sqlite3.connect(f"file:{project_db}?mode=ro", uri=True)
                    rows = con.execute("SELECT id, archived_path, header_json FROM plans").fetchall()
                    total_stored += len(rows)
                    for plan_id, arch, hdr_json in rows:
                        target_file = None
                        if arch and os.path.exists(arch):
                            target_file = arch
                        elif arch and os.path.exists(os.path.join(proj_dir, arch)):
                            target_file = os.path.join(proj_dir, arch)
                        if target_file:
                            p_out = mk("parse", target_file)
                            assert p_out["ok"] is True and p_out["plan"] == plan_id
                            # compare with main's parse output
                            out_main = subprocess.check_output([sys.executable, main_py, "parse", target_file])
                            parsed_main = json.loads(out_main)
                            assert p_out == parsed_main, f"parse mismatch with main for {plan_id}"
                            stored_parsed += 1
                        else:
                            hdr = json.loads(hdr_json)
                            # assert the phases and final_gates shape against the stored json, not only that a "phases" key exists
                            assert "phases" in hdr and isinstance(hdr["phases"], list) and len(hdr["phases"]) > 0
                            assert all(isinstance(p, dict) and "id" in p for p in hdr["phases"])
                            fg = hdr.get("final_gates")
                            if fg is not None:
                                assert isinstance(fg, list)
                                assert all(isinstance(g, dict) and ("covers" in g or "id" in g) for g in fg)
                            fallback_count += 1
                    con.close()
                assert stored_parsed + fallback_count == total_stored, f"expected {total_stored} stored plans, got {stored_parsed} parsed and {fallback_count} fallback"
                assert stored_parsed == 15 and fallback_count == 3, f"expected 15 parsed and 3 fallback, got {stored_parsed} and {fallback_count}"
            finally:
                shutil.rmtree(tmp_main_dir, ignore_errors=True)

        print("mk-plan self-test ok")
    finally:
        restore_data()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    self_test()
