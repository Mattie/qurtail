"""Follow log streams while compacting conservative, repeated patterns."""

from __future__ import annotations

import argparse
from collections import OrderedDict, deque
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Callable, TextIO


__version__ = "1.0.0"

DEFAULT_TAIL_LINES = 10
DEFAULT_DOT_EVERY = 1
DEFAULT_SUMMARY_INTERVAL = 30.0
DEFAULT_PATTERN_LIMIT = 4096
MAX_SIGNATURE_CHARACTERS = 1024
FOLLOW_POLL_INTERVAL = 0.05
DOT_BATCH_SIZE = 8
DOT_FLUSH_INTERVAL = 1.0
INTERRUPT_GRACE_SECONDS = 2.0
PROCESS_GROUP_POLL_INTERVAL = 0.05
FILE_CHECKPOINT_LIMIT = 8
FILE_TAIL_CHECKPOINT_BYTES = 64 * 1024

_ACCESS_TIMESTAMP = re.compile(
    r"(?<!\d)\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4}"
)
_SYSLOG_TIMESTAMP = re.compile(
    r"(?<![A-Za-z])[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"
)
_LEADING_LEVEL_CLOCK_TIMESTAMP = re.compile(
    r"^(?:\[\s*)?(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:[.,]\d+)?(?:\s*\])?"
    r"(?=\s+(?:TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b)",
    re.IGNORECASE,
)
_MONTH_NAME = re.compile(
    r"(?<![A-Za-z])(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"(?![A-Za-z])"
)
_LEADING_ISO_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?(?:Z|[+-][0-9:]+)?"
)
_UUID = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
_NAMED_ID = re.compile(
    r"\b(?P<id_key>"
    r"request(?:_|-)?id|trace(?:_|-)?id|span(?:_|-)?id|"
    r"correlation(?:_|-)?id|traceparent"
    r")(?P<id_separator>\s*[=:]\s*)(?:[\"']?)"
    r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*(?:[\"']?)",
    re.IGNORECASE,
)
_JSON_ID_FIELDS = {
    "requestid",
    "traceid",
    "spanid",
    "correlationid",
    "traceparent",
}
_JSON_TOP_LEVEL_METADATA = {
    "@timestamp",
    "host",
    "hostname",
    "observedtimeunixnano",
    "pid",
    "time",
    "timecreated",
    "timestamp",
    "timeunixnano",
    "ts",
}
_NAMED_ID_KEYS = (
    "request_id",
    "request-id",
    "requestid",
    "trace_id",
    "trace-id",
    "traceid",
    "span_id",
    "span-id",
    "spanid",
    "correlation_id",
    "correlation-id",
    "correlationid",
    "traceparent",
)
_SIMPLE_NAMED_ID_MARKERS = (
    "request_id=",
    "request-id=",
    "requestid=",
    "trace_id=",
    "trace-id=",
    "traceid=",
    "span_id=",
    "span-id=",
    "spanid=",
    "correlation_id=",
    "correlation-id=",
    "correlationid=",
    "traceparent=",
)
_ID_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*")
_AGGRESSIVE_PREFIXED_HEX = re.compile(
    r"(?<![A-Za-z0-9_])0[xX][0-9A-Fa-f]+(?![A-Za-z0-9_])"
)
_AGGRESSIVE_INTEGER = re.compile(
    r"(?<![A-Za-z0-9_.])[+-]?\d{4,}(?![A-Za-z0-9_.])"
)
_UNQUOTED_PATH_END = frozenset("\"'<>|,;()[]{}")
_DIAGNOSTIC_START = re.compile(
    r"(?:Traceback \(most recent call last\):|Exception in thread|"
    r"^\s*(?:Caused by:|Suppressed:|panic:)|"
    r"^\s*[A-Za-z_$][\w.$]*(?:Error|Exception|Failure)(?::|$))"
)
_DIAGNOSTIC_LINE = re.compile(
    r"^\s+(?:File \"|at\s+|\.\.\.\s+\d+\s+more|\^+\s*$)|"
    r"^\s*(?:Caused by:|Suppressed:)"
)
_RECORD_BOUNDARY = re.compile(
    r"^\s*(?:"
    r"\{|<\d{1,3}>\d?\s|"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|"
    r"\[?\d{2}:\d{2}:\d{2}|"
    r"(?:TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b"
    r")",
    re.IGNORECASE,
)


class _TerminationRequested(BaseException):
    """Turn a termination signal into orderly child-process cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _field_name(name: object) -> str:
    """Normalize a structured field name for conservative metadata matching."""
    return re.sub(r"[^a-z0-9@]", "", str(name).casefold())


def _iso_timestamp_end(text: str, start: int) -> int | None:
    """Return the end of an ISO timestamp at ``start`` using fixed separators."""
    if start < 0 or start + 19 > len(text):
        return None
    candidate = text[start : start + 19]
    if not (
        candidate[0:4].isdigit()
        and candidate[4] == "-"
        and candidate[5:7].isdigit()
        and candidate[7] == "-"
        and candidate[8:10].isdigit()
        and candidate[10] in "T "
        and candidate[11:13].isdigit()
        and candidate[13] == ":"
        and candidate[14:16].isdigit()
        and candidate[16] == ":"
        and candidate[17:19].isdigit()
    ):
        return None

    end = start + 19
    if end < len(text) and text[end] in ".,":
        end += 1
        while end < len(text) and text[end].isdigit():
            end += 1
    if end < len(text) and text[end] == "Z":
        return end + 1
    if end < len(text) and text[end] in "+-":
        zone_end = end + 1
        while zone_end < len(text) and (
            text[zone_end].isdigit() or text[zone_end] == ":"
        ):
            zone_end += 1
        return zone_end
    return end


def _replace_iso_timestamps(text: str) -> tuple[str, bool]:
    """Replace ISO timestamps without scanning the record through a broad regex."""
    pieces: list[str] = []
    cursor = 0
    changed = False
    search_from = 0
    while True:
        separator = text.find("-", search_from)
        if separator < 0:
            break
        start = separator - 4
        end = _iso_timestamp_end(text, start)
        if end is None:
            search_from = separator + 1
            continue
        pieces.extend((text[cursor:start], "<timestamp>"))
        cursor = end
        search_from = end
        changed = True
    if not changed:
        return text, False
    pieces.append(text[cursor:])
    return "".join(pieces), True


def _replace_single_named_id(text: str) -> tuple[str, bool]:
    """Replace one common named ID without running a whole-line regex scan."""
    for marker in _SIMPLE_NAMED_ID_MARKERS:
        start = text.find(marker)
        if start < 0:
            continue
        if start and (text[start - 1].isalnum() or text[start - 1] == "_"):
            break
        if marker == "traceparent=":
            if "id" in text or text.rfind(marker) != start:
                break
        else:
            id_start = start + marker.rfind("id")
            if text.find("id") != id_start or text.rfind("id") != id_start:
                break
        value_start = start + len(marker)
        quoted = value_start < len(text) and text[value_start] in "\"'"
        if quoted:
            value_start += 1
        value = _ID_VALUE.match(text, value_start)
        if value is None:
            break
        value_end = value.end()
        if quoted and value_end < len(text) and text[value_end] in "\"'":
            value_end += 1
        prefix_end = start + len(marker)
        return text[:prefix_end] + "<id>" + text[value_end:], True

    lowered = text.casefold()
    for key in _NAMED_ID_KEYS:
        start = lowered.find(key)
        if start < 0:
            continue
        if key == "traceparent":
            if "id" in lowered or lowered.rfind(key) != start:
                return text, False
        else:
            id_start = start + len(key) - 2
            if (
                lowered.find("id") != id_start
                or lowered.rfind("id") != id_start
                or "traceparent" in lowered
            ):
                return text, False
        if start and (lowered[start - 1].isalnum() or lowered[start - 1] == "_"):
            return text, False
        cursor = start + len(key)
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] not in "=:":
            return text, False
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        prefix_end = cursor
        if cursor < len(text) and text[cursor] in "\"'":
            cursor += 1
        value = _ID_VALUE.match(text, cursor)
        if value is None:
            return text, False
        value_end = value.end()
        if value_end < len(text) and text[value_end] in "\"'":
            value_end += 1
        return text[:prefix_end] + "<id>" + text[value_end:], True
    return text, False


def _absolute_path_kind(text: str, start: int) -> str | None:
    """Identify a Unix, drive-letter, or UNC path at ``start``."""
    if start < 0 or start >= len(text):
        return None
    if start and (
        text[start - 1].isalnum() or text[start - 1] in "_./:\\"
    ):
        return None
    if text[start] == "/" and not text.startswith("//", start):
        return "unix"
    if (
        start + 2 < len(text)
        and text[start].isascii()
        and text[start].isalpha()
        and text[start + 1] == ":"
        and text[start + 2] in "/\\"
    ):
        return "drive"
    if text.startswith("\\\\", start):
        return "unc"
    return None


def _unc_path_has_share(path: str) -> bool:
    """Require both a server and share in a UNC path candidate."""
    remainder = path[2:]
    separator = remainder.find("\\")
    if separator < 1:
        separator = remainder.find("/")
    return separator >= 1 and separator + 1 < len(remainder)


def _absolute_path_end(text: str, start: int, kind: str) -> int | None:
    """Return the end of an absolute path without consuming log punctuation."""
    quote = text[start - 1] if start and text[start - 1] in "\"'" else None
    cursor = start
    while cursor < len(text):
        character = text[cursor]
        if quote is not None:
            if character == quote:
                break
        elif character.isspace() or character in _UNQUOTED_PATH_END:
            break
        cursor += 1
    if cursor == start:
        return None
    if kind == "unc" and not _unc_path_has_share(text[start:cursor]):
        return None
    return cursor


def _is_absolute_path(text: str) -> bool:
    """Report whether a complete string has an aggressive absolute-path shape."""
    kind = _absolute_path_kind(text, 0)
    if kind is None:
        return False
    if kind == "unc":
        return _unc_path_has_share(text)
    return True


def _replace_aggressive_paths(text: str) -> str:
    """Replace absolute paths while retaining surrounding quotes and punctuation."""
    pieces: list[str] = []
    cursor = 0
    search_from = 0
    while search_from < len(text):
        kind = _absolute_path_kind(text, search_from)
        if kind is None:
            search_from += 1
            continue
        end = _absolute_path_end(text, search_from, kind)
        if end is None:
            search_from += 1
            continue
        pieces.extend((text[cursor:search_from], "<path>"))
        cursor = end
        search_from = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _aggressive_text_signature(text: str) -> str:
    """Replace the broader values enabled by the aggressive CLI option."""
    signature = _replace_aggressive_paths(text)
    signature = _AGGRESSIVE_PREFIXED_HEX.sub("<hex>", signature)
    return _AGGRESSIVE_INTEGER.sub("<integer>", signature)


def _fast_text_signature(text: str) -> tuple[str, str, str] | None:
    """Build a compact signature for the common leading-ISO plus one-ID shape."""
    leading_iso = _LEADING_ISO_TIMESTAMP.match(text)
    if leading_iso is None:
        return None
    stable_start = leading_iso.end()
    if text.count("-", stable_start) >= 4:
        return None

    for marker in _SIMPLE_NAMED_ID_MARKERS:
        marker_start = text.find(marker, stable_start)
        if marker_start < 0:
            continue
        if marker_start and (
            text[marker_start - 1].isalnum() or text[marker_start - 1] == "_"
        ):
            return None
        if marker == "traceparent=":
            if "id" in text or text.rfind(marker) != marker_start:
                return None
        else:
            id_start = marker_start + marker.rfind("id")
            if text.find("id") != id_start or text.rfind("id") != id_start:
                return None
        value_start = marker_start + len(marker)
        quoted = value_start < len(text) and text[value_start] in "\"'"
        if quoted:
            value_start += 1
        value = _ID_VALUE.match(text, value_start)
        if value is None:
            return None
        value_end = value.end()
        if quoted and value_end < len(text) and text[value_end] in "\"'":
            value_end += 1
        prefix_end = marker_start + len(marker)
        if len(text) - (value_end - prefix_end) > MAX_SIGNATURE_CHARACTERS:
            return None
        return (
            "text-leading-iso-id",
            text[stable_start:prefix_end],
            text[value_end:],
        )
    return None


def _compile_fast_text_matcher(
    signature: object | None,
) -> tuple[re.Pattern[str], str] | None:
    """Compile one bounded matcher for the active common text signature."""
    if (
        not isinstance(signature, tuple)
        or len(signature) != 3
        or signature[0] != "text-leading-iso-id"
    ):
        return None
    prefix = signature[1]
    suffix = signature[2]
    return (
        re.compile(
            _LEADING_ISO_TIMESTAMP.pattern
            + re.escape(prefix)
            + _ID_VALUE.pattern
        ),
        suffix,
    )


def _text_signature(text: str, *, aggressive: bool = False) -> str:
    """Replace conservative values and any explicitly enabled broad values."""
    leading_iso = _LEADING_ISO_TIMESTAMP.match(text)
    if leading_iso is not None:
        signature = "<timestamp>" + text[leading_iso.end():]
        had_iso_timestamp = True
    else:
        signature, had_iso_timestamp = _replace_iso_timestamps(text)
    if not had_iso_timestamp:
        if _MONTH_NAME.search(signature):
            if "/" in signature:
                signature = _ACCESS_TIMESTAMP.sub("<timestamp>", signature)
            signature = _SYSLOG_TIMESTAMP.sub("<timestamp>", signature)
        leading_clock = _LEADING_LEVEL_CLOCK_TIMESTAMP.match(signature)
        if leading_clock is not None:
            signature = "<timestamp>" + signature[leading_clock.end() :]
    if signature.count("-") >= 4:
        signature = _UUID.sub("<uuid>", signature)

    if not (
        "id" in signature
        or "ID" in signature
        or "Id" in signature
        or "iD" in signature
        or "traceparent" in signature.casefold()
    ):
        return _aggressive_text_signature(signature) if aggressive else signature

    signature, replaced_single_id = _replace_single_named_id(signature)
    if replaced_single_id:
        return _aggressive_text_signature(signature) if aggressive else signature

    def replace_id(match: re.Match[str]) -> str:
        return f"{match.group('id_key')}{match.group('id_separator')}<id>"

    signature = _NAMED_ID.sub(replace_id, signature)
    return _aggressive_text_signature(signature) if aggressive else signature


def _stable_json(
    value: object,
    *,
    top_level: bool = False,
    aggressive: bool = False,
) -> object:
    """Build a JSON value with standard volatile metadata replaced."""
    if isinstance(value, dict):
        stable: dict[str, object] = {}
        for key, item in value.items():
            normalized = _field_name(key)
            if normalized in _JSON_ID_FIELDS:
                stable[str(key)] = "<id>"
            elif top_level and normalized in _JSON_TOP_LEVEL_METADATA:
                stable[str(key)] = "<metadata>"
            else:
                stable[str(key)] = _stable_json(item, aggressive=aggressive)
        return stable
    if isinstance(value, list):
        return [_stable_json(item, aggressive=aggressive) for item in value]
    if isinstance(value, str):
        if aggressive and _is_absolute_path(value):
            return "<path>"
        return _text_signature(value, aggressive=aggressive)
    if aggressive and isinstance(value, int) and not isinstance(value, bool):
        if len(str(abs(value))) >= 4:
            return "<integer>"
    return value


def _signature(line: str, *, aggressive: bool = False) -> object | None:
    """Return an indexed signature, or ``None`` when the record should stay visible."""
    if len(line) > MAX_SIGNATURE_CHARACTERS:
        return None
    first = line[:1]
    stripped = line.strip() if first.isspace() else line
    if stripped[:1] and stripped[0] in "{[":
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(value, dict):
            return None
        try:
            stable = _stable_json(
                value,
                top_level=True,
                aggressive=aggressive,
            )
            signature = "json:" + json.dumps(
                stable,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return (
                signature
                if len(signature) <= MAX_SIGNATURE_CHARACTERS
                else None
            )
        except RecursionError:
            return None
    if not aggressive:
        fast_signature = _fast_text_signature(line)
        if fast_signature is not None:
            return fast_signature
    signature = "text:" + _text_signature(line, aggressive=aggressive)
    return signature if len(signature) <= MAX_SIGNATURE_CHARACTERS else None


class _StreamReducer:
    """Reduce a stream with bounded pattern state and exact signature matching."""

    def __init__(
        self,
        output: TextIO,
        *,
        dot_every: int = DEFAULT_DOT_EVERY,
        summary_interval: float = DEFAULT_SUMMARY_INTERVAL,
        pattern_limit: int = DEFAULT_PATTERN_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        live_timer: bool = False,
        aggressive: bool = False,
    ) -> None:
        if dot_every < 1:
            raise ValueError("dot_every must be at least 1")
        if summary_interval <= 0:
            raise ValueError("summary_interval must be greater than 0")
        if pattern_limit < 1:
            raise ValueError("pattern_limit must be at least 1")

        self._output = output
        self._dot_every = dot_every
        self._summary_interval = summary_interval
        self._pattern_limit = pattern_limit
        self._clock = clock
        self._live_timer = live_timer
        self._aggressive = aggressive
        self._patterns: OrderedDict[object, None] = OrderedDict()
        self._diagnostic_open = False
        self._first_record = True
        self._recent_signature: object | None = None
        self._fast_matcher_signature: object | None = None
        self._fast_matcher: tuple[re.Pattern[str], str] | None = None

        self._run_signature: object | None = None
        self._run_count = 0
        self._run_started = 0.0
        self._last_dot_flush = 0.0
        self._dots_written = 0

        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._finished = False

    def _schedule_timer(self) -> None:
        """Schedule live dot and summary progress while a run is open."""
        if not self._live_timer or self._run_signature is None or self._finished:
            return
        if self._timer is not None:
            return
        timer = threading.Timer(
            min(DOT_FLUSH_INTERVAL, self._summary_interval),
            self._on_timer,
        )
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        """Cancel the current live timer when a run closes."""
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _on_timer(self) -> None:
        """Advance a live run without requiring another input record."""
        with self._lock:
            self._timer = None
            if self._finished:
                return
            self._tick_locked(self._clock())
            self._schedule_timer()

    def _remember(self, signature: object) -> None:
        """Index one visible pattern and evict the oldest pattern when bounded."""
        self._patterns[signature] = None
        if len(self._patterns) > self._pattern_limit:
            self._patterns.popitem(last=False)

    def _write_visible(self, printable: str) -> None:
        """Emit one complete record immediately."""
        self._output.write(printable + "\n")
        self._output.flush()

    def _keep_diagnostic(self, printable: str) -> bool:
        """Keep recognized and uncertain multiline diagnostic content intact."""
        if self._diagnostic_open:
            diagnostic = bool(
                _DIAGNOSTIC_START.search(printable)
                or _DIAGNOSTIC_LINE.search(printable)
            )
            if _RECORD_BOUNDARY.search(printable) and not diagnostic:
                self._diagnostic_open = False
                return False
            return True

        if printable[:1].isspace():
            if _DIAGNOSTIC_LINE.search(printable):
                self._diagnostic_open = True
            return True

        has_traceback = "Traceback (most recent call last):" in printable
        if not has_traceback and printable[:1] in "0123456789{[<":
            return False
        starts_diagnostic = printable.startswith(
            ("Exception in thread", "panic:", "Caused by:", "Suppressed:")
        )
        separator = printable.find(":")
        named_diagnostic = separator >= 0 and printable[:separator].endswith(
            ("Error", "Exception", "Failure")
        )
        if starts_diagnostic or has_traceback or named_diagnostic:
            self._diagnostic_open = True
            return True
        return False

    def _write_pending_dots(self, now: float, *, force: bool = False) -> None:
        """Write buffered dot groups when size or latency makes them due."""
        due = self._run_count // self._dot_every - self._dots_written
        if due <= 0:
            return
        if (
            not force
            and due < DOT_BATCH_SIZE
            and now - self._last_dot_flush < DOT_FLUSH_INTERVAL
        ):
            return
        self._output.write("." * due)
        self._output.flush()
        self._dots_written += due
        self._last_dot_flush = now

    def _close_run(self, now: float, *, reason: str) -> None:
        """Close one repeated-pattern run with its exact suppressed count."""
        if self._run_signature is None:
            return
        self._write_pending_dots(now, force=True)
        prefix = " " if self._dots_written else ""
        if reason == "stop":
            summary = f"[{self._run_count} similar before stop]"
        else:
            elapsed = int(max(0.0, now - self._run_started))
            summary = f"[{self._run_count} similar in {elapsed}s]"
        self._output.write(prefix + summary + "\n")
        self._output.flush()
        self._cancel_timer()
        self._run_signature = None
        self._run_count = 0
        self._run_started = 0.0
        self._last_dot_flush = 0.0
        self._dots_written = 0

    def _suppress(self, signature: object, now: float) -> None:
        """Add one record to the active repeated-pattern run."""
        if self._run_signature is not None and signature != self._run_signature:
            self._close_run(now, reason="change")
        if self._run_signature is None:
            self._run_signature = signature
            self._run_started = now
            self._last_dot_flush = now
            self._schedule_timer()
        self._run_count += 1
        due = self._run_count // self._dot_every - self._dots_written
        if (
            due >= DOT_BATCH_SIZE
            or now - self._last_dot_flush >= DOT_FLUSH_INTERVAL
        ):
            self._write_pending_dots(now)
        if now - self._run_started >= self._summary_interval:
            self._close_run(now, reason="interval")

    def _process_unlocked(self, line: str) -> bool:
        """Process one record and report whether its full text was suppressed."""
        printable = line.rstrip("\r\n")
        if self._finished:
            raise RuntimeError("cannot process records after finish")
        if self._first_record:
            printable = printable.removeprefix("\ufeff")
            self._first_record = False
        common_record = (
            not self._diagnostic_open
            and printable[:1] in "0123456789{[<"
            and "Traceback (most recent call last):" not in printable
        )
        if not common_record and self._keep_diagnostic(printable):
            if self._run_signature is not None:
                self._close_run(self._clock(), reason="change")
            self._recent_signature = None
            self._write_visible(printable)
            return False

        candidate = self._run_signature or self._recent_signature
        if candidate is not self._fast_matcher_signature:
            self._fast_matcher_signature = candidate
            self._fast_matcher = _compile_fast_text_matcher(candidate)
        fast_match = None
        if self._fast_matcher is not None:
            matcher, suffix = self._fast_matcher
            fast_match = matcher.match(printable)
        if (
            fast_match is not None
            and fast_match.end() == len(printable) - len(suffix)
            and printable.endswith(suffix)
        ):
            self._suppress(candidate, self._clock())
            return True

        signature = _signature(printable, aggressive=self._aggressive)
        if signature is not None and signature in self._patterns:
            if signature != self._recent_signature:
                if self._run_signature is not None:
                    self._close_run(self._clock(), reason="change")
                self._recent_signature = signature
                self._write_visible(printable)
                return False
            self._suppress(signature, self._clock())
            return True

        if self._run_signature is not None:
            self._close_run(self._clock(), reason="change")
        if signature is not None:
            self._remember(signature)
        self._recent_signature = signature
        self._write_visible(printable)
        return False

    def process(self, line: str) -> bool:
        """Process one record and report whether its full text was suppressed."""
        if not self._live_timer:
            return self._process_unlocked(line)
        with self._lock:
            return self._process_unlocked(line)

    def _tick_locked(self, now: float) -> None:
        """Advance buffered output using a supplied current time."""
        if self._run_signature is None:
            return
        self._write_pending_dots(now)
        if now - self._run_started >= self._summary_interval:
            self._close_run(now, reason="interval")

    def tick(self, now: float | None = None) -> None:
        """Advance periodic output for deterministic replay or an event loop."""
        if not self._live_timer:
            if not self._finished:
                self._tick_locked(self._clock() if now is None else now)
            return
        with self._lock:
            if not self._finished:
                self._tick_locked(self._clock() if now is None else now)

    def finish(self) -> None:
        """Flush the final repeat run and stop live timers."""
        if not self._live_timer:
            if self._finished:
                return
            self._finished = True
            if self._run_signature is not None:
                self._close_run(self._clock(), reason="stop")
            self._output.flush()
            return
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._close_run(self._clock(), reason="stop")
            self._cancel_timer()
            self._output.flush()


def _tail_existing(
    source: TextIO, line_count: int, reducer: _StreamReducer
) -> list[tuple[int, str, bool]]:
    """Read existing context and retain bounded rewrite checkpoints."""
    if line_count == 0:
        checkpoints: list[tuple[int, str, bool]] = []
        source.seek(0)
        for _ in range(FILE_CHECKPOINT_LIMIT // 2):
            start = source.tell()
            line = source.readline()
            if not line:
                break
            _remember_file_checkpoint(checkpoints, start, line)
        end_position = source.seek(0, os.SEEK_END)
        source.seek(max(0, end_position - FILE_TAIL_CHECKPOINT_BYTES))
        while True:
            start = source.tell()
            line = source.readline()
            if not line:
                break
            _remember_file_checkpoint(checkpoints, start, line)
        source.seek(0, os.SEEK_END)
        return checkpoints

    records: deque[tuple[int, str]] = deque(maxlen=line_count)
    checkpoints: list[tuple[int, str, bool]] = []
    while True:
        start = source.tell()
        line = source.readline()
        if not line:
            break
        records.append((start, line))
        _remember_file_checkpoint(checkpoints, start, line)
    for _, line in records:
        reducer.process(line)
    return checkpoints


def _remember_file_checkpoint(
    checkpoints: list[tuple[int, str, bool]],
    start: int,
    line: str,
) -> None:
    """Retain the first and most recent bounded samples of consumed content."""
    checkpoint = (start, line, not line.endswith(("\n", "\r")))
    if checkpoints and checkpoints[-1][0] == start:
        checkpoints[-1] = checkpoint
        return
    if len(checkpoints) >= FILE_CHECKPOINT_LIMIT:
        checkpoints.pop(FILE_CHECKPOINT_LIMIT // 2)
    checkpoints.append(checkpoint)


def _checkpoint_matches(
    source: TextIO,
    checkpoints: list[tuple[int, str, bool]],
    current_position: int,
) -> bool:
    """Check that consumed content still matches after an in-place rewrite."""
    if not checkpoints:
        return True
    try:
        for start, expected, allow_extension in checkpoints:
            source.seek(start)
            observed = source.readline()
            if allow_extension:
                if not observed.startswith(expected):
                    return False
            elif observed != expected:
                return False
        return True
    finally:
        source.seek(current_position)


def _open_source(path: Path) -> TextIO:
    """Open a log for reading while allowing Windows writers to rotate it."""
    if os.name != "nt":
        return path.open("r", encoding="utf-8", errors="replace")

    import ctypes
    from ctypes import wintypes
    import msvcrt

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # read, write, and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    handle_value = handle if isinstance(handle, int) else handle.value
    invalid_handle = wintypes.HANDLE(-1).value
    if handle_value == invalid_handle:
        error_code = ctypes.get_last_error()
        message = ctypes.FormatError(error_code)
        if error_code in {2, 3}:
            raise FileNotFoundError(error_code, message, str(path))
        raise OSError(error_code, message, str(path))

    try:
        descriptor = msvcrt.open_osfhandle(handle_value, os.O_RDONLY)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    return os.fdopen(
        descriptor,
        "r",
        encoding="utf-8",
        errors="replace",
    )


def _read_file_once(path: Path, line_count: int, reducer: _StreamReducer) -> None:
    """Read a file once using tail-style context selection."""
    with _open_source(path) as source:
        _tail_existing(source, line_count, reducer)


def _follow_file(path: Path, line_count: int, reducer: _StreamReducer) -> None:
    """Follow a file across appends, truncation, replacement, and rotation."""
    source: TextIO | None = None
    first_open = True
    checkpoints: list[tuple[int, str, bool]] = []
    at_eof = False
    try:
        while True:
            if source is None:
                try:
                    source = _open_source(path)
                except FileNotFoundError:
                    time.sleep(FOLLOW_POLL_INTERVAL)
                    continue
                if first_open:
                    checkpoints = _tail_existing(source, line_count, reducer)
                    first_open = False
                else:
                    while True:
                        line_start = source.tell()
                        line = source.readline()
                        if not line:
                            break
                        reducer.process(line)
                        _remember_file_checkpoint(checkpoints, line_start, line)
                at_eof = True

            if at_eof:
                try:
                    path_stat = path.stat()
                    source_stat = os.fstat(source.fileno())
                except FileNotFoundError:
                    path_stat = None
                    source_stat = None
                if path_stat is not None and source_stat is not None:
                    if (path_stat.st_dev, path_stat.st_ino) != (
                        source_stat.st_dev,
                        source_stat.st_ino,
                    ):
                        source.close()
                        source = None
                        checkpoints = []
                        continue
                    current_position = source.tell()
                    changed_in_place = (
                        bool(checkpoints)
                        and not _checkpoint_matches(
                            source,
                            checkpoints,
                            current_position,
                        )
                    )
                    if path_stat.st_size < current_position or changed_in_place:
                        source.seek(0)
                        checkpoints = []
                at_eof = False

            line_start = source.tell()
            line = source.readline()
            if line:
                reducer.process(line)
                _remember_file_checkpoint(checkpoints, line_start, line)
                continue

            at_eof = True
            reducer.tick()
            try:
                path_stat = path.stat()
                source_stat = os.fstat(source.fileno())
            except FileNotFoundError:
                time.sleep(FOLLOW_POLL_INTERVAL)
                continue

            if (path_stat.st_dev, path_stat.st_ino) != (
                source_stat.st_dev,
                source_stat.st_ino,
            ):
                source.close()
                source = None
                checkpoints = []
                at_eof = False
                continue
            current_position = source.tell()
            changed_in_place = (
                bool(checkpoints)
                and not _checkpoint_matches(source, checkpoints, current_position)
            )
            if path_stat.st_size < current_position or changed_in_place:
                source.seek(0)
                checkpoints = []
                continue
            time.sleep(FOLLOW_POLL_INTERVAL)
    finally:
        if source is not None:
            source.close()


def _child_exit_status(
    return_code: int, *, requested_signal: int | None = None
) -> int:
    """Convert a child return code to the status qurtail should return."""
    if return_code < 0:
        return 128 - return_code
    if requested_signal is not None and return_code == 0:
        return 128 + requested_signal
    return return_code


def _process_group_exists(process_group: int) -> bool:
    """Report whether a POSIX process group still has live members."""
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group(
    process: subprocess.Popen[bytes], timeout: float
) -> bool:
    """Wait up to ``timeout`` for a POSIX process group to disappear."""
    deadline = time.monotonic() + timeout
    while _process_group_exists(process.pid):
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_INTERVAL, remaining))
    return True


def _signal_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
    """Signal the isolated child process group on the active platform."""
    if os.name == "nt":
        if signum == signal.SIGINT and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
        return
    os.killpg(process.pid, signum)


def _interrupt_child(
    process: subprocess.Popen[bytes],
    *,
    requested_signal: int = signal.SIGINT,
) -> int:
    """Stop and reap an isolated child process group with bounded escalation."""
    if process.poll() is not None and (
        os.name == "nt" or not _process_group_exists(process.pid)
    ):
        return process.returncode

    try:
        _signal_process_group(process, requested_signal)
    except (OSError, ValueError):
        try:
            process.terminate()
        except OSError:
            pass

    if os.name != "nt":
        if not _wait_for_process_group(process, INTERRUPT_GRACE_SECONDS):
            try:
                _signal_process_group(process, signal.SIGTERM)
            except (OSError, ValueError):
                pass
        if not _wait_for_process_group(process, INTERRUPT_GRACE_SECONDS):
            try:
                _signal_process_group(process, signal.SIGKILL)
            except (OSError, ValueError):
                pass
            _wait_for_process_group(process, INTERRUPT_GRACE_SECONDS)
        return process.wait()

    try:
        return process.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        return process.wait(timeout=INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        return process.wait()


def _interrupt_and_drain(
    process: subprocess.Popen[bytes],
    reducer: _StreamReducer,
    raw_output: BinaryIO | None,
    requested_signal: int,
) -> int:
    """Drain output concurrently while the child group handles a signal."""
    outcome: dict[str, object] = {}

    def stop_child() -> None:
        try:
            outcome["return_code"] = _interrupt_child(
                process,
                requested_signal=requested_signal,
            )
        except BaseException as error:
            outcome["error"] = error

    interrupter = threading.Thread(target=stop_child, daemon=True)
    interrupter.start()
    try:
        assert process.stdout is not None
        _stream_child_output(process.stdout, reducer, raw_output)
    except (KeyboardInterrupt, _TerminationRequested):
        pass
    interrupter.join()
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return int(outcome["return_code"])


def _stream_child_output(
    source: BinaryIO,
    reducer: _StreamReducer,
    raw_output: BinaryIO | None,
) -> None:
    """Copy child records to the optional transcript and compact view."""
    for raw_line in source:
        if raw_output is not None:
            raw_output.write(raw_line)
            raw_output.flush()
        reducer.process(raw_line.decode("utf-8", errors="replace"))


def _run_child_command(
    command: list[str],
    reducer: _StreamReducer,
    *,
    raw_log: Path | None = None,
    overwrite_raw_log: bool = False,
) -> int:
    """Stream one child command through the reducer and return its exit status."""
    popen_options: dict[str, object]
    if os.name == "nt":
        popen_options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        popen_options = {"start_new_session": True}

    raw_mode = "wb" if overwrite_raw_log else "xb"
    raw_output = raw_log.open(raw_mode) if raw_log is not None else None
    process: subprocess.Popen[bytes] | None = None
    previous_sigterm: object | None = None

    def request_termination(signum: int, _frame: object) -> None:
        raise _TerminationRequested(signum)

    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, request_termination)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            shell=False,
            **popen_options,
        )
        assert process.stdout is not None
        try:
            _stream_child_output(process.stdout, reducer, raw_output)
            return _child_exit_status(process.wait())
        except (KeyboardInterrupt, _TerminationRequested) as interruption:
            requested_signal = (
                interruption.signum
                if isinstance(interruption, _TerminationRequested)
                else signal.SIGINT
            )
            return_code = _interrupt_and_drain(
                process,
                reducer,
                raw_output,
                requested_signal,
            )
            return _child_exit_status(
                return_code,
                requested_signal=requested_signal,
            )
        except BaseException:
            if process.poll() is None:
                try:
                    _signal_process_group(process, signal.SIGKILL)
                except (OSError, ValueError):
                    process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if raw_output is not None:
            raw_output.close()


def _build_parser() -> argparse.ArgumentParser:
    """Build the complete qurtail command-line reference."""
    parser = argparse.ArgumentParser(
        prog="qurtail",
        description="Follow logs while compacting conservative repeated patterns.",
        epilog=(
            "Run a child command: qurtail run [--aggressive] [--dot-every N] "
            "[--raw-log PATH] [--overwrite] -- COMMAND..."
        ),
    )
    parser.add_argument("file", nargs="?", help="file to read; stdin when omitted")
    parser.add_argument(
        "-f",
        "-F",
        "--follow",
        action="store_true",
        help="keep following a file through truncation, replacement, and rotation",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=DEFAULT_TAIL_LINES,
        metavar="N",
        help="existing file lines to read (default: %(default)s)",
    )
    parser.add_argument(
        "--dot-every",
        type=int,
        default=DEFAULT_DOT_EVERY,
        metavar="N",
        help="write one dot for every N suppressed records (default: %(default)s)",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="also ignore prefixed hex, long integers, and absolute paths",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _build_run_parser() -> argparse.ArgumentParser:
    """Build the child-command runner reference."""
    parser = argparse.ArgumentParser(
        prog="qurtail run",
        description=(
            "Run a child command and compact its combined standard output and error."
        ),
    )
    parser.add_argument(
        "--dot-every",
        type=int,
        default=DEFAULT_DOT_EVERY,
        metavar="N",
        help="write one dot for every N suppressed records (default: %(default)s)",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="also ignore prefixed hex, long integers, and absolute paths",
    )
    parser.add_argument(
        "--raw-log",
        type=Path,
        metavar="PATH",
        help="write exact combined output bytes to a new PATH",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow --raw-log to replace an existing file",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command and arguments to run after --",
    )
    return parser


def _run_mode(argv: list[str]) -> int:
    """Parse and execute the child-command runner."""
    parser = _build_run_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    if args.dot_every < 1:
        parser.error("--dot-every must be at least 1")
    if args.overwrite and args.raw_log is None:
        parser.error("--overwrite requires --raw-log")

    reducer = _StreamReducer(
        sys.stdout,
        dot_every=args.dot_every,
        live_timer=True,
        aggressive=args.aggressive,
    )
    try:
        return _run_child_command(
            command,
            reducer,
            raw_log=args.raw_log,
            overwrite_raw_log=args.overwrite,
        )
    except OSError as error:
        parser.exit(1, f"qurtail: {error}\n")
    finally:
        reducer.finish()


def main(argv: list[str] | None = None) -> int:
    """Run the qurtail command and return its process exit status."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["run"]:
        return _run_mode(arguments[1:])

    parser = _build_parser()
    args = parser.parse_args(arguments)
    if args.lines < 0:
        parser.error("--lines must be zero or greater")
    if args.dot_every < 1:
        parser.error("--dot-every must be at least 1")
    if args.follow and not args.file:
        parser.error("--follow requires a file")

    reducer = _StreamReducer(
        sys.stdout,
        dot_every=args.dot_every,
        live_timer=True,
        aggressive=args.aggressive,
    )
    try:
        if args.file:
            path = Path(args.file)
            if args.follow:
                _follow_file(path, args.lines, reducer)
            else:
                _read_file_once(path, args.lines, reducer)
        else:
            for line in sys.stdin:
                reducer.process(line)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"qurtail: {error}\n")
    finally:
        reducer.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
