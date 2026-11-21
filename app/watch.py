"""Render-log parsers and sandbox sources, vendored from design-agent `harness/watch.py` (Apache-2.0).

The parsers (HyperFrames, ffmpeg, JSON status, regex, heartbeat) and the sources that read a growing
log or status file over a sandbox exec are unchanged. DA's in-process `WatchRunner`, ledger and
event bus are not used: Studio's watcher runs as a durable background task (`app.renders`).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

from app.looks import SandboxExec

_LOGGER = logging.getLogger(__name__)

#: Progress events: when the fraction moves this much, or at least this often while output flows.
PROGRESS_STEP = 0.01
PROGRESS_EVERY_SECONDS = 60.0
#: The ETA is a straight line through the progress of this recent window.
ETA_WINDOW_SECONDS = 300.0
#: Lines kept for context (the error text, a stall, a failed job's report).
TAIL_LINES = 20
#: A read that takes longer is abandoned and counted as failed, so a hung call can't hide a stall.
READ_TIMEOUT_SECONDS = 120.0
#: Ended watches stay listed this long.
KEEP_ENDED_SECONDS = 24 * 3600.0

#: Lines that mean a job failed, for any log: every terminal state, not only success. `Error\b`
#: catches `TypeError:` but not `ErrorBoundary`.
FAILURE = (
    r"Error\b|error:|✖|Traceback|ENOENT|\bKilled\b|\bOOM\b|out of memory|Segmentation fault"
    r"|exited with code [1-9]"
)
#: The ffmpeg messages that end it. "Error while decoding" is routine and ffmpeg carries on.
FFMPEG_FAILURE = (
    r"(?i)conversion failed|error opening (?:input|output)|error initializing"
    r"|no such file or directory|unrecognized option|unknown encoder|invalid argument"
    r"|\bkilled\b|out of memory|exited with code [1-9]"
)

Pattern = str | re.Pattern[str]


# -- what a parser reads ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Update:
    """What a parser found in new output.

    `kind` is `progress` (a counter or status moved), `error` (a line that means it failed), `done`
    (a line that means it finished) or `line` (anything else, kept for context). `fraction` is 0-1
    when known; `eta_seconds` when the source states one; `phase` names the stage ("rendering",
    "encoding", a status file's `step`); `data` carries extra facts (`cost_usd`, `output`) into
    the event.
    """

    kind: Literal["progress", "error", "done", "line"]
    fraction: float | None = None
    eta_seconds: float | None = None
    text: str = ""
    phase: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class WatchSource(Protocol):
    """Where a watch reads from.

    `read` returns the text that is new since the last read, or "" when nothing is. `closed` turns
    true once nothing more can come. `note` says why the last read had nothing (the file does not
    exist yet, the JSON is torn), for the stall message.
    """

    closed: bool
    note: str

    async def read(self) -> str: ...


@runtime_checkable
class WatchParser(Protocol):
    def parse(self, text: str) -> list[Update]:
        """Updates for a source's new text. "" means the source was quiet this poll; a line parser
        then finishes a partial line it was holding."""
        ...


# -- parsers ----------------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_BREAK = re.compile(r"\r\n|\r|\n")


def _patterns(value: Pattern | Sequence[Pattern] | None) -> list[re.Pattern[str]]:
    if value is None or isinstance(value, str | re.Pattern):
        value = [value] if value else []
    return [p if isinstance(p, re.Pattern) else re.compile(p) for p in value if p]


class LineParser:
    """Line-oriented output: split on newlines and carriage returns (a TTY progress bar redraws with
    `\\r`), colors stripped, a partial last line held until it completes or the source goes quiet.
    Each line is checked for done, then error, then progress (`progress`, which subclasses define).
    """

    def __init__(
        self,
        *,
        error: Pattern | Sequence[Pattern] | None = FAILURE,
        done: Pattern | Sequence[Pattern] | None = None,
    ) -> None:
        self._error = _patterns(error)
        self._done = _patterns(done)
        self._held = ""

    def parse(self, text: str) -> list[Update]:
        if text:
            pieces = _BREAK.split(self._held + text)
            self._held = pieces.pop()
        else:
            pieces, self._held = [self._held], ""
        updates: list[Update] = []
        for piece in pieces:
            line = _ANSI.sub("", piece).strip()
            if line:
                updates.extend(self.parse_line(line))
        return updates

    def parse_line(self, line: str) -> list[Update]:
        if any(p.search(line) for p in self._done):
            return [Update("done", fraction=1.0, text=line)]
        if any(p.search(line) for p in self._error):
            return [Update("error", text=line)]
        return self.progress(line)

    def progress(self, line: str) -> list[Update]:
        return [Update("line", text=line)]


def _counter(match: re.Match[str] | None) -> tuple[int, int] | None:
    """A done/total pair from a match, or None when it can't be a counter (0 total, done > total:
    a partial line)."""
    if match is None:
        return None
    done, total = int(match[1]), int(match[2])
    return (done, total) if 0 < total and 0 <= done <= total else None


class HeartbeatParser(LineParser):
    """Any log, read for signs of life only: each new line resets the stall timer and is kept for
    context, and a line that means failure (`FAILURE`) ends the watch. It reports no percentage.
    The default format: a detached job's exit code still says how it ended."""


