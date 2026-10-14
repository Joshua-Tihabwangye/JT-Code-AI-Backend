"""The engine guard blocks network, processes and file access outside the sandbox."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - the test starts a fixed interpreter
import sys
import tempfile
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "apps" / "visualize" / "viz_guard.py"

ATTEMPTS = r"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("guard", sys.argv[1])
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
import pandas, numpy, scipy.stats, socket, subprocess  # imports stay allowed
guard.install(temp_dir=sys.argv[2])
results = {}
def attempt(name, action):
    try:
        action(); results[name] = "allowed"
    except PermissionError as exc:
        results[name] = "blocked"
    except Exception as exc:
        results[name] = f"error:{type(exc).__name__}"
attempt("socket", lambda: socket.create_connection(("127.0.0.1", 9), timeout=1))
attempt("dns", lambda: socket.getaddrinfo("example.com", 80))
attempt("subprocess", lambda: subprocess.run(["/bin/true"]))
attempt("system", lambda: os.system("true"))
attempt("fork", lambda: os.fork())
attempt("read_secret", lambda: open("/etc/passwd").read())
attempt("read_project", lambda: open(sys.argv[3]).read())
attempt("write_outside", lambda: open("/tmp/jt-guard-escape.txt", "w").write("x"))
attempt("remove_outside", lambda: os.remove("/tmp/jt-guard-nothing"))
attempt("write_inside", lambda: open(os.path.join(sys.argv[2], "ok.txt"), "w").write("x"))
attempt("read_inside", lambda: open(os.path.join(sys.argv[2], "ok.txt")).read())
attempt("pandas_work", lambda: pandas.DataFrame({"a": [1, 2, 3]}).groupby("a").size().sum())
import pickle
attempt("pickle", lambda: pickle.loads(pickle.dumps(pandas.Timestamp("2024-01-01"))))
seen = []
sys.addaudithook(lambda event, args: seen.append(event))  # silently refused by the guard
open(os.path.join(sys.argv[2], "ok.txt")).read()
results["new_hook"] = "blocked" if not seen else "allowed"
print(json.dumps(results))
"""


@pytest.fixture(scope="module")
def outcome() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="jt-guard-") as workdir:
        completed = subprocess.run(  # nosec B603 - fixed interpreter and script
            [sys.executable, "-I", "-c", ATTEMPTS, str(GUARD), workdir, str(GUARD.parents[2] / "manage.py")],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir,
            env={"PATH": "/usr/bin:/bin", "HOME": workdir, "TMPDIR": workdir, "LANG": "C.UTF-8"},
            check=False,
        )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "action",
    [
        "socket",
        "dns",
        "subprocess",
        "system",
        "fork",
        "read_secret",
        "read_project",
        "write_outside",
        "remove_outside",
        "pickle",
        "new_hook",
    ],
)
def test_guard_blocks_escapes(outcome, action):
    assert outcome[action] == "blocked", outcome


def test_guard_allows_normal_data_work(outcome):
    assert outcome["write_inside"] == "allowed"
    assert outcome["read_inside"] == "allowed"
    assert outcome["pandas_work"] == "allowed"
    assert not Path("/tmp/jt-guard-escape.txt").exists()
