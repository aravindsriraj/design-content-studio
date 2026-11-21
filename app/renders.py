"""Long renders and watched logs (DA middleware/watch_tools.py + harness/offload.py), made durable.

`start_render_job` runs a command detached in the workspace sandbox (`nohup setsid`, output in a log,
exit code in `<log>.exit`) and starts a watcher; `watch_progress` watches a log or status file the
lead started itself. The watcher is a background task (`app.tasks`, effort "render") whose job polls
the sandbox, parses progress with design-agent's parsers (`app.watch`), keeps the task row's progress
line current, says once when output stalls, and ends with the exit code (or the parser's done/error)
and the log's tail. Its end wakes the lead like any helper's. A restarted watcher re-reads the log.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
import uuid

from google.adk.tools import ToolContext

from app import jobs, sandbox, tasks
from app.looks import _Exec
from app.watch import (
    FAILURE,
    TAIL_LINES,
    SandboxFileSource,
    SandboxJsonSource,
    parser_for,
)

POLL_SECONDS = 5
STALL_MINUTES = 5.0
KINDS = ("hyperframes", "ffmpeg", "json", "regex", "heartbeat")
_LABEL = re.compile(r"[^A-Za-z0-9_-]+")


def _under_workspace(path: str) -> str:
    p = path if path.startswith("/") else f"/workspace/{path}"
    if ".." in p.split("/") or not p.startswith("/workspace/"):
        raise ValueError(f"{path} is outside /workspace")
    return p


async def _start_watch(tool_context: ToolContext, spec: dict, label: str) -> dict:
    t = await tasks.start(tool_context.user_id, tool_context.session.id, json.dumps(spec), "render",
                          f"render: {label}", tool_context._invocation_context.session_service)
    return {"status": "watching", "id": t["id"][:8], "label": t["label"], "log": spec["path"],
            "next": "The output is not ready yet. Tell the person what is rendering, then end your turn; "
                    "you are woken when it exits. Do not use the output before then."}


async def start_render_job(command: str, label: str, tool_context: ToolContext, log_path: str = "",
                           kind: str = "hyperframes", workdir: str = "", stall_minutes: float = STALL_MINUTES,
                           total_frames: int = 0, duration_seconds: float = 0, progress_regex: str = "") -> dict:
    """Run a long command detached in your computer and watch it until it exits.

    For renders and encodes that take minutes. It runs in `workdir` (default /workspace), outlives
    your turn, writes its output to `log_path` (default /workspace/.renders/<label>-<id>.log) and
    its exit code to `<log_path>.exit`. Returns an id at once; you are woken when it exits (with the
    end of its log on failure) or shows nothing new for `stall_minutes`. Progress is read from the
    log as `kind` (hyperframes, ffmpeg, regex or heartbeat). cancel_task with the id stops it.

    Its output does not exist until you are woken: never read, mix, check or deliver it before
    then; end your turn instead. A render you expect to take under two minutes (a short sting, a
    draft) is simpler run directly with `bash` and waited for.

    Args:
      command: The shell command, e.g. `hyperframes render . --output renders/film.mp4`.
      label: A few words naming the job.
      log_path: Where its output goes (under /workspace).
      kind: How to read progress from the log.
      workdir: Where it runs (under /workspace).
      stall_minutes: Silence this long counts as a stall.
      total_frames: For ffmpeg progress, the frame count.
      duration_seconds: For ffmpeg progress, the output duration.
      progress_regex: For kind regex: groups done/total, or a percent.
    """
    if kind not in KINDS or kind == "json":
        return {"status": "not started", "error": "kind must be hyperframes, ffmpeg, regex or heartbeat"}
    rid = uuid.uuid4().hex[:6]
    try:
        wd = _under_workspace(workdir or "/workspace")
        log = _under_workspace(log_path or f"/workspace/.renders/{_LABEL.sub('-', label)[:40] or 'job'}-{rid}.log")
        parser_for(kind, total_frames=total_frames, duration_seconds=duration_seconds, progress_regex=progress_regex)
    except (ValueError, re.error) as e:
        return {"status": "not started", "error": str(e)}
    q = shlex.quote
    inner = f"cd {q(wd)} && ( {command} ) > {q(log)} 2>&1; echo $? > {q(log + '.exit')}"
    launch = (f"mkdir -p {q(log.rsplit('/', 1)[0])} && rm -f {q(log + '.exit')} && "
              f"nohup setsid sh -c {q(inner)} > /dev/null 2>&1 & echo $!")
    env = await sandbox.for_workspace(tool_context.user_id)
    r = await env.execute(launch, timeout=30)
    pid = (r.stdout or "").strip().splitlines()[-1:] or [""]
    if r.exit_code != 0 or not pid[0].isdigit():
        return {"status": "not started", "error": (r.stderr or r.stdout)[-400:]}
    spec = {"path": log, "kind": kind, "pid": int(pid[0]), "exit_file": log + ".exit", "stall_minutes": stall_minutes,
            "total_frames": total_frames, "duration_seconds": duration_seconds, "progress_regex": progress_regex,
            "command": command}
    return await _start_watch(tool_context, spec, label)


async def watch_progress(path: str, label: str, tool_context: ToolContext, kind: str = "heartbeat",
                         stall_minutes: float = STALL_MINUTES, total_frames: int = 0, duration_seconds: float = 0,
                         progress_regex: str = "", error_regex: str = "", done_regex: str = "",
                         timeout_minutes: float = 0) -> dict:
    """Watch a log or status file in your computer, and hear when the job ends.

    For long work you started yourself. Returns a watch id at once; you are woken when the job
    finishes, fails, or shows nothing new for `stall_minutes`. Watching only reads the file, so
    stopping the watch leaves the job running; for a command not yet started, use
    start_render_job, which also knows its exit code.

    Args:
      path: The log or status file under /workspace.
      label: A few words naming the job.
      kind: hyperframes, ffmpeg (pass total_frames or duration_seconds), json (a status file with
        status, step, progress or fraction, eta), regex (progress_regex) or heartbeat.
      stall_minutes: Silence this long counts as a stall.
      total_frames: ffmpeg frame count.
      duration_seconds: ffmpeg output duration.
      progress_regex: The regex kind's counter.
      error_regex: Lines that mean failure (replaces the default).
      done_regex: A line that means done.
      timeout_minutes: Above 0, end the watch after this long.
    """
    try:
        p = _under_workspace(path)
        parser_for(kind, total_frames=total_frames, duration_seconds=duration_seconds,
                   progress_regex=progress_regex, error_regex=error_regex, done_regex=done_regex)
    except (ValueError, re.error) as e:
        return {"status": "not started", "error": str(e)}
    spec = {"path": p, "kind": kind, "stall_minutes": stall_minutes, "total_frames": total_frames,
            "duration_seconds": duration_seconds, "progress_regex": progress_regex, "error_regex": error_regex,
            "done_regex": done_regex, "timeout_minutes": timeout_minutes}
    return await _start_watch(tool_context, spec, label)


def _line(fraction, phase, eta) -> str:
    bits = [f"{round(fraction * 100)}%" if fraction is not None else "", phase,
            f"~{max(1, round(eta / 60))} min left" if eta else ""]
    return " · ".join(b for b in bits if b) or "running"


async def run_watch(task: dict, spec: dict, *, poll_seconds: float = POLL_SECONDS, on_beat=None) -> tuple[str, str]:
    """The watcher job: poll until the job ends. Returns (status, report) for the task."""
    env = await sandbox.for_workspace(task["workspace_id"])
    ex = _Exec(env)
    kind = spec.get("kind", "heartbeat")
    source = (SandboxJsonSource if kind == "json" else SandboxFileSource)(ex, spec["path"])
    parser = parser_for(kind, total_frames=spec.get("total_frames") or 0,
                        duration_seconds=spec.get("duration_seconds") or 0,
                        progress_regex=spec.get("progress_regex") or "", error_regex=spec.get("error_regex") or "",
                        done_regex=spec.get("done_regex") or "")
    tail: list[str] = []
    fraction = eta = None
    phase, shown, stalled = "", "", False
    started = last_output = time.monotonic()
    stall_s = float(spec.get("stall_minutes") or STALL_MINUTES) * 60
    timeout_s = float(spec.get("timeout_minutes") or 0) * 60
    verdict: tuple[str, str] | None = None
    while verdict is None:
        current = await tasks.get(task["id"])
        if current and current["status"] == "cancelled":
            if spec.get("pid"):
                await env.execute(f"kill -TERM -- -{int(spec['pid'])} 2>/dev/null || kill {int(spec['pid'])} 2>/dev/null", timeout=30)
            return "cancelled", "Stopped by the lead."
        text = await source.read()
        if text:
            last_output = time.monotonic()
            if stalled:
                stalled = False
                await tasks.update(task["id"], progress="[urgent] output resumed after a stall")
        for u in parser.parse(text):
            if u.text:
                tail = [*tail, u.text][-TAIL_LINES:]
            if u.kind == "progress":
                fraction = u.fraction if u.fraction is not None else fraction
                eta, phase = u.eta_seconds or eta, u.phase or phase
            elif u.kind == "error" and not spec.get("exit_file"):
                verdict = ("failed", u.text)
            elif u.kind == "done" and not spec.get("exit_file"):
                verdict = ("succeeded", u.text or "done")
        if spec.get("exit_file"):
            r = await env.execute(f"cat {shlex.quote(spec['exit_file'])} 2>/dev/null", timeout=30)
            code = (r.stdout or "").strip()
            if code.lstrip("-").isdigit():
                await asyncio.sleep(0)  # the log is complete once the exit file exists
                for u in parser.parse(await source.read()) + parser.parse(""):
                    if u.text:
                        tail = [*tail, u.text][-TAIL_LINES:]
                verdict = ("succeeded" if code == "0" else "failed", f"exit {code}")
        now = time.monotonic()
        line = _line(fraction, phase, eta)
        if verdict is None and line != shown:
            shown = line
            await tasks.update(task["id"], progress=line)
        if verdict is None and not stalled and now - last_output > stall_s:
            stalled = True
            await tasks.update(task["id"], progress=f"[urgent] no new output for {round(stall_s / 60)} min ({line})")
            await _wake_now(task, f"render \"{task['label']}\" shows no new output for {round(stall_s / 60)} min "
                                  f"({line}); its log is {spec['path']}. It may be stuck (silence is not success).")
        if verdict is None and timeout_s and now - started > timeout_s:
            verdict = ("failed", f"timed out after {round(timeout_s / 60)} min")
        if verdict is None:
            if on_beat:
                await on_beat()
            await asyncio.sleep(poll_seconds)
    status, why = verdict
    report = f"{why}. Log: {spec['path']}\nLast lines:\n" + "\n".join(tail[-TAIL_LINES:])
    return status, report


async def _wake_now(task: dict, text: str) -> None:
    ws, parent = task["workspace_id"], task["parent_session_id"]
    if await jobs.active_for_session(ws, parent):
        return  # the lead is mid-turn; the progress line carries it
    last = await jobs.latest_for_session(ws, parent)
    try:
        job = await jobs.create(ws, parent, {"role": "user", "parts": [{"text": f"[Background notice, not from the person] {text}"}]},
                                last.get("notify_email") if last else None)
        await jobs.dispatch(job["id"])
    except jobs.Busy:
        pass


__all__ = ["FAILURE", "run_watch", "start_render_job", "watch_progress"]
