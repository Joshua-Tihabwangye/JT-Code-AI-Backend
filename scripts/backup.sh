#!/usr/bin/env bash
# Back up the self-hosted stack, or prove the newest backup restores (backlog P8-1, P8-2).
#
#   scripts/backup.sh [production|local]                    make prod-backup
#   scripts/backup.sh --restore-check [production|local]    make prod-restore-check
#
# A backup is two files plus their SHA-256 sums, in BACKUP_DIR (mode 700, files 600):
#   jt-code-db-<UTC stamp>.dump        pg_dump -Fc of the whole database (public, auth, storage, viz_data)
#   jt-code-storage-<UTC stamp>.tar.gz the Supabase Storage volume (every uploaded file)
# The database is dumped first, so every file the dump refers to already exists when the files are
# copied (a file uploaded in between is an orphan, never a dangling reference). The dump is checked
# with pg_restore --list before it replaces anything. Files older than BACKUP_KEEP_DAYS are deleted,
# and BACKUP_RSYNC_TARGET (user@host:/path) gets a copy off this server.
#
# --restore-check restores the newest dump into a scratch database next to the live one
# (jt_restore_check_<stamp>, dropped afterwards), then compares row counts with the live database:
# Django's migration history must match exactly and the core identity tables must not be empty.
# It needs free disk space for one more copy of the database.
#
# Settings (environment): BACKUP_DIR (default ./backups), BACKUP_KEEP_DAYS (default 14),
# BACKUP_RSYNC_TARGET (default empty), BACKUP_DRY_RUN=1 (print the commands, change nothing).
# For tests and non-Docker hosts: BACKUP_DB_EXEC (command prefix that runs pg_dump/psql; empty =
# run them here, with the usual PG* variables) and BACKUP_STORAGE_DIR (archive this directory
# instead of the supabase-storage container's volume).
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=backup
if [[ "${1:-}" == "--restore-check" ]]; then
  MODE=restore-check
  shift
fi
STACK="${1:-production}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
BACKUP_RSYNC_TARGET="${BACKUP_RSYNC_TARGET:-}"
DRY_RUN="${BACKUP_DRY_RUN:-0}"
STORAGE_PATH=/var/lib/storage            # FILE_STORAGE_BACKEND_PATH of supabase-storage
HELPER_IMAGE=alpine:3.22                 # reads the storage volume (the storage image may lack tar)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log() { printf '[backup] %s\n' "$*" >&2; }
die() { printf '[backup] error: %s\n' "$*" >&2; exit 1; }

[[ "$BACKUP_KEEP_DAYS" =~ ^[0-9]+$ ]] || die "BACKUP_KEEP_DAYS must be a whole number of days."
case "$STACK" in production | local) ;; *) die "unknown stack '$STACK' (expected production or local)." ;; esac

# --- how to reach the database and the storage volume --------------------------------------------
if [[ -n "${BACKUP_DB_EXEC+set}" ]]; then
  read -r -a DB_EXEC <<< "$BACKUP_DB_EXEC"   # explicit prefix (empty: run the tools on this host)
  DB_USER="${PGUSER:-postgres}"
else
  # shellcheck source=scripts/compose.sh
  JT_COMPOSE_SOURCE_ONLY=1 source "$ROOT/scripts/compose.sh"
  compose_args "$STACK"
  if [[ "$DRY_RUN" == 1 ]]; then
    COMPOSE_COMMAND=(docker compose)
    DOCKER_COMMAND=(docker)
  else
    compose_command
  fi
  [[ "$DRY_RUN" == 1 || -f "$ROOT/$COMPOSE_ENV_FILE" ]] || die "$COMPOSE_ENV_FILE not found in $ROOT."
  # The OS user postgres reaches the superuser over the container's local socket (no password needed).
  DB_EXEC=("${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" exec -T -u postgres supabase-db)
  DB_USER=supabase_admin
fi

db() { (cd "$ROOT" && "${DB_EXEC[@]}" "$@"); }
psql_db() { db psql -U "$DB_USER" -X -v ON_ERROR_STOP=1 -tAq "$@"; }

pick_db_user() {
  [[ -n "${BACKUP_DB_EXEC+set}" || "$DRY_RUN" == 1 ]] && return 0
  # Supabase images have supabase_admin (owns the extension schemas); fall back to postgres.
  if ! psql_db -d postgres -c 'select 1' >/dev/null 2>&1; then
    DB_USER=postgres
    psql_db -d postgres -c 'select 1' >/dev/null 2>&1 || die "cannot reach the database (is supabase-db running?)."
  fi
}

