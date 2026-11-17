"""scripts/backup.sh (backlog Phase 8): backups, rotation and the restore check.

The end-to-end test runs a real dump and restore against a throwaway PostgreSQL cluster when the
server binaries are installed (``pg_config --bindir``); the dry-run tests need only bash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_script_passes_shellcheck():
    result = subprocess.run(["shellcheck", "-x", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_dry_run_dumps_through_the_database_container_and_reads_storage_read_only(tmp_path):
    env = {"BACKUP_DRY_RUN": "1", "BACKUP_DIR": str(tmp_path), "BACKUP_RSYNC_TARGET": "backup@vault:/srv/jt"}
    env.pop("BACKUP_DB_EXEC", None)
    result = _run("production", env=env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    dump = "--env-file .env.production exec -T -u postgres supabase-db pg_dump -U supabase_admin -d postgres"
    assert f"{dump} -Fc" in out
    assert "pg_restore --list" in out  # the dump is verified before it is kept
    assert "--volumes-from \\<supabase-storage\\>:ro" in out and "tar -C /var/lib/storage" in out
    assert "-mtime +14 -delete" in out  # default retention
    assert "rsync -a --chmod=F600" in out and "backup@vault:/srv/jt/" in out
    assert list(tmp_path.iterdir()) == []  # a dry run writes nothing


def test_dry_run_restore_check_uses_a_scratch_database_and_drops_it():
    result = _run("--restore-check", "local", env={"BACKUP_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert "--env-file .env.docker" in result.stdout
    assert "createdb -U supabase_admin jt_restore_check_" in result.stdout
    assert "dropdb -U supabase_admin --if-exists jt_restore_check_" in result.stdout
    # Never restores over the live database.
    assert "-d postgres" not in result.stdout.split("pg_restore", 1)[1]


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        (("staging",), {}, "unknown stack"),
        ((), {"BACKUP_KEEP_DAYS": "two weeks"}, "BACKUP_KEEP_DAYS"),
        (("--restore-check",), {"BACKUP_DB_EXEC": "", "BACKUP_DIR": "/nonexistent-jt"}, "no jt-code-db-"),
    ],
)
def test_bad_input_fails_with_a_message(args, env, message):
    result = _run(*args, env=env)
    assert result.returncode != 0
    assert message in result.stderr


def test_make_targets_and_git_ignore():
    makefile = (ROOT / "Makefile").read_text()
    assert "prod-backup:\n\t@bash scripts/backup.sh production" in makefile
    assert "prod-restore-check:\n\t@bash scripts/backup.sh --restore-check production" in makefile
    assert "backups/" in (ROOT / ".gitignore").read_text().splitlines()
    assert "backups/" in (ROOT / ".dockerignore").read_text().splitlines()


# End to end against a throwaway cluster ---------------------------------------------------------


def _pg_bindir() -> Path | None:
    pg_config = shutil.which("pg_config")
    if pg_config is None:
        return None
    bindir = Path(subprocess.run([pg_config, "--bindir"], capture_output=True, text=True).stdout.strip())
    return bindir if (bindir / "initdb").exists() and (bindir / "pg_ctl").exists() else None


@pytest.fixture
def cluster(tmp_path):
    bindir = _pg_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL server binaries (initdb, pg_ctl) are not installed")
    if os.geteuid() == 0:
        pytest.skip("initdb refuses to run as root")
    data, sock = tmp_path / "data", tmp_path / "sock"
    sock.mkdir()
    initdb = [bindir / "initdb", "-D", data, "-U", "postgres", "--auth=trust"]
    subprocess.run(initdb, check=True, capture_output=True)
    port = str(56000 + os.getpid() % 1000)
    subprocess.run(
        [bindir / "pg_ctl", "-D", data, "-o", f"-p {port} -k {sock} -c listen_addresses=''"]
        + ["-l", tmp_path / "log", "-w", "start"],
        check=True,
        capture_output=True,
    )
    path = f"{bindir}:{os.environ['PATH']}"
    env = {"PGHOST": str(sock), "PGPORT": port, "PGUSER": "postgres", "PATH": path}
    try:
        yield env
    finally:
        subprocess.run([bindir / "pg_ctl", "-D", data, "-m", "immediate", "stop"], capture_output=True)


def _psql(env: dict[str, str], sql: str, database: str = "postgres") -> str:
    return subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tAq", "-d", database, "-c", sql],
        env={**os.environ, **env},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_backup_rotation_and_restore_check_end_to_end(cluster, tmp_path):
    _psql(
        cluster,
        "create schema auth; create schema storage;"
        "create table public.django_migrations (id serial primary key, name text);"
        "insert into public.django_migrations (name) values ('0001'), ('0002');"
        "create table public.identity_user (id serial primary key, email text);"
        "insert into public.identity_user (email) values ('ada@example.com');"
        "create table public.identity_organization (id serial primary key, name text);"
        "insert into public.identity_organization (name) values ('Team');"
        "create table auth.users (id serial primary key, encrypted_password text);"
        "insert into auth.users (encrypted_password) values ('$2a$hash');",
    )
    storage, backups = tmp_path / "storage", tmp_path / "backups"
    (storage / "bucket").mkdir(parents=True)
    (storage / "bucket" / "report.pdf").write_bytes(b"%PDF-1.7")
    backups.mkdir()
    stale = backups / "jt-code-db-20200101T000000Z.dump"
    stale.write_bytes(b"old")
    unrelated = backups / "notes.txt"
    unrelated.write_text("keep me")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    env = {**cluster, "BACKUP_DB_EXEC": "", "BACKUP_STORAGE_DIR": str(storage), "BACKUP_DIR": str(backups)}

    backup = _run(env=env)
    assert backup.returncode == 0, backup.stderr
    dumps = sorted(backups.glob("jt-code-db-*.dump"))
    archives = sorted(backups.glob("jt-code-storage-*.tar.gz"))
    assert len(dumps) == 1 and len(archives) == 1 and not stale.exists() and unrelated.exists()
    assert (backups / f"{dumps[0].name}.sha256").exists()
    assert oct(dumps[0].stat().st_mode & 0o777) == "0o600" and oct(backups.stat().st_mode & 0o777) == "0o700"
    listing = subprocess.run(["tar", "-tzf", archives[0]], capture_output=True, text=True, check=True).stdout
    assert "./bucket/report.pdf" in listing

    _psql(cluster, "insert into public.identity_user (email) values ('grace@example.com')")
    check = _run("--restore-check", env=env)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "public.identity_user: 1 in the backup, 2 now" in check.stdout
    assert _psql(cluster, "select count(*) from pg_database where datname like 'jt_restore_check_%'") == "0"

    # A dump from another release (different migration history) fails the check.
    _psql(cluster, "insert into public.django_migrations (name) values ('0003')")
    mismatch = _run("--restore-check", env=env)
    assert mismatch.returncode == 1 and "FAIL: django_migrations differs" in mismatch.stdout
    assert _psql(cluster, "select count(*) from pg_database where datname like 'jt_restore_check_%'") == "0"

    # A damaged dump is refused before anything is restored.
    with dumps[0].open("ab") as handle:
        handle.write(b"x")
    damaged = _run("--restore-check", env=env)
    assert damaged.returncode != 0 and "does not match its checksum" in damaged.stderr