#: What ends a HyperFrames render as failed: its error box (`✗  Render failed`, `✗  Not a
#: directory: …`), a `Failed:` or `Render cancelled` progress line, or the process being killed.
#: Generic `Error` lines are left out on purpose: a best-effort render logs the page's own script
#: errors as warnings and still finishes.
HYPERFRAMES_FAILURE = (
    r"^✗\s+\S",
    r"^[█░]*\s*\d{1,3}%\s+(?:Failed:|Render cancelled)",
    r"\bKilled\b|\bOOM\b|out of memory|Segmentation fault|exited with code [1-9]",
)

#: Phase names for HyperFrames' stage codes and progress-line labels, matched by keyword in order.
_HF_PHASES = (
    ("compil", "compiling"),
    ("extract", "extracting video"),
    ("audio", "processing audio"),
    ("start", "starting capture"),
    ("browser", "starting capture"),
    ("captur", "capturing frames"),
    ("stream", "capturing frames"),
    ("encod", "encoding"),
    ("assembl", "assembling"),
)
_COUNTS = re.compile(r"\([^)]*\)|\d+\s*/\s*\d+")
_FRAME_PHASES = frozenset({"capturing frames", "encoding"})


def _percent(value: object) -> float | None:
    """A percent (0-100) as a fraction (0-1); None when it is not a number."""
    number = _number(value)
    return None if number is None or number < 0 else min(1.0, number / 100)


def _hf_phase(stage: str) -> str:
    """One phase name for a stage, whether it came as a code (`capture`) or a label
    (`Streaming frame 12/60`), so the two forms of one step never read as a phase change."""
    lowered = stage.lower()
    for key, phase in _HF_PHASES:
        if key in lowered:
            return phase
    return " ".join(_COUNTS.sub("", lowered).replace("_", " ").split()) or "rendering"