say() {  # print a command in dry-run mode
  printf '+'
  printf ' %q' "$@"
  printf '\n'
}

# --- backup ----------------------------------------------------------------------------------------
dump_database() {
  local final="$BACKUP_DIR/jt-code-db-$STAMP.dump" partial="$BACKUP_DIR/.jt-code-db-$STAMP.dump.partial"
  if [[ "$DRY_RUN" == 1 ]]; then
    say "${DB_EXEC[@]}" pg_dump -U "$DB_USER" -d postgres -Fc
    say "${DB_EXEC[@]}" pg_restore --list
    return 0
  fi
  log "dumping the database as $DB_USER"
  db pg_dump -U "$DB_USER" -d postgres -Fc > "$partial" || { rm -f "$partial"; die "pg_dump failed."; }
  db pg_restore --list < "$partial" > /dev/null || { rm -f "$partial"; die "the dump is not a readable archive."; }
  mv "$partial" "$final"
  checksum "$final"
  log "database: $(basename "$final") ($(du -h "$final" | cut -f1))"
}

archive_storage() {
  local final="$BACKUP_DIR/jt-code-storage-$STAMP.tar.gz" partial="$BACKUP_DIR/.jt-code-storage-$STAMP.tar.gz.partial"
  if [[ -n "${BACKUP_STORAGE_DIR:-}" ]]; then
    [[ -d "$BACKUP_STORAGE_DIR" ]] || die "BACKUP_STORAGE_DIR $BACKUP_STORAGE_DIR is not a directory."
    if [[ "$DRY_RUN" == 1 ]]; then say tar -C "$BACKUP_STORAGE_DIR" -czf - .; return 0; fi
    tar -C "$BACKUP_STORAGE_DIR" -czf "$partial" . || { rm -f "$partial"; die "archiving storage failed."; }
  else
    local container=""
    if [[ "$DRY_RUN" == 1 ]]; then
      say "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" ps -q supabase-storage
      say "${DOCKER_COMMAND[@]}" run --rm --volumes-from '<supabase-storage>:ro' "$HELPER_IMAGE" tar -C "$STORAGE_PATH" -czf - .
      return 0
    fi
    container="$(cd "$ROOT" && "${COMPOSE_COMMAND[@]}" "${COMPOSE_ARGS[@]}" ps -q supabase-storage)"
    [[ -n "$container" ]] || die "the supabase-storage container is not running."
    log "archiving uploaded files"
    "${DOCKER_COMMAND[@]}" run --rm --volumes-from "$container:ro" "$HELPER_IMAGE" \
      tar -C "$STORAGE_PATH" -czf - . > "$partial" || { rm -f "$partial"; die "archiving storage failed."; }
  fi
  gzip -t "$partial" || { rm -f "$partial"; die "the storage archive is corrupt."; }
  mv "$partial" "$final"
  checksum "$final"
  log "storage: $(basename "$final") ($(du -h "$final" | cut -f1))"
}

checksum() {
  (cd "$(dirname "$1")" && sha256sum "$(basename "$1")" > "$(basename "$1").sha256")
}

rotate() {
  if [[ "$DRY_RUN" == 1 ]]; then
    say find "$BACKUP_DIR" -maxdepth 1 -type f -name 'jt-code-*' -mtime "+$BACKUP_KEEP_DAYS" -delete
    return 0
  fi
  local removed
  removed="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'jt-code-*' -mtime "+$BACKUP_KEEP_DAYS" -print -delete | wc -l)"
  log "rotation: removed $removed file(s) older than $BACKUP_KEEP_DAYS days"
}

copy_off_host() {
  [[ -n "$BACKUP_RSYNC_TARGET" ]] || { log "BACKUP_RSYNC_TARGET is empty: the backup stays on this server only."; return 0; }
  local files=("$BACKUP_DIR"/jt-code-*-"$STAMP".*)
  if [[ "$DRY_RUN" == 1 ]]; then say rsync -a --chmod=F600 "${files[@]}" "$BACKUP_RSYNC_TARGET/"; return 0; fi
  command -v rsync > /dev/null || die "rsync is not installed (needed for BACKUP_RSYNC_TARGET)."
  rsync -a --chmod=F600 "${files[@]}" "$BACKUP_RSYNC_TARGET/" || die "copy to $BACKUP_RSYNC_TARGET failed."
  log "copied off-host to $BACKUP_RSYNC_TARGET"
}

