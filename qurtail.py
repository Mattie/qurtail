"""Compress repetitive line-oriented output while preserving meaningful changes."""

from __future__ import annotations

import argparse
from collections import deque
import configparser
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable, TextIO


DEFAULT_HISTORY_SIZE = 100
DEFAULT_SIMILARITY = 0.85
DEFAULT_TAIL_LINES = 10
DEFAULT_SPINNER = "|/-\\"
RC_FILE_NAME = ".qurtailrc"

ANSI_COLORS = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "bright_black": 90,
    "bright_red": 91,
    "bright_green": 92,
    "bright_yellow": 93,
    "bright_blue": 94,
    "bright_magenta": 95,
    "bright_cyan": 96,
    "bright_white": 97,
}

TIMESTAMP_PREFIXES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*",
        r"^\s*\[?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]?\s*",
        r"^\s*[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+",
        r"^\s*\[?\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4}\]?\s*",
    )
)
LOG_LEVEL_PREFIX = re.compile(
    r"^\s*\[?(?:TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\]?"
    r"(?:\s*[:|-]\s*|\s+)",
    re.IGNORECASE,
)
SIGNAL_LEVEL = re.compile(
    r"(?<![A-Za-z])(?:TRACE|DEBUG|INFO(?:RMATION)?|NOTICE|WARN(?:ING)?|ERROR|"
    r"CRITICAL|FATAL|PANIC|ALERT|EMERG(?:ENCY)?)(?![A-Za-z])",
    re.IGNORECASE,
)
HTTP_STATUS = re.compile(r'"\s+([1-5]\d{2})(?:\s|$)')
NAMED_STATUS = re.compile(r"\bstatus(?:_code)?\s*[=:]\s*[\"']?([1-5]\d{2})\b")
SYSLOG_PRIORITY = re.compile(r"^\s*<(\d{1,3})>")
MACOS_LOG_LEVEL = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+"
    r"(Df|D|I|N|E|F)\s"
)
FAILURE_WORD = re.compile(
    r"\b(?:exception|traceback|panic|segfault|failed|failure|denied|refused|"
    r"unavailable|deadlock|crash(?:ed|ing)?)\b",
    re.IGNORECASE,
)
SIGNAL_RANK = {"normal": 0, "warning": 1, "error": 2}
JSON_ERROR_FIELDS = ("error", "Error", "exception", "Exception", "err")
_MISSING = object()


def _status_signal(status: int) -> str:
    """Group an HTTP status into normal, warning, or error traffic."""
    if status >= 500:
        return "error"
    if status >= 400:
        return "warning"
    return "normal"


def _strongest_signal(signals: Iterable[str]) -> str | None:
    """Return the most severe signal found across a structured log record."""
    return max(signals, key=SIGNAL_RANK.__getitem__, default=None)


def _json_field(value: dict[str, object], path: str) -> object:
    """Resolve a dotted field path in a JSON object."""
    if path in value:
        return value[path]

    current: object = value
    for field in path.split("."):
        if not isinstance(current, dict) or field not in current:
            return _MISSING
        current = current[field]
    return current