class HyperFramesParser(LineParser):
    """`hyperframes render` output, as a detached job's log (no TTY) or a terminal's.

    Without a TTY the CLI writes `@hf-progress {"code": …, "pct": …, "done": …, "total": …}` for
    every step, beside a plain progress line (`████░░  42%  Streaming frame 25/60`); under a TTY
    only the progress line is drawn, redrawn with `\\r`. Either gives the fraction (`pct` / 100), a
    phase (compiling, capturing frames, encoding, assembling) and frame counts. Done at
    `100%  Render complete`, with the output path from the `◇  <path>` line after it (the done line
    waits one read for it, and a quiet poll gives up). Failed on `HYPERFRAMES_FAILURE`.
    """

    MACHINE = re.compile(r"^@hf-progress\s+(\{.*\})\s*$")
    BAR = re.compile(r"^[█░]+\s+(\d{1,3})%\s+(\S.*)$")
    COMPLETE = r"^[█░]*\s*100%\s+Render complete\b"
    OUTPUT = re.compile(r"^◇\s+(/\S.*)$")
    LOG_TAG = re.compile(r"\[[\w:-]+\]")
    GLYPHS = re.compile(r"^[█░]+\s+")

    def __init__(
        self,
        *,
        error: Pattern | Sequence[Pattern] | None = HYPERFRAMES_FAILURE,
        done: Pattern | Sequence[Pattern] | None = None,
    ) -> None:
        super().__init__(error=error, done=[self.COMPLETE, *_patterns(done)])
        #: A done line waiting one read for the output path that follows it.
        self._held_done: Update | None = None

    def parse(self, text: str) -> list[Update]:
        updates = [replace(u, text=self.GLYPHS.sub("", u.text)) for u in super().parse(text)]
        if self._held_done is not None:
            updates.insert(0, self._held_done)
            self._held_done = None
        for index, update in enumerate(updates):
            if update.kind != "done":
                continue
            for later in updates[index + 1 :]:
                found = self.OUTPUT.match(later.text)
                if found is not None:
                    data = {**update.data, "output": found[1]}
                    shown = f"Render complete: {found[1]}"
                    updates[index] = replace(update, text=shown, data=data)
                    break
            else:
                if text:  # the path may be in the next read; a quiet poll gives up on it
                    self._held_done = updates.pop(index)
            break
        return updates

    def progress(self, line: str) -> list[Update]:
        machine = self.MACHINE.match(line)
        if machine is not None:
            return self._machine(line, machine[1])
        bar = self.BAR.match(line)
        if bar is None:
            return [Update("line", text=line)]
        # Under a TTY the bar has no newline, so the next log line can land on the same line.
        stage = self.LOG_TAG.split(bar[2], maxsplit=1)[0].strip() or bar[2]
        phase = _hf_phase(stage)
        counts = _counter(re.search(r"(\d+)\s*/\s*(\d+)", stage))
        data = (
            {"frames": counts[0], "total_frames": counts[1]}
            if counts is not None and phase in _FRAME_PHASES
            else {}
        )
        return [Update("progress", _percent(bar[1]), text=stage, phase=phase, data=data)]

    def _machine(self, line: str, payload: str) -> list[Update]:
        try:
            value = json.loads(payload)
        except ValueError:
            return [Update("line", text=line)]
        if not isinstance(value, Mapping):
            return [Update("line", text=line)]
        phase = _hf_phase(str(value.get("code") or ""))
        done, total = value.get("done"), value.get("total")
        counted = isinstance(done, int) and isinstance(total, int) and 0 < total
        data = {"frames": done, "total_frames": total} if counted and phase in _FRAME_PHASES else {}
        text = f"{phase} {done}/{total}" if counted else phase
        return [Update("progress", _percent(value.get("pct")), text=text, phase=phase, data=data)]


def _number(value: object) -> float | None:
    try:
        number = float(str(value).strip().rstrip("x"))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _clock_seconds(value: str) -> float | None:
    parts = value.strip().split(":")
    if len(parts) != 3 or value.strip().startswith("-"):
        return None
    numbers = [_number(p) for p in parts]
    if any(n is None for n in numbers):
        return None
    hours, mins, secs = (n or 0.0 for n in numbers)
    return hours * 3600 + mins * 60 + secs


