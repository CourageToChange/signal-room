#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_ROOT=/opt/signal-room
readonly RELEASE_ROOT="$INSTALL_ROOT/releases"
readonly CURRENT_LINK="$INSTALL_ROOT/current"
readonly STATE_DB=/var/lib/signal-room/signal-room.sqlite3
readonly PRIVATE_ORIGIN=https://signal.noorfamily.uk
readonly INSTALL_LOCK=/run/signal-room-install.lock

usage() {
  echo "usage: sudo install-release.sh <verified-private-bundle> <config.yaml> <runbooks.yaml>" >&2
  exit 64
}

[[ "$EUID" -eq 0 && "$#" -eq 3 ]] || usage
INSTALL_UMASK="$(umask)"
umask 077
exec 9>"$INSTALL_LOCK"
umask "$INSTALL_UMASK"
chmod 0600 "$INSTALL_LOCK"
flock --exclusive --nonblock 9 || {
  echo "another Signal Room installation is already running" >&2
  exit 75
}
SOURCE="$(readlink -f -- "$1")"
CONFIG="$(readlink -f -- "$2")"
RUNBOOKS="$(readlink -f -- "$3")"
[[ -d "$SOURCE" && -f "$CONFIG" && -f "$RUNBOOKS" ]] || usage
[[ "$(tr -d '\r\n' < "$SOURCE/BUNDLE_KIND" 2>/dev/null || true)" == private ]] || {
  echo "installer accepts only a private bundle" >&2
  exit 65
}

if find "$SOURCE" -type l -print -quit | grep -q .; then
  echo "release bundle contains a symlink" >&2
  exit 65
fi
if find "$SOURCE" \( ! -user root -o -perm /022 \) -print -quit | grep -q .; then
  echo "release bundle must be root-owned and not writable by group or other" >&2
  exit 65
fi
if find "$CONFIG" "$RUNBOOKS" -maxdepth 0 \
  \( ! -user root -o -perm /022 \) -print -quit | grep -q .; then
  echo "configuration files must be root-owned and not writable by group or other" >&2
  exit 65