def _text_values(value: object) -> Iterable[str]:
    """Yield text contained in a selected JSON message value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)


def _has_error_payload(value: object) -> bool:
    """Return whether a conventional JSON error field carries useful failure data."""
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip().casefold()
        return bool(text) and text not in {"0", "false", "none", "null"}
    if isinstance(value, dict):
        return any(_has_error_payload(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_error_payload(item) for item in value)
    return bool(value)


def _json_signal_texts(
    value: dict[str, object], message_field: str | None
) -> tuple[str, ...]:
    """Select message values for free-text severity and failure detection."""
    fields = (
        (message_field,)
        if message_field
        else (
            "message",
            "Message",
            "msg",
            "log",
            "body",
            "Body",
            "event",
            *JSON_ERROR_FIELDS,
        )
    )
    return tuple(
        text
        for field in fields
        if (selected := _json_field(value, field)) is not _MISSING
        for text in _text_values(selected)
    )


def _level_signal(level: object, *, field: str = "") -> str | None:
    """Map common textual and structured log levels to a signal class."""
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        if field.casefold() in {"severitynumber", "severity_number"}:
            if level >= 17:
                return "error"
            if level >= 13:
                return "warning"
            return "normal"
        if level >= 50:
            return "error"
        if level >= 40:
            return "warning"
        if level >= 10:
            return "normal"
        return None

    text = str(level).strip().casefold()
    if text.isdigit():
        return _level_signal(int(text), field=field)
    if text in {
        "error",
        "critical",
        "fatal",
        "panic",
        "alert",
        "emerg",
        "emergency",
    }:
        return "error"
    if text in {"warn", "warning"}:
        return "warning"
    if text in {"trace", "debug", "info", "information", "notice"}:
        return "normal"
    return None


def _signal_class(
    line: str,
    *,
    ignore_levels: bool = False,
    message_field: str | None = None,
) -> str | None:
    """Detect severity changes that should remain visible despite high similarity."""
    signals: list[str] = []
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        value = None

    signal_texts = (line,)
    if isinstance(value, dict):
        if not ignore_levels:
            for field in (
                "SeverityText",
                "severity_text",
                "severityText",
                "SeverityNumber",
                "severity_number",
                "severityNumber",
                "LogLevel",
                "logLevel",
                "LevelDisplayName",
                "levelDisplayName",
                "levelname",
                "severity",
                "level",
            ):
                if field in value:
                    signal = _level_signal(value[field], field=field)
                    if signal:
                        signals.append(signal)

        status_containers = [value]
        status_containers.extend(
            nested
            for field in ("res", "response", "http")
            if isinstance((nested := value.get(field)), dict)
        )
        for container in status_containers:
            for field in ("status", "status_code", "statusCode", "StatusCode"):
                if field in container:
                    try:
                        signals.append(_status_signal(int(container[field])))
                    except (TypeError, ValueError):
                        pass
        if any(
            field in value and _has_error_payload(value[field])
            for field in JSON_ERROR_FIELDS
        ):
            signals.append("error")
        signal_texts = _json_signal_texts(value, message_field)

    for signal_text in signal_texts:
        priority_match = SYSLOG_PRIORITY.match(signal_text)
        if priority_match and not ignore_levels:
            priority = int(priority_match.group(1))
            if priority <= 191:
                severity = priority % 8
                if severity <= 3:
                    signals.append("error")
                elif severity == 4:
                    signals.append("warning")
                else:
                    signals.append("normal")

        status_match = HTTP_STATUS.search(signal_text) or NAMED_STATUS.search(
            signal_text
        )
        if status_match:
            signals.append(_status_signal(int(status_match.group(1))))

        if not ignore_levels:
            macos_level = MACOS_LOG_LEVEL.match(signal_text)
            if macos_level:
                signals.append(
                    "error" if macos_level.group(1) in {"E", "F"} else "normal"
                )
            level_match = SIGNAL_LEVEL.search(signal_text)
            if level_match:
                signal = _level_signal(level_match.group(0))
                if signal:
                    signals.append(signal)
        if FAILURE_WORD.search(signal_text):
            signals.append("error")
    return _strongest_signal(signals)


def _normalize_line(
    line: str,
    *,
    ignore_timestamps: bool,
    ignore_levels: bool,
    ignore_prefixes: tuple[str, ...],
) -> str:
    """Remove configured log metadata before comparing line content."""
    normalized = line.strip()
    for _ in range(4):
        previous = normalized
        if ignore_timestamps:
            for pattern in TIMESTAMP_PREFIXES:
                normalized = pattern.sub("", normalized, count=1)
        if ignore_levels:
            normalized = LOG_LEVEL_PREFIX.sub("", normalized, count=1)
        for prefix in ignore_prefixes:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].lstrip(" \t:|-")
        if normalized == previous:
            break
    return normalized


def _json_comparable(
    line: str,
    *,
    ignore_fields: tuple[str, ...],
    message_field: str | None,
) -> str:
    """Return stable comparable text for a JSON object, or the original line."""
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line

    if not isinstance(value, dict):
        return line
    if message_field:
        message = _json_field(value, message_field)
        if message is not _MISSING:
            return (
                message
                if isinstance(message, str)
                else json.dumps(message, sort_keys=True)
            )

    filtered = {key: item for key, item in value.items() if key not in ignore_fields}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"))


def _colorize(text: str, color: str | None, enabled: bool) -> str:
    """Wrap text in an ANSI color when explicitly configured for a terminal."""
    if not color or not enabled:
        return text
    return f"\x1b[{ANSI_COLORS[color]}m{text}\x1b[0m"


def _parse_rotate_sample(value: object) -> tuple[int | None, float | None]:
    """Parse a repeat-sampling interval expressed as lines or suffixed seconds."""
    if value is None:
        return None, None

    text = str(value).strip().casefold()
    try:
        if text.endswith("s"):
            seconds = float(text[:-1])
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError
            return None, seconds

        count = int(text)
        if count <= 0:
            raise ValueError
        return count, None
    except ValueError as error:
        raise ValueError(
            "rotate_sample must be a positive line count or duration such as '10s'"
        ) from error


class QurTail:
    """Write novel lines in full and represent similar recent lines with a marker."""

    def __init__(
        self,
        output: TextIO,
        *,
        similarity: float = DEFAULT_SIMILARITY,
        history_size: int = DEFAULT_HISTORY_SIZE,
        marker: str = ".",
        mode: str = "dots",
        spinner: str = DEFAULT_SPINNER,
        spinner_color: str | None = None,
        dot_color: str | None = None,
        dot_every: int = 1,
        ignore_timestamps: bool = False,
        ignore_levels: bool = False,
        ignore_prefixes: tuple[str, ...] = (),
        ignore_fields: tuple[str, ...] = (),
        message_field: str | None = None,
        include_regex: str | None = None,
        exclude_regex: str | None = None,
        comparison_lines: Iterable[str] = (),
        rotate_sample: str | int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= similarity <= 1:
            raise ValueError("similarity must be between 0 and 1")
        if history_size < 1:
            raise ValueError("history_size must be at least 1")
        if not marker or "\n" in marker or "\r" in marker:
            raise ValueError("marker must be non-empty and contain no newlines")
        if mode not in {"dots", "spinner", "counts"}:
            raise ValueError("mode must be 'dots', 'spinner', or 'counts'")
        if not spinner or any(character in spinner for character in "\b\r\n"):
            raise ValueError("spinner must be non-empty and contain no control characters")
        if dot_every < 1:
            raise ValueError("dot_every must be at least 1")
        for option, color in (("spinner_color", spinner_color), ("dot_color", dot_color)):
            if color and color not in ANSI_COLORS:
                raise ValueError(f"{option} must be a recognized color name")
        try:
            include_pattern = re.compile(include_regex) if include_regex else None
            exclude_pattern = re.compile(exclude_regex) if exclude_regex else None
        except re.error as error:
            raise ValueError(f"invalid filter regex: {error}") from error
        rotate_sample_count, rotate_sample_seconds = _parse_rotate_sample(
            rotate_sample
        )

        self._output = output
        self._similarity = similarity
        self._history: deque[tuple[str | None, str]] = deque(maxlen=history_size)
        self._marker = marker
        self._mode = mode
        self._spinner = spinner
        self._spinner_color = spinner_color
        self._dot_color = dot_color
        self._dot_every = dot_every
        self._color_enabled = bool(getattr(output, "isatty", lambda: False)())
        self._spinner_index = 0
        self._repeat_count = 0
        self._repeat_started = 0.0
        self._clock = clock
        self._ignore_timestamps = ignore_timestamps
        self._ignore_levels = ignore_levels
        self._ignore_prefixes = ignore_prefixes
        self._ignore_fields = ignore_fields
        self._message_field = message_field
        self._include_pattern = include_pattern
        self._exclude_pattern = exclude_pattern
        self._rotate_sample_count = rotate_sample_count
        self._rotate_sample_seconds = rotate_sample_seconds
        self._references = tuple(
            self._comparison_key(line.rstrip("\r\n")) for line in comparison_lines
        )
        self._markers_open = False

    def _comparable(self, line: str) -> str:
        """Build the normalized representation used for similarity comparisons."""
        structured = _json_comparable(
            line,
            ignore_fields=self._ignore_fields,
            message_field=self._message_field,
        )
        return _normalize_line(
            structured,
            ignore_timestamps=self._ignore_timestamps,
            ignore_levels=self._ignore_levels,
            ignore_prefixes=self._ignore_prefixes,
        )

    def _comparison_key(self, line: str) -> tuple[str | None, str]:
        """Keep severity transitions separate from normalized line content."""
        signal = _signal_class(
            line,
            ignore_levels=self._ignore_levels,
            message_field=self._message_field,
        )
        return signal, self._comparable(line)

    def process(self, line: str) -> None:
        """Process one input line and write its full or compressed representation."""
        printable = line.rstrip("\r\n")
        if self._include_pattern and not self._include_pattern.search(printable):
            return
        if self._exclude_pattern and self._exclude_pattern.search(printable):
            return

        signal, comparable = self._comparison_key(printable)
        repeated = any(
            signal == previous_signal
            and SequenceMatcher(None, comparable, previous).ratio() >= self._similarity
            for previous_signal, previous in self._history
        ) or any(
            signal == reference_signal
            and SequenceMatcher(None, comparable, reference).ratio() >= self._similarity
            for reference_signal, reference in self._references
        )
        self._history.append((signal, comparable))

        if repeated:
            now = self._clock()
            if self._repeat_count == 0:
                self._repeat_started = now
            self._repeat_count += 1
            sample_due = (
                self._rotate_sample_count is not None
                and self._repeat_count >= self._rotate_sample_count
            ) or (
                self._rotate_sample_seconds is not None
                and now - self._repeat_started >= self._rotate_sample_seconds
            )
            if sample_due:
                if self._markers_open:
                    self._output.write("\n")
                self._output.write(printable + "\n")
                self._output.flush()
                self._markers_open = False
                self._spinner_index = 0
                self._repeat_count = 0
                return

            if self._mode == "spinner":
                if self._markers_open:
                    self._output.write("\b")
                symbol = self._spinner[self._spinner_index % len(self._spinner)]
                self._output.write(_colorize(symbol, self._spinner_color, self._color_enabled))
                self._spinner_index += 1
                self._markers_open = True
            elif self._mode == "counts":
                if self._markers_open:
                    self._output.write("\r")
                elapsed = int(max(0, now - self._repeat_started))
                noun = "line" if self._repeat_count == 1 else "lines"
                self._output.write(
                    f"[{self._repeat_count} similar {noun}, {elapsed}s]"
                )
                self._markers_open = True
            else:
                if self._repeat_count % self._dot_every == 0:
                    self._output.write(
                        _colorize(self._marker, self._dot_color, self._color_enabled)
                    )
                    self._markers_open = True
            if self._markers_open:
                self._output.flush()
            return

        if self._markers_open:
            self._output.write("\n")
        self._markers_open = False
        self._spinner_index = 0
        self._repeat_count = 0

        self._output.write(printable + "\n")
        self._output.flush()

    def finish(self) -> None:
        """Close an unfinished marker run so the terminal prompt starts on a new line."""
        if self._markers_open:
            self._output.write("\n")
        self._markers_open = False
        self._spinner_index = 0
        self._repeat_count = 0
        self._output.flush()


def compress(lines: Iterable[str], output: TextIO, **options: object) -> None:
    """Compress a finite iterable of lines into the provided text stream."""
    tail = QurTail(output, **options)
    for line in lines:
        tail.process(line)
    tail.finish()


def _follow_file(path: Path, tail: QurTail, poll_interval: float) -> None:
    """Follow a path across appends, truncation, disappearance, and replacement."""
    source: TextIO | None = None
    first_open = True
    try:
        while True:
            if source is None:
                try:
                    source = path.open("r", encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    time.sleep(poll_interval)
                    continue

                if first_open:
                    for line in deque(source, maxlen=DEFAULT_TAIL_LINES):
                        tail.process(line)
                    first_open = False
                else:
                    for line in source:
                        tail.process(line)

            line = source.readline()
            if line:
                tail.process(line)
                continue

            try:
                path_stat = path.stat()
                source_stat = os.fstat(source.fileno())
            except FileNotFoundError:
                time.sleep(poll_interval)
                continue

            if (path_stat.st_dev, path_stat.st_ino) != (
                source_stat.st_dev,
                source_stat.st_ino,
            ):
                source.close()
                source = None
                continue

            if path_stat.st_size < source.tell():
                source.seek(0)
                continue

            time.sleep(poll_interval)
    finally:
        if source is not None:
            source.close()


def _config_paths(argv: list[str]) -> tuple[Path, tuple[Path, ...]]:
    """Resolve layered rc paths before parsing options that use their defaults."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "-c", "--config", type=Path, default=Path.home() / RC_FILE_NAME
    )
    parser.add_argument("-x", "--extra-config", action="append", type=Path, default=[])
    parser.add_argument("-f", "--follow", action="store_true")
    args, _ = parser.parse_known_args(argv)
    return (
        args.config.expanduser(),
        tuple(path.expanduser() for path in args.extra_config),
    )


