"""The pre-loaded fork server (apps/analytics/zygote.py) keeps the sandbox's guarantees per request."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apps.analytics import sandbox
from apps.analytics.services import AnalysisError

# A minimal engine: same contract as engine.py (limits from JT_ANALYTICS_LIMITS, request on stdin).
ENGINE = textwrap.dedent(
    """
    import json, os, sys, time
    COUNTER = {"requests": 0}

    def main():
        import resource
        limits = json.loads(os.environ.get("JT_ANALYTICS_LIMITS", "{}"))
        memory = int(limits["memory_bytes"])
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        request = json.loads(sys.stdin.buffer.read())
        COUNTER["requests"] += 1
        op = request["op"]
        if op == "sleep":
            time.sleep(30)
        if op == "crash":
            os.kill(os.getpid(), 9)
        if op == "allocate":
            try:
                bytearray(2 * 1024 * 1024 * 1024)
            except MemoryError:
                sys.stdout.write(json.dumps({"ok": False, "kind": "resources", "error": "memory"}))
                return 0
        if op == "write":
            open("left-behind.txt", "w").write("x")
        state = {
            "requests_seen": COUNTER["requests"],
            "cwd": os.getcwd(),
            "tmp": os.environ["TMPDIR"],
            "leftover": os.path.exists("left-behind.txt"),
            "env": sorted(os.environ),
        }
        sys.stdout.write(json.dumps({"ok": True, **state}))
        return 0

    if __name__ == "__main__":
        raise SystemExit(main())
    """
)


@pytest.fixture
def engine_script(tmp_path: Path, settings) -> Path:
    settings.ANALYTICS_SANDBOX_ZYGOTE = True
    script = tmp_path / "test_engine.py"
    script.write_text(ENGINE)
    return script


def test_each_request_is_a_fresh_child_with_its_own_directory(engine_script):
    first = sandbox.run_engine("write", b"", script=engine_script)
    second = sandbox.run_engine("state", b"", script=engine_script)
    # No state survives between requests: module globals and files are per child.
    assert first["requests_seen"] == 1 and second["requests_seen"] == 1
    assert first["cwd"] != second["cwd"] and second["leftover"] is False
    assert first["cwd"] == first["tmp"]
    assert sandbox._zygote_for(engine_script).alive  # both went through the fork server


def test_children_get_no_credentials(engine_script, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "secret")
    names = " ".join(sandbox.run_engine("state", b"", script=engine_script)["env"])
    for secret in ("DATABASE_URL", "DJANGO_SECRET_KEY", "SUPABASE", "GEMINI", "OPENAI"):
        assert secret not in names


def test_time_limit_kills_the_child(engine_script, settings):
    settings.ANALYTICS_SANDBOX_TIMEOUT_SECONDS = 1
    with pytest.raises(AnalysisError, match="time limit"):
        sandbox.run_engine("sleep", b"", script=engine_script)
    # The fork server keeps serving after a child was killed.
    assert sandbox.run_engine("state", b"", script=engine_script)["requests_seen"] == 1


def test_a_crashed_child_is_reported_as_stopped(engine_script):
    with pytest.raises(AnalysisError, match="resource limits"):
        sandbox.run_engine("crash", b"", script=engine_script)


def test_memory_limit_applies_in_the_child(engine_script, settings):
    settings.ANALYTICS_SANDBOX_MEMORY_MB = 1200
    with pytest.raises(AnalysisError, match="memory"):
        sandbox.run_engine("allocate", b"", script=engine_script)