fi
while IFS= read -r line; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[0-9a-f]{64}\ \ [A-Za-z0-9._/+@=-]+$ ]] || {
    echo "unsafe SHA256SUMS entry" >&2
    exit 65
  }
  relative="${line:66}"
  [[ "$relative" != /* && "$relative" != *".."* && "$relative" != *\\* ]] || {
    echo "unsafe manifest path: $relative" >&2
    exit 65
  }
done < "$SOURCE/SHA256SUMS"
(cd "$SOURCE" && sha256sum --strict --check SHA256SUMS)
python3 "$SOURCE/deploy/verify-release.py" "$SOURCE"

VERSION="$(tr -d '\r\n' < "$SOURCE/VERSION")"
BUILD_SHA="$(tr -d '\r\n' < "$SOURCE/BUILD_SHA")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$BUILD_SHA" =~ ^[0-9a-f]{40,64}$ ]] || {
  echo "invalid release identity" >&2
  exit 65
}

for user in \
  signal-room-core signal-room-collector signal-room-web signal-room-notifier; do
  id -u "$user" >/dev/null 2>&1 || {
    echo "required service user is missing: $user" >&2
    exit 65
  }
done
for group in \
  signal-room signal-room-core signal-room-collector signal-room-web signal-room-notifier \
  signal-room-query signal-room-ingest signal-room-notify; do
  getent group "$group" >/dev/null || {
    echo "required service group is missing: $group" >&2
    exit 65
  }
done

install -d -o signal-room-core -g signal-room-core -m 0700 \
  /var/lib/signal-room /var/backups/signal-room
STATE_DB_EXISTED=0
for state_file in "$STATE_DB" "$STATE_DB-wal" "$STATE_DB-shm"; do
  [[ ! -e "$state_file" ]] && continue
  [[ -f "$state_file" && ! -L "$state_file" ]] || {
    echo "unsafe database state path: $state_file" >&2
    exit 65
  }
  chown signal-room-core:signal-room-core "$state_file"
  chmod 0600 "$state_file"
done
[[ ! -f "$STATE_DB" ]] || STATE_DB_EXISTED=1

install -d -o root -g root -m 0755 "$RELEASE_ROOT"
TARGET="$RELEASE_ROOT/$VERSION-$BUILD_SHA"
STAGE="$TARGET.partial.$$"
[[ ! -e "$TARGET" && ! -e "$STAGE" ]] || {
  echo "immutable release already exists: $TARGET" >&2
  exit 73
}
case "$STAGE" in
  "$RELEASE_ROOT"/*.partial.*) ;;
  *) echo "unsafe staging path" >&2; exit 70 ;;
esac

PREVIOUS=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS="$(readlink -f -- "$CURRENT_LINK")"
  case "$PREVIOUS" in
    "$RELEASE_ROOT"/*) ;;
    *) echo "current release points outside the immutable release root" >&2; exit 65 ;;
  esac
  [[ -d "$PREVIOUS" && ! -L "$PREVIOUS" ]] || {
    echo "current release target is not a safe directory" >&2
    exit 65
  }
elif [[ -e "$CURRENT_LINK" ]]; then
  echo "current release path is not a symlink" >&2
  exit 65
fi

SMOKE_ROOT=""
CORE_PID=""
WEB_PID=""
UNIT_NEXT=""
ENABLE_NEXT=""
NEXT_LINK=""
ROLLBACK_LINK=""
ROLLBACK_DB=""
TARGET_CREATED=0
ACTIVATION_PENDING=0
NEW_SERVICES_MAY_HAVE_RUN=0
SNAPSHOT_READY=0
TARGET_WAS_ACTIVE=0
TIMER_WAS_ACTIVE=0

signal_room_units=(
  signal-room-backup.timer
  signal-room-backup.service
  signal-room.target
  signal-room-migrate.service
  signal-room-core.service
  signal-room-collector.service
  signal-room-web.service
  signal-room-notifier.service
)

stop_release_services() {
  local unit state main_pid
  systemctl stop "${signal_room_units[@]}" || return 1
  for unit in "${signal_room_units[@]}"; do
    state="$(systemctl show --property=ActiveState --value "$unit" 2>/dev/null || true)"
    main_pid="$(systemctl show --property=MainPID --value "$unit" 2>/dev/null || true)"
    case "$state" in
      inactive|failed) ;;
      *) echo "release unit did not stop safely: $unit ($state)" >&2; return 1 ;;
    esac
    [[ -z "$main_pid" || "$main_pid" == 0 ]] || {
      echo "release unit retained a process after stop: $unit ($main_pid)" >&2
      return 1
    }
  done
}

remove_new_database() {
  local state_file
  for state_file in "$STATE_DB-wal" "$STATE_DB-shm" "$STATE_DB"; do
    [[ ! -e "$state_file" && ! -L "$state_file" ]] && continue
    [[ -f "$state_file" && ! -L "$state_file" ]] || {
      echo "refusing to remove unsafe new database path: $state_file" >&2
      return 1
    }
    rm -f -- "$state_file" || return 1
  done
}

remove_release_unit_links_without_previous_files() {
  local unit unit_name link expected
  for unit in "$TARGET"/deploy/*.service "$TARGET"/deploy/*.timer "$TARGET"/deploy/*.target; do
    [[ -f "$unit" ]] || continue
    unit_name="$(basename "$unit")"
    [[ -z "$PREVIOUS" || ! -f "$PREVIOUS/deploy/$unit_name" ]] || continue
    link="/etc/systemd/system/$unit_name"
    expected="$CURRENT_LINK/deploy/$unit_name"
    if [[ -L "$link" && "$(readlink -- "$link")" == "$expected" ]]; then
      rm -f -- "$link" || return 1
    fi
  done
}

remove_enable_link() {
  local link="$1" expected="$2"
  if [[ -L "$link" && "$(readlink -- "$link")" == "$expected" ]]; then
    rm -f -- "$link"
  elif [[ -e "$link" || -L "$link" ]]; then
    echo "refusing to remove an unexpected enablement path: $link" >&2
    return 1
  fi
}

rollback_activation() {
  local failed=0
  echo "release activation failed; restoring the previous code and database" >&2
  stop_release_services || failed=1
  if (( failed )); then
    echo "database rollback was not attempted because release processes may still be running" >&2
    return 1
  fi

  if (( NEW_SERVICES_MAY_HAVE_RUN )); then
    if (( STATE_DB_EXISTED )); then
      if (( ! SNAPSHOT_READY )) || [[ -z "$ROLLBACK_DB" ]]; then
        echo "verified database rollback snapshot is unavailable" >&2
        return 1
      fi
      python3 "$TARGET/deploy/sqlite-release-snapshot.py" \
        restore "$ROLLBACK_DB" "$STATE_DB" || return 1
    else
      remove_new_database || return 1
    fi
  fi

  if [[ -n "$PREVIOUS" ]]; then
    ROLLBACK_LINK="$INSTALL_ROOT/.rollback.$$"
    [[ ! -e "$ROLLBACK_LINK" && ! -L "$ROLLBACK_LINK" ]] || return 1
    ln -s -- "$PREVIOUS" "$ROLLBACK_LINK" || return 1
    mv -Tf -- "$ROLLBACK_LINK" "$CURRENT_LINK" || return 1
    ROLLBACK_LINK=""
  elif [[ -L "$CURRENT_LINK" ]]; then
    [[ "$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)" == "$TARGET" ]] || {
      echo "refusing to remove an unexpected current release link" >&2
      return 1
    }
    rm -f -- "$CURRENT_LINK" || return 1
  elif [[ -e "$CURRENT_LINK" ]]; then
    echo "current release path became unsafe during rollback" >&2
    return 1
  fi

  remove_release_unit_links_without_previous_files || return 1
  if [[ -z "$PREVIOUS" ]]; then
    remove_enable_link \
      /etc/systemd/system/multi-user.target.wants/signal-room.target \
      /etc/systemd/system/signal-room.target || return 1
    remove_enable_link \
      /etc/systemd/system/timers.target.wants/signal-room-backup.timer \
      /etc/systemd/system/signal-room-backup.timer || return 1
  fi
  systemctl daemon-reload || return 1
  systemctl reset-failed "${signal_room_units[@]}" 2>/dev/null || true

  if [[ -n "$PREVIOUS" && "$TARGET_WAS_ACTIVE" == 1 ]]; then
    systemctl start signal-room.target || return 1
    systemctl is-active --quiet signal-room-core.service || return 1
    systemctl is-active --quiet signal-room-collector.service || return 1
    systemctl is-active --quiet signal-room-web.service || return 1
    systemctl is-active --quiet signal-room-notifier.service || return 1
    curl --retry 20 --retry-delay 1 --retry-connrefused \
      --silent --show-error --fail --max-time 5 \
      http://127.0.0.1:8080/api/health/ready >/dev/null || return 1
  fi
  if [[ -n "$PREVIOUS" && "$TIMER_WAS_ACTIVE" == 1 ]]; then
    systemctl start signal-room-backup.timer || return 1
    systemctl is-active --quiet signal-room-backup.timer || return 1
  fi
  ACTIVATION_PENDING=0
}

remove_failed_target() {
  local current=""
  (( TARGET_CREATED )) || return 0
  case "$TARGET" in
    "$RELEASE_ROOT/$VERSION-$BUILD_SHA") ;;
    *) echo "refusing to remove an unexpected failed release path" >&2; return 1 ;;
  esac
  [[ ! -L "$TARGET" ]] || {
    echo "refusing to remove a failed release symlink" >&2
    return 1
  }
  [[ ! -L "$CURRENT_LINK" ]] || current="$(readlink -f -- "$CURRENT_LINK" 2>/dev/null || true)"
  [[ "$current" != "$TARGET" ]] || {
    echo "refusing to remove the selected current release" >&2
    return 1
  }
  [[ ! -d "$TARGET" ]] || rm -rf --one-file-system -- "$TARGET"
}

cleanup_temporary() {
  [[ -z "$WEB_PID" ]] || kill "$WEB_PID" 2>/dev/null || true
  [[ -z "$CORE_PID" ]] || kill "$CORE_PID" 2>/dev/null || true
  [[ -z "$UNIT_NEXT" || ! -L "$UNIT_NEXT" ]] || rm -f -- "$UNIT_NEXT"
  [[ -z "$ENABLE_NEXT" || ! -L "$ENABLE_NEXT" ]] || rm -f -- "$ENABLE_NEXT"
  [[ -z "$NEXT_LINK" || ! -L "$NEXT_LINK" ]] || rm -f -- "$NEXT_LINK"
  [[ -z "$ROLLBACK_LINK" || ! -L "$ROLLBACK_LINK" ]] || rm -f -- "$ROLLBACK_LINK"
  if [[ -n "$SMOKE_ROOT" && -d "$SMOKE_ROOT" ]]; then
    case "$SMOKE_ROOT" in
      /var/tmp/signal-room-release.*) rm -rf --one-file-system -- "$SMOKE_ROOT" ;;
      *) echo "refusing to remove an unexpected smoke-test path" >&2 ;;
    esac
  fi
  if [[ -d "$STAGE" ]]; then
    case "$STAGE" in
      "$RELEASE_ROOT/$VERSION-$BUILD_SHA.partial.$$") rm -rf --one-file-system -- "$STAGE" ;;
      *) echo "refusing to remove an unexpected staging path" >&2 ;;
    esac
  fi
}

on_exit() {
  local status=$? rollback_status=0
  trap - EXIT HUP INT TERM
  set +e
  if (( ACTIVATION_PENDING )); then
    rollback_activation || rollback_status=1
  fi
  if (( ! rollback_status )); then
    remove_failed_target || rollback_status=1
  fi
  cleanup_temporary
  if (( rollback_status )); then
    echo "CRITICAL: automatic release rollback did not complete; leave the target container isolated" >&2
    exit 71
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'echo "release installation interrupted" >&2; exit 70' HUP INT TERM

install -d -o root -g root -m 0755 "$STAGE"
cp -a -- "$SOURCE/." "$STAGE/"
python3.13 -m venv "$STAGE/.venv"
"$STAGE/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-index --find-links "$STAGE/wheelhouse" \
  --require-hashes -r "$STAGE/requirements.lock"
mapfile -t application_wheels < <(printf '%s\n' "$STAGE"/wheels/signal_room-*.whl)
[[ "${#application_wheels[@]}" -eq 1 && -f "${application_wheels[0]}" ]] || {
  echo "private bundle must contain exactly one application wheel" >&2
  exit 65
}
"$STAGE/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-index --no-deps "${application_wheels[0]}"

COMMON_ENV=(
  env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin
  SIGNAL_ROOM_CONFIG_PATH="$CONFIG"
  SIGNAL_ROOM_RUNBOOKS_PATH="$RUNBOOKS"
  SIGNAL_ROOM_PUBLIC_ORIGIN="$PRIVATE_ORIGIN"
  SIGNAL_ROOM_TRUSTED_HOSTS=signal.noorfamily.uk
  SIGNAL_ROOM_BUILD_SHA="$BUILD_SHA"
)
(
  cd "$STAGE"
  "${COMMON_ENV[@]}" \
    SIGNAL_ROOM_ENVIRONMENT=production \
    SIGNAL_ROOM_RUNTIME_ROLE=maintenance \
    SIGNAL_ROOM_MODE=live \
    SIGNAL_ROOM_DB_PATH="$STATE_DB" \
    "$STAGE/.venv/bin/signal-room" validate-config \
    --schema-output "$STAGE/runtime-config-schema.json"
)
cmp --silent "$STAGE/config-schema.json" "$STAGE/runtime-config-schema.json" || {
  echo "bundled configuration schema does not match the application wheel" >&2
  exit 65
}
rm -- "$STAGE/runtime-config-schema.json"

SMOKE_ROOT="$(mktemp -d /var/tmp/signal-room-release.XXXXXX)"
TEST_DB="$SMOKE_ROOT/migration-test.sqlite3"
if [[ -f "$STATE_DB" ]]; then
  python3 "$STAGE/deploy/sqlite-release-snapshot.py" backup "$STATE_DB" "$TEST_DB"
fi
(
  cd "$STAGE"
  "${COMMON_ENV[@]}" \
    SIGNAL_ROOM_ENVIRONMENT=production \
    SIGNAL_ROOM_RUNTIME_ROLE=maintenance \
    SIGNAL_ROOM_MODE=live \
    SIGNAL_ROOM_DB_PATH="$TEST_DB" \
    "$STAGE/.venv/bin/signal-room" migrate \
    --backup-directory "$SMOKE_ROOT/pre-migration"
)

SMOKE_PORT="${SIGNAL_ROOM_SMOKE_PORT:-18080}"
SMOKE_QUERY="$SMOKE_ROOT/query.sock"
SMOKE_INGEST="$SMOKE_ROOT/ingest.sock"
SMOKE_NOTIFIER="$SMOKE_ROOT/notifier.sock"
SMOKE_MAINTENANCE="$SMOKE_ROOT/maintenance.sock"
SMOKE_ENV=(
  "${COMMON_ENV[@]}"
  SIGNAL_ROOM_ENVIRONMENT=test
  SIGNAL_ROOM_MODE=fixture
  SIGNAL_ROOM_DB_PATH="$TEST_DB"
  SIGNAL_ROOM_QUERY_SOCKET="$SMOKE_QUERY"
  SIGNAL_ROOM_INGEST_SOCKET="$SMOKE_INGEST"
  SIGNAL_ROOM_NOTIFIER_SOCKET="$SMOKE_NOTIFIER"
  SIGNAL_ROOM_MAINTENANCE_SOCKET="$SMOKE_MAINTENANCE"
)
(
  cd "$STAGE"
  "${SMOKE_ENV[@]}" SIGNAL_ROOM_RUNTIME_ROLE=core "$STAGE/.venv/bin/signal-room" core
) >"$SMOKE_ROOT/core.log" 2>&1 &
CORE_PID=$!
for _ in {1..50}; do
  [[ -S "$SMOKE_QUERY" ]] && break
  kill -0 "$CORE_PID" 2>/dev/null || {
    cat "$SMOKE_ROOT/core.log" >&2
    exit 70
  }
  sleep 0.1
done
[[ -S "$SMOKE_QUERY" ]] || { echo "alternate core socket did not start" >&2; exit 70; }
(
  cd "$STAGE"
  "${SMOKE_ENV[@]}" \
    SIGNAL_ROOM_RUNTIME_ROLE=web \
    SIGNAL_ROOM_AUTH_MODE=development \
    SIGNAL_ROOM_STATIC_DIR="$STAGE/web" \
    SIGNAL_ROOM_PUBLIC_ORIGIN="http://127.0.0.1:$SMOKE_PORT" \
    SIGNAL_ROOM_TRUSTED_HOSTS=127.0.0.1 \
    "$STAGE/.venv/bin/signal-room" serve --host 127.0.0.1 --port "$SMOKE_PORT"
) >"$SMOKE_ROOT/web.log" 2>&1 &
WEB_PID=$!
for _ in {1..50}; do
  if curl --silent --show-error --fail \
    "http://127.0.0.1:$SMOKE_PORT/api/health/live" >/dev/null; then
    break
  fi
  kill -0 "$WEB_PID" 2>/dev/null || {
    cat "$SMOKE_ROOT/web.log" >&2
    exit 70
  }
  sleep 0.1
done
curl --silent --show-error --fail \
  "http://127.0.0.1:$SMOKE_PORT/api/v1/bootstrap" >/dev/null
kill "$WEB_PID" "$CORE_PID"
wait "$WEB_PID" "$CORE_PID" 2>/dev/null || true
WEB_PID=""
CORE_PID=""

python3 "$STAGE/deploy/relocate-venv.py" "$STAGE" "$TARGET"
TARGET_CREATED=1
mv -- "$STAGE" "$TARGET"
if systemctl is-active --quiet signal-room.target; then
  TARGET_WAS_ACTIVE=1
fi
if systemctl is-active --quiet signal-room-backup.timer; then
  TIMER_WAS_ACTIVE=1
fi
ACTIVATION_PENDING=1
for unit in "$TARGET"/deploy/*.service "$TARGET"/deploy/*.timer "$TARGET"/deploy/*.target; do
  [[ -f "$unit" ]] || continue
  unit_name="$(basename "$unit")"
  UNIT_NEXT="/etc/systemd/system/.$unit_name.$$"
  ln -s -- "$CURRENT_LINK/deploy/$unit_name" "$UNIT_NEXT"
  mv -Tf -- "$UNIT_NEXT" "/etc/systemd/system/$unit_name"
  UNIT_NEXT=""
done
install -d -o root -g root -m 0755 \
  /etc/systemd/system/multi-user.target.wants \
  /etc/systemd/system/timers.target.wants
ENABLE_NEXT="/etc/systemd/system/multi-user.target.wants/.signal-room.target.$$"
ln -s -- /etc/systemd/system/signal-room.target "$ENABLE_NEXT"
mv -Tf -- "$ENABLE_NEXT" /etc/systemd/system/multi-user.target.wants/signal-room.target
ENABLE_NEXT="/etc/systemd/system/timers.target.wants/.signal-room-backup.timer.$$"
ln -s -- /etc/systemd/system/signal-room-backup.timer "$ENABLE_NEXT"
mv -Tf -- "$ENABLE_NEXT" /etc/systemd/system/timers.target.wants/signal-room-backup.timer
ENABLE_NEXT=""

stop_release_services || {
  echo "could not quiesce the existing release safely" >&2
  exit 70
}
if (( STATE_DB_EXISTED )); then
  ROLLBACK_DB="$SMOKE_ROOT/release-rollback.sqlite3"
  python3 "$TARGET/deploy/sqlite-release-snapshot.py" \
    backup "$STATE_DB" "$ROLLBACK_DB" || exit 70
  SNAPSHOT_READY=1
fi

NEXT_LINK="$INSTALL_ROOT/.current.$$"
[[ ! -e "$NEXT_LINK" && ! -L "$NEXT_LINK" ]] || {
  echo "temporary current-link path already exists" >&2
  exit 70
}
ln -s -- "$TARGET" "$NEXT_LINK"
mv -Tf -- "$NEXT_LINK" "$CURRENT_LINK"
NEXT_LINK=""
systemctl daemon-reload
test "$(systemctl is-enabled signal-room.target 2>/dev/null)" = enabled || exit 70
test "$(systemctl is-enabled signal-room-backup.timer 2>/dev/null)" = enabled || exit 70
systemctl reset-failed "${signal_room_units[@]}" 2>/dev/null || true
NEW_SERVICES_MAY_HAVE_RUN=1
systemctl start signal-room.target || exit 70
systemctl is-active --quiet signal-room-core.service || exit 70
systemctl is-active --quiet signal-room-collector.service || exit 70
systemctl is-active --quiet signal-room-web.service || exit 70
systemctl is-active --quiet signal-room-notifier.service || exit 70
curl --retry 20 --retry-delay 1 --retry-connrefused \
  --silent --show-error --fail --max-time 5 \
  http://127.0.0.1:8080/api/health/live >/dev/null || exit 70
curl --retry 20 --retry-delay 1 --retry-connrefused \
  --silent --show-error --fail --max-time 5 \
  http://127.0.0.1:8080/api/health/ready >/dev/null || exit 70
systemctl start signal-room-backup.timer || exit 70
systemctl is-active --quiet signal-room-backup.timer || exit 70
trap - EXIT HUP INT TERM
ACTIVATION_PENDING=0
TARGET_CREATED=0
cleanup_temporary
echo "installed and activated Signal Room $VERSION ($BUILD_SHA)"