class FfmpegProgressParser(LineParser):
    """ffmpeg's `-progress` blocks (`frame=`, `out_time_us=`, `speed=`, ending `progress=continue`
    or `progress=end`, every half second), and its ordinary stats line as a fallback.

    The fraction needs `total_frames` or `duration_seconds`; without them progress carries the frame
    and time only. `progress=end` is reported done once a quiet poll confirms nothing follows it,
    because on a failure ffmpeg prints its last block before "Conversion failed!". Only messages
    that end ffmpeg count as errors (`FFMPEG_FAILURE`).
    """

    KEY = re.compile(r"^([a-z_0-9]+)=(\S*)$")
    STATS = re.compile(
        r"frame=\s*(\d+)(?:.*?fps=\s*([\d.]+))?.*?time=\s*(-?[\d:.]+)(?:.*?speed=\s*([\d.]+)x)?"
    )

    def __init__(
        self,
        total_frames: int | None = None,
        duration_seconds: float | None = None,
        *,
        error: Pattern | Sequence[Pattern] | None = FFMPEG_FAILURE,
        done: Pattern | Sequence[Pattern] | None = None,
    ) -> None:
        super().__init__(error=error, done=done)
        self._total = total_frames or None
        self._duration = duration_seconds or None
        self._block: dict[str, str] = {}
        self._ended = False

    def parse(self, text: str) -> list[Update]:
        updates = super().parse(text)
        if not text and self._ended:
            self._ended = False
            updates.append(Update("done", fraction=1.0, text="ffmpeg finished (progress=end)."))
        return updates

    def progress(self, line: str) -> list[Update]:
        key = self.KEY.match(line)
        if key is not None:
            self._block[key[1]] = key[2]
            if key[1] != "progress":
                return []
            block, self._block = self._block, {}
            if key[2] == "end":
                self._ended = True
            seconds = next(
                (
                    n / 1e6
                    for n in (_number(block.get("out_time_us")), _number(block.get("out_time_ms")))
                    if n is not None and n >= 0
                ),
                _clock_seconds(block.get("out_time", "")),
            )
            frame = _number(block.get("frame"))
            return [
                self._update(frame, seconds, _number(block.get("speed")), _number(block.get("fps")))
            ]
        stats = self.STATS.search(line)
        if stats is None:
            return [Update("line", text=line)]
        return [
            self._update(
                _number(stats[1]), _clock_seconds(stats[3]), _number(stats[4]), _number(stats[2])
            )
        ]

    def _update(
        self, frame: float | None, seconds: float | None, speed: float | None, fps: float | None
    ) -> Update:
        fraction = eta = None
        if self._total and frame is not None:
            fraction = min(1.0, frame / self._total)
            eta = max(0.0, (self._total - frame) / fps) if fps else None
        elif self._duration and seconds is not None:
            fraction = min(1.0, seconds / self._duration)
            eta = max(0.0, (self._duration - seconds) / speed) if speed else None
        parts = []
        if frame is not None:
            parts.append(f"frame {int(frame)}" + (f"/{self._total}" if self._total else ""))
        if seconds is not None:
            parts.append(f"{seconds:.1f}s" + (f" of {self._duration:g}s" if self._duration else ""))
        if speed:
            parts.append(f"{speed:g}x")
        data = {
            key: value
            for key, value in (("frame", frame), ("out_seconds", seconds), ("speed", speed))
            if value is not None
        }
        return Update("progress", fraction, eta, ", ".join(parts), phase="encoding", data=data)


def _fraction(value: object) -> float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return min(1.0, number / 100 if number > 1 else number)


def _eta(value: object) -> float | None:
    if isinstance(value, str) and ":" in value:
        return _clock_seconds(value)
    if isinstance(value, str) and value.strip().endswith(("s", "m", "h")):
        number = _number(value.strip()[:-1])
        scale = {"s": 1, "m": 60, "h": 3600}[value.strip()[-1]]
        return number * scale if number is not None else None
    number = _number(value)
    return number if number is not None and number >= 0 else None


class JsonStatusParser:
    """A JSON status file, such as a project's run.json: `status`, `step`, `progress` or `fraction`
    (0-1, or a percent), `eta` (seconds, "90s", "3m" or "00:03:00"). A status of done, success,
    complete or finished ends the watch as done; failed or error, as an error with the file's
    `error` or `message`. A `cost` or `output` field rides along in the event."""

    DONE = frozenset({"done", "success", "succeeded", "complete", "completed", "finished"})
    FAILED = frozenset({"failed", "failure", "error", "errored"})

    def parse(self, text: str) -> list[Update]:
        if not text.strip():
            return []
        try:
            value = json.loads(text)
        except ValueError:
            return [Update("line", text=text.strip()[:300])]
        if not isinstance(value, Mapping):
            return [Update("line", text=text.strip()[:300])]
        status = str(value.get("status") or "").strip()
        step = str(value.get("step") or "").strip()
        data = {
            key: value[key]
            for key in ("cost", "cost_usd", "output")
            if value.get(key) not in (None, "")
        }
        summary = ", ".join(part for part in (status, step) if part) or text.strip()[:200]
        if status.lower() in self.DONE:
            return [Update("done", fraction=1.0, text=summary, data=data)]
        if status.lower() in self.FAILED:
            reason = str(value.get("error") or value.get("message") or "").strip()
            return [Update("error", text=f"{summary}: {reason}" if reason else summary, data=data)]
        fraction = _fraction(value.get("progress", value.get("fraction")))
        return [
            Update(
                "progress",
                fraction=fraction,
                eta_seconds=_eta(value.get("eta")),
                text=summary,
                phase=step or status,
                data=data,
            )
        ]


