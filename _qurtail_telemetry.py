"""Write qurtail's opt-in local command telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import tomllib
from typing import Iterator, Sequence
from uuid import uuid4


CONFIG_READ_LIMIT = 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_FILES = 5
LOCK_RETRY_INTERVAL = 0.01
LOCK_WAIT_SECONDS = 0.25


@dataclass(frozen=True)
class _TelemetryConfig:
    """Hold one invocation's validated telemetry settings."""

    directory: Path
    max_file_bytes: int
    max_files: int


class TelemetrySession:
    """Track one invocation and write its completion at most once."""

    def __init__(
        self,
        config: _TelemetryConfig | None = None,
        *,
        invocation: str = "",
        started_ns: int = 0,
        started: bool = False,
    ) -> None:
        self._config = config
        self._invocation = invocation
        self._started_ns = started_ns
        self._started = started
        self._finished = False

    def finish(self, exit_status: int) -> None:
        """Attempt one FINISH event without affecting the command outcome."""
        if self._finished:
            return
        self._finished = True
        if self._config is None or not self._started:
            return

        try:
            duration_ms = max(0, (time.monotonic_ns() - self._started_ns) // 1_000_000)
            record = (
                f"{_utc_timestamp()} format=1 event=FINISH "
                f"invocation={self._invocation} exit={exit_status} "
                f"duration_ms={duration_ms}\n"
            )
            _append_event(self._config, record)
        except Exception:
            return


def start_session(argv: Sequence[str], version: str) -> TelemetrySession:
    """Attempt a START event and return a best-effort lifecycle session."""
    try:
        config = _load_config()
        if config is None:
            return TelemetrySession()

        cwd, cwd_lossy = _unicode_text(os.getcwd())
        safe_argv: list[str] = []
        argv_lossy = False
        for argument in argv:
            safe_argument, argument_lossy = _unicode_text(argument)
            safe_argv.append(safe_argument)
            argv_lossy = argv_lossy or argument_lossy

        invocation = str(uuid4())
        started_ns = time.monotonic_ns()
        record = (
            f"{_utc_timestamp()} format=1 event=START "
            f"invocation={invocation} tool=qurtail version={version} "
            f"pid={os.getpid()} cwd={_json(cwd)} argv={_json(safe_argv)} "
            f"lossy={str(cwd_lossy or argv_lossy).lower()}\n"
        )
        _append_event(config, record)
        return TelemetrySession(
            config,
            invocation=invocation,
            started_ns=started_ns,
            started=True,
        )
    except Exception:
        return TelemetrySession()


def _home_directory() -> Path:
    """Resolve the current user's home directory through the active platform."""
    return Path.home()


def _load_config() -> _TelemetryConfig | None:
    """Load a bounded, valid full-mode configuration or disable telemetry."""
    home = _home_directory()
    config_path = home / ".qurtail" / "config.toml"
    try:
        with config_path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > CONFIG_READ_LIMIT:
                return None
            payload = source.read(CONFIG_READ_LIMIT)
    except FileNotFoundError:
        return None

    document = tomllib.loads(payload.decode("utf-8"))
    telemetry = document.get("telemetry")
    if not isinstance(telemetry, dict):
        return None

    mode = telemetry.get("mode", "off")
    if mode == "off":
        return None
    if mode != "full":
        return None

    max_file_bytes = telemetry.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    max_files = telemetry.get("max_files", DEFAULT_MAX_FILES)
    if not _positive_integer(max_file_bytes) or not _positive_integer(max_files):
        return None

    return _TelemetryConfig(
        directory=home / ".qurtail" / "telemetry",
        max_file_bytes=max_file_bytes,
        max_files=max_files,
    )


def _positive_integer(value: object) -> bool:
    """Reject booleans and non-positive values from integer settings."""
    return type(value) is int and value > 0


def _unicode_text(value: str) -> tuple[str, bool]:
    """Replace surrogate code points that UTF-8 cannot represent."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return (
            "".join(
                "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
                for character in value
            ),
            True,
        )
    return value, False


def _json(value: object) -> str:
    """Encode one compact JSON value for the line-oriented record."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utc_timestamp() -> str:
    """Return a UTC timestamp with a stable millisecond precision."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _append_event(config: _TelemetryConfig, record: str) -> None:
    """Rotate and append one complete record while holding the process lock."""
    directory = config.directory
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _restrict_permissions(directory, 0o700)
    lock_path = directory / "commands.lock"

    with _exclusive_lock(lock_path):
        active_path = directory / "commands.log"
        _prune_archives(directory, config.max_files)
        if active_path.is_file() and active_path.stat().st_size >= config.max_file_bytes:
            _rotate(active_path, config.max_files)

        descriptor = os.open(
            active_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            _restrict_permissions(active_path, 0o600)
            with os.fdopen(descriptor, "ab", closefd=False) as destination:
                destination.write(record.encode("utf-8"))
                destination.flush()
        finally:
            os.close(descriptor)


def _prune_archives(directory: Path, max_files: int) -> None:
    """Remove numeric archives outside the configured total-file retention."""
    for candidate in directory.glob("commands.log.*"):
        suffix = candidate.name.removeprefix("commands.log.")
        if suffix.isdigit() and int(suffix) >= max_files:
            candidate.unlink(missing_ok=True)
        elif suffix.isdigit() and candidate.is_file():
            _restrict_permissions(candidate, 0o600)


def _rotate(active_path: Path, max_files: int) -> None:
    """Move the active log through the bounded numbered archive set."""
    archive_count = max_files - 1
    if archive_count == 0:
        active_path.unlink(missing_ok=True)
        return

    oldest = active_path.with_name(f"commands.log.{archive_count}")
    oldest.unlink(missing_ok=True)
    for number in range(archive_count - 1, 0, -1):
        source = active_path.with_name(f"commands.log.{number}")
        if source.exists():
            source.replace(active_path.with_name(f"commands.log.{number + 1}"))
    active_path.replace(active_path.with_name("commands.log.1"))


def _restrict_permissions(path: Path, mode: int) -> None:
    """Apply private POSIX modes while retaining Windows profile ACLs."""
    if os.name != "nt":
        path.chmod(mode)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Acquire the cross-process lock for at most the configured deadline."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        _restrict_permissions(path, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                _try_lock(descriptor)
                acquired = True
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(LOCK_RETRY_INTERVAL, remaining))
        yield
    finally:
        if acquired:
            _unlock(descriptor)
        os.close(descriptor)


def _try_lock(descriptor: int) -> None:
    """Try the active platform's non-blocking exclusive file lock once."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    # Windows type stubs omit this POSIX-only API even behind the platform branch.
    fcntl.flock(  # type: ignore[attr-defined]
        descriptor,
        fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
    )


def _unlock(descriptor: int) -> None:
    """Release the active platform's file lock."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    # Windows type stubs omit this POSIX-only API even behind the platform branch.
    fcntl.flock(descriptor, fcntl.LOCK_UN)  # type: ignore[attr-defined]