def _rc_bool(value: str) -> bool:
    """Parse a conventional boolean value from an rc option."""
    normalized = value.casefold()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _rc_string(value: str) -> str:
    """Parse an unquoted rc value or a TOML-compatible quoted string."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped.startswith('"') and stripped.endswith('"'):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid quoted string: {error.msg}") from error
        if isinstance(parsed, str):
            return parsed
    if len(stripped) >= 2 and stripped.startswith("'") and stripped.endswith("'"):
        return stripped[1:-1]
    return value


def _rc_prefixes(value: str) -> tuple[str, ...]:
    """Parse a comma-separated list of literal prefixes."""
    unquoted = _rc_string(value)
    return tuple(prefix.strip() for prefix in unquoted.split(",") if prefix.strip())


def _load_rc(path: Path) -> dict[str, object]:
    """Load supported options from a qurtail rc file when it exists."""
    if not path.exists():
        return {}

    config = configparser.ConfigParser(interpolation=None)
    with path.open("r", encoding="utf-8") as source:
        config.read_file(source)

    if "qurtail" not in config:
        raise ValueError("rc file must contain a [qurtail] section")

    section = config["qurtail"]
    converters = {
        "similarity": float,
        "history": int,
        "marker": _rc_string,
        "mode": _rc_string,
        "spinner": _rc_string,
        "spinner_color": _rc_string,
        "dot_color": _rc_string,
        "dot_every": int,
        "poll_interval": float,
        "ignore_timestamps": _rc_bool,
        "ignore_levels": _rc_bool,
        "ignore_prefixes": _rc_prefixes,
        "include_regex": _rc_string,
        "exclude_regex": _rc_string,
        "ignore_fields": _rc_prefixes,
        "message_field": _rc_string,
        "comparison_file": lambda value: Path(_rc_string(value)),
        "rotate_sample": _rc_string,
    }
    unknown = set(section) - set(converters)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown rc option: {names}")

    try:
        values = {name: converters[name](value) for name, value in section.items()}
    except ValueError as error:
        raise ValueError(f"invalid rc value: {error}") from error

    comparison_file = values.get("comparison_file")
    if isinstance(comparison_file, Path) and not comparison_file.is_absolute():
        values["comparison_file"] = path.parent / comparison_file
    return values


def _build_parser(
    defaults: dict[str, object],
    config_path: Path,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qurtail",
        description="Follow text while compacting lines similar to recent output.",
    )
    parser.add_argument("file", nargs="?", help="file to read; stdin when omitted")
    parser.add_argument("-f", "--follow", action="store_true", help="wait for appended file data")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=config_path,
        metavar="PATH",
        help=f"rc file (default: ~/{RC_FILE_NAME})",
    )
    parser.add_argument(
        "-x",
        "--extra-config",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="additional rc file; may be repeated and later files override earlier ones",
    )
    parser.add_argument(
        "--similarity",
        type=float,
        default=defaults.get("similarity", DEFAULT_SIMILARITY),
        metavar="RATIO",
        help="similarity ratio from 0 to 1 (default: %(default)s)",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=defaults.get("history", DEFAULT_HISTORY_SIZE),
        metavar="LINES",
        help="number of recent lines to remember (default: %(default)s)",
    )
    parser.add_argument(
        "--marker",
        default=defaults.get("marker", "."),
        help="repeat marker (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=("dots", "spinner", "counts"),
        default=defaults.get("mode", "dots"),
        help="repeat display mode (default: %(default)s)",
    )
    parser.add_argument(
        "--spinner",
        default=defaults.get("spinner", DEFAULT_SPINNER),
        help="spinner character sequence (default: %(default)s)",
    )
    parser.add_argument(
        "--spinner-color",
        choices=tuple(ANSI_COLORS),
        default=defaults.get("spinner_color"),
        help="spinner color for terminal output",
    )
    parser.add_argument(
        "--dot-color",
        choices=tuple(ANSI_COLORS),
        default=defaults.get("dot_color"),
        help="dot-marker color for terminal output",
    )
    parser.add_argument(
        "--dot-every",
        type=int,
        default=defaults.get("dot_every", 1),
        metavar="N",
        help="print one dot for every N suppressed lines (default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-timestamps",
        action=argparse.BooleanOptionalAction,
        default=defaults.get("ignore_timestamps", False),
        help="ignore recognized timestamp prefixes when comparing lines",
    )
    parser.add_argument(
        "--ignore-levels",
        action=argparse.BooleanOptionalAction,
        default=defaults.get("ignore_levels", False),
        help="ignore standard log-level prefixes when comparing lines",
    )
    parser.add_argument(
        "--ignore-prefix",
        "--ignore-prefixes",
        action="extend",
        type=_rc_prefixes,
        dest="ignore_prefixes",
        default=list(defaults.get("ignore_prefixes", ())),
        metavar="TEXT",
        help="ignore comma-separated literal line prefixes; may be repeated",
    )
    parser.add_argument(
        "--include-regex",
        default=defaults.get("include_regex"),
        metavar="REGEX",
        help="only process lines matching this regular expression",
    )
    parser.add_argument(
        "--exclude-regex",
        default=defaults.get("exclude_regex"),
        metavar="REGEX",
        help="skip lines matching this regular expression",
    )
    parser.add_argument(
        "--ignore-field",
        action="append",
        dest="ignore_fields",
        default=list(defaults.get("ignore_fields", ())),
        metavar="NAME",
        help="ignore a JSON field when comparing objects; may be repeated",
    )
    parser.add_argument(
        "--message-field",
        default=defaults.get("message_field"),
        metavar="NAME",
        help="compare this JSON field instead of the full object",
    )
    parser.add_argument(
        "--comparison-file",
        type=Path,
        default=defaults.get("comparison_file"),
        metavar="PATH",
        help="suppress lines similar to entries in this reference file",
    )
    parser.add_argument(
        "--rotate-sample",
        default=defaults.get("rotate_sample"),
        metavar="N|SECONDSs",
        help="print every Nth repeat, or sample by time with a value such as 10s",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=defaults.get("poll_interval", 0.2),
        metavar="SECONDS",
        help="follow polling interval (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the qurtail command and return its process exit status."""
    arguments = sys.argv[1:] if argv is None else argv
    config_path, extra_config_paths = _config_paths(arguments)
    defaults: dict[str, object] = {}
    for path in (config_path, *extra_config_paths):
        if path in extra_config_paths and not path.exists():
            raise SystemExit(f"qurtail: {path}: extra config file does not exist")
        try:
            defaults.update(_load_rc(path))
        except (OSError, configparser.Error, ValueError) as error:
            raise SystemExit(f"qurtail: {path}: {error}") from error

    parser = _build_parser(defaults, config_path)
    args = parser.parse_args(arguments)

    if not 0 <= args.similarity <= 1:
        parser.error("--similarity must be between 0 and 1")
    if args.history < 1:
        parser.error("--history must be at least 1")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than 0")
    if args.dot_every < 1:
        parser.error("--dot-every must be at least 1")
    if not args.marker or "\n" in args.marker or "\r" in args.marker:
        parser.error("--marker must be non-empty and contain no newlines")
    if args.mode not in {"dots", "spinner", "counts"}:
        parser.error("--mode must be 'dots', 'spinner', or 'counts'")
    if not args.spinner or any(character in args.spinner for character in "\b\r\n"):
        parser.error("--spinner must be non-empty and contain no control characters")
    if args.follow and not args.file:
        parser.error("--follow requires a file")

    comparison_lines: list[str] = []
    if args.comparison_file:
        try:
            comparison_lines = args.comparison_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as error:
            parser.exit(1, f"qurtail: {args.comparison_file}: {error}\n")

    try:
        tail = QurTail(
            sys.stdout,
            similarity=args.similarity,
            history_size=args.history,
            marker=args.marker,
            mode=args.mode,
            spinner=args.spinner,
            spinner_color=args.spinner_color,
            dot_color=args.dot_color,
            dot_every=args.dot_every,
            ignore_timestamps=args.ignore_timestamps,
            ignore_levels=args.ignore_levels,
            ignore_prefixes=tuple(args.ignore_prefixes),
            ignore_fields=tuple(args.ignore_fields),
            message_field=args.message_field,
            include_regex=args.include_regex,
            exclude_regex=args.exclude_regex,
            comparison_lines=comparison_lines,
            rotate_sample=args.rotate_sample,
        )
    except ValueError as error:
        parser.error(str(error))

    try:
        if args.file:
            path = Path(args.file)
            if args.follow:
                _follow_file(path, tail, args.poll_interval)
            else:
                with path.open("r", encoding="utf-8", errors="replace") as source:
                    for line in deque(source, maxlen=DEFAULT_TAIL_LINES):
                        tail.process(line)
        else:
            for line in sys.stdin:
                tail.process(line)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"qurtail: {error}\n")
    finally:
        tail.finish()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