class RegexParser(LineParser):
    """Any log with a counter. `progress` has two groups (done, total) or one (a percent); the
    default reads "120/300", as in `render: 120/300 frames`. `error` and
    `done` are the lines that end the watch (by default errors are `FAILURE`, and nothing is done:
    such a job's end shows as a stall unless something prints a done line)."""

    def __init__(
        self,
        progress: Pattern | None = r"(\d+)\s*/\s*(\d+)",
        *,
        error: Pattern | Sequence[Pattern] | None = FAILURE,
        done: Pattern | Sequence[Pattern] | None = None,
    ) -> None:
        super().__init__(error=error, done=done)
        self._progress = _patterns(progress)

    def progress(self, line: str) -> list[Update]:
        match = next((m for p in self._progress if (m := p.search(line))), None)
        groups = [g for g in (match.groups() if match else ()) if g is not None]
        numbers = [_number(g) for g in groups[:2]]
        fraction = None
        if len(numbers) == 2 and numbers[0] is not None and numbers[1]:
            if 0 <= numbers[0] <= numbers[1]:
                fraction = numbers[0] / numbers[1]
        elif len(numbers) == 1 and numbers[0] is not None and 0 <= numbers[0] <= 100:
            fraction = numbers[0] / 100
        if fraction is None:
            return [Update("line", text=line)]
        return [Update("progress", fraction=fraction, text=line)]


# -- choosing a parser ------------------------------------------------------------------------------

ParserKind = Literal["hyperframes", "ffmpeg", "json", "regex", "heartbeat"]
PARSER_KINDS: tuple[ParserKind, ...] = ("hyperframes", "ffmpeg", "json", "regex", "heartbeat")
DEFAULT_PARSER: ParserKind = "heartbeat"


def parser_for(
    kind: str = DEFAULT_PARSER,
    *,
    total_frames: int = 0,
    duration_seconds: float = 0,
    progress_regex: str = "",
    error_regex: str = "",
    done_regex: str = "",
) -> WatchParser:
    """The parser for output of `kind` (`PARSER_KINDS`).

    `error_regex` replaces the lines that mean failure; `done_regex` adds a line that means done;
    `progress_regex` is the `regex` kind's counter; `total_frames` or `duration_seconds` give
    `ffmpeg` a percentage. `json` reads a status file, so its source is `SandboxJsonSource`.
    Raises `ValueError` for an unknown kind and `re.error` for a bad pattern.
    """
    done = done_regex or None
    if kind == "hyperframes":
        return HyperFramesParser(error=error_regex or HYPERFRAMES_FAILURE, done=done)
    if kind == "ffmpeg":
        return FfmpegProgressParser(
            total_frames or None,
            duration_seconds or None,
            error=error_regex or FFMPEG_FAILURE,
            done=done,
        )
    if kind == "json":
        return JsonStatusParser()
    if kind == "regex":
        return RegexParser(
            progress_regex or r"(\d+)\s*/\s*(\d+)", error=error_regex or FAILURE, done=done
        )
    if kind == "heartbeat":
        return HeartbeatParser(error=error_regex or FAILURE, done=done)
    raise ValueError(f"unknown kind {kind!r}; use {', '.join(PARSER_KINDS)}")


# -- sources ----------------------------------------------------------------------------------------

#: One read returns at most this many new bytes. A log that grew more between polls is read from its
#: last `READ_BYTES`: the newest output is what says how the job is doing.
READ_BYTES = 256 * 1024
#: Seconds one sandbox command may run.
EXEC_TIMEOUT = 60
_MARK = "@@watch"
_END = "@@watch-end"


class WatchReadError(RuntimeError):
    """A source could not read (its sandbox command failed). The watch keeps polling; the failure
    goes into the stall message if reads keep failing."""