run_backup() {
  [[ "$DRY_RUN" == 1 ]] || mkdir -p "$BACKUP_DIR"
  [[ "$DRY_RUN" == 1 ]] || chmod 700 "$BACKUP_DIR"
  pick_db_user
  dump_database
  archive_storage
  rotate
  copy_off_host
  log "done: $STAMP"
}

# --- restore check ---------------------------------------------------------------------------------
# Row counts of every base table in the schemas the app owns, as "schema.table<TAB>rows".
COUNT_SQL="
create temporary table jt_counts (name text, n bigint);
do \$\$
declare t record; c bigint;
begin
  for t in select table_schema, table_name from information_schema.tables
           where table_type = 'BASE TABLE' and table_schema in ('public', 'auth', 'storage')
  loop
    execute format('select count(*) from %I.%I', t.table_schema, t.table_name) into c;
    insert into jt_counts values (t.table_schema || '.' || t.table_name, c);
  end loop;
end \$\$;
select name || chr(9) || n from jt_counts order by name;
"

SCRATCH_DB="jt_restore_check_${STAMP,,}"

drop_scratch() { db dropdb -U "$DB_USER" --if-exists "$SCRATCH_DB" >/dev/null 2>&1 || true; }

run_restore_check() {
  local dump scratch="$SCRATCH_DB"
  dump="$( (find "$BACKUP_DIR" -maxdepth 1 -type f -name 'jt-code-db-*.dump' 2>/dev/null || true) | sort | tail -n 1)"
  if [[ "$DRY_RUN" == 1 ]]; then
    say "${DB_EXEC[@]}" createdb -U "$DB_USER" "$scratch"
    say "${DB_EXEC[@]}" pg_restore -U "$DB_USER" -d "$scratch" --no-owner --no-privileges
    say "${DB_EXEC[@]}" dropdb -U "$DB_USER" --if-exists "$scratch"
    return 0
  fi
  [[ -n "$dump" ]] || die "no jt-code-db-*.dump in $BACKUP_DIR (run make prod-backup first)."
  if [[ -f "$dump.sha256" ]]; then
    (cd "$(dirname "$dump")" && sha256sum --quiet -c "$(basename "$dump").sha256") || die "$(basename "$dump") does not match its checksum."
  fi
  pick_db_user
  log "restoring $(basename "$dump") into scratch database $scratch"
  trap drop_scratch EXIT  # also on failure or Ctrl-C
  db createdb -U "$DB_USER" "$scratch"
  local errors=0
  # Supabase-managed objects (event triggers, extension internals) may report errors in a scratch
  # database; the counts below decide whether the application data came back.
  db pg_restore -U "$DB_USER" -d "$scratch" --no-owner --no-privileges < "$dump" 2> "$BACKUP_DIR/.restore-check.log" || errors=$?
  [[ "$errors" == 0 ]] || log "pg_restore reported problems (see $BACKUP_DIR/.restore-check.log); checking the data"

  local restored live
  restored="$(psql_db -d "$scratch" -c "$COUNT_SQL")"
  live="$(psql_db -d postgres -c "$COUNT_SQL")"
  python3 - "$restored" "$live" <<'PY'
import sys

def parse(text):
    rows = {}
    for line in text.splitlines():
        if "\t" in line:
            name, n = line.rsplit("\t", 1)
            rows[name] = int(n)
    return rows

restored, live = parse(sys.argv[1]), parse(sys.argv[2])
problems = []
for table in ("public.django_migrations", "public.identity_user", "public.identity_organization", "auth.users"):
    if table in live and not restored.get(table):
        problems.append(f"{table} is missing or empty in the restore")
if restored.get("public.django_migrations") != live.get("public.django_migrations"):
    problems.append("django_migrations differs from the live database (dump from another release?)")
missing = sorted(set(live) - set(restored))
print(f"restored {len(restored)} tables, {sum(restored.values())} rows; live has {len(live)} tables")
for name in missing[:20]:
    print(f"  not in the backup: {name}")
changed = [(n, restored[n], live[n]) for n in sorted(set(live) & set(restored)) if restored[n] != live[n]]
for name, before, now in changed[:20]:
    print(f"  {name}: {before} in the backup, {now} now (changed since the backup)")
for problem in problems:
    print(f"FAIL: {problem}")
sys.exit(1 if problems else 0)
PY
  log "restore check passed: $(basename "$dump")"
}

if [[ "$MODE" == restore-check ]]; then run_restore_check; else run_backup; fi