def _after_mark(output: str) -> tuple[str, str]:
    """The header line after the marker, and everything after it."""
    at = output.find(_MARK)
    if at < 0:
        return "", output
    header, _, body = output[at:].partition("\n")
    return header.strip(), body


class SandboxFileSource:
    """New bytes of a growing file in the sandbox, by byte offset: `wc -c`, then `tail -c +N`.

    A file that shrank (truncated, or rewritten from the start) is read again from the top. A file
    that doesn't exist yet reads as nothing, so a watch can start before the job writes its log.
    The output ends with a marker so a trailing newline survives transport, and stderr is kept out
    of it (some sandbox backends append stderr to stdout).
    """

    def __init__(
        self,
        exec: SandboxExec,
        path: str,
        *,
        max_bytes: int = READ_BYTES,
        timeout: int = EXEC_TIMEOUT,
    ) -> None:
        self.path = path
        self.offset = 0
        self.closed = False
        self.note = ""
        self._exec = exec
        self._max = max_bytes
        self._timeout = timeout

    def command(self) -> str:
        return (
            f"f={shlex.quote(self.path)}; o={self.offset}; cap={self._max}\n"
            f'if [ ! -f "$f" ]; then echo "{_MARK} missing"; exit 0; fi\n'
            's=$( { wc -c < "$f"; } 2>/dev/null | tr -d " \\n"); s=${s:-0}\n'
            'if [ "$s" -lt "$o" ]; then o=0; fi\n'
            'if [ $((s - o)) -gt "$cap" ]; then o=$((s - cap)); fi\n'
            f'echo "{_MARK} $s $o"\n'
            'tail -c +$((o + 1)) "$f" 2>/dev/null | head -c $((s - o)) 2>/dev/null\n'
            "printf '\\n" + _END + "\\n'"
        )

    async def read(self) -> str:
        result = await self._exec.execute(self.command(), timeout=self._timeout)
        header, body = _after_mark(result.output)
        if header == f"{_MARK} missing":
            self.offset = 0  # a file that comes back is a new file
            self.note = f"{self.path} does not exist yet"
            return ""
        fields = header.split()
        end = body.rfind("\n" + _END)
        if len(fields) != 3 or end < 0 or not all(f.isdigit() for f in fields[1:]):
            raise WatchReadError(f"reading {self.path} failed: {result.output.strip()[-300:]}")
        body = body[:end]
        size, start = int(fields[1]), int(fields[2])
        truncated = size < self.offset
        if start > 0 and start != self.offset:
            body = body.partition("\n")[2]  # it starts mid-line after the skipped bytes
        self.offset = size
        self.note = f"{self.path} was truncated; reading it again from the top" if truncated else ""
        return body


class SandboxJsonSource:
    """A small JSON status file (a project's run.json), re-read each poll and returned, in
    canonical form, when its content changes. A torn or half-written file is skipped until it
    parses."""

    def __init__(
        self,
        exec: SandboxExec,
        path: str,
        *,
        max_bytes: int = 64 * 1024,
        timeout: int = EXEC_TIMEOUT,
    ) -> None:
        self.path = path
        self.closed = False
        self.note = ""
        self._exec = exec
        self._max = max_bytes
        self._timeout = timeout
        self._last: str | None = None

    def command(self) -> str:
        return (
            f"f={shlex.quote(self.path)}\n"
            f'if [ -f "$f" ]; then echo "{_MARK} file"; head -c {self._max} "$f" 2>/dev/null; '
            f'else echo "{_MARK} missing"; fi'
        )

    async def read(self) -> str:
        result = await self._exec.execute(self.command(), timeout=self._timeout)
        header, body = _after_mark(result.output)
        if header == f"{_MARK} missing":
            self.note = f"{self.path} does not exist yet"
            return ""
        if header != f"{_MARK} file":
            raise WatchReadError(f"reading {self.path} failed: {result.output.strip()[-300:]}")
        try:
            value = json.loads(body)
        except ValueError:
            self.note = f"{self.path} is not valid JSON (yet)"
            return ""
        self.note = ""
        text = json.dumps(value, sort_keys=True)
        if text == self._last:
            return ""
        self._last = text
        return text


