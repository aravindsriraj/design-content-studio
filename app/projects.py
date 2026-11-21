"""Project state from the workspace's sandbox (DA tools/projects.py): recent projects and one
project's render status. Same outputs as design-agent's; read with one sandbox command each."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from google.adk.tools import ToolContext

from app import sandbox

PROJECTS_ROOT = "/workspace/projects"
OUTPUT_SUFFIXES = (".mp4", ".mov", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg", ".wav", ".mp3")
PROJECT_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
STAGE_FILES = ("BRIEF.md", "PLAN.md", "run.json", "DELIVERY.json")
RUN_FIELDS = ("status", "phase", "step", "eta", "cost_usd", "started_at", "finished_at")


def _renders(names: list[str]) -> list[str]:
    return sorted(n for n in names if n.lower().endswith(OUTPUT_SUFFIXES))


async def list_recent_projects(tool_context: ToolContext, limit: int = 10) -> str:
    """List recent projects in your computer and whether each has renders.

    Renders existing does not mean anything was delivered.

    Args:
      limit: 1 to 50.
    """
    env = await sandbox.for_workspace(tool_context.user_id)
    r = await env.execute(
        f"cd {PROJECTS_ROOT} 2>/dev/null || exit 0; for d in */; do d=${{d%/}}; [ -d \"$d\" ] || continue; "
        "printf '%s\\t%s\\t' \"$(date -u -r \"$d\" +%Y-%m-%dT%H:%M:%SZ)\" \"$d\"; "
        "ls \"$d/renders\" 2>/dev/null | tr '\\n' '|'; echo; done", timeout=60)
    rows = []
    for line in (r.stdout or "").splitlines():
        modified, name, renders = [*line.split("\t"), "", ""][:3]
        if PROJECT_SLUG.fullmatch(name):
            rows.append((modified, name, _renders([x for x in renders.split("|") if x])))
    if not rows:
        return "No projects yet."
    rows.sort(reverse=True)
    capped = max(1, min(limit, 50))
    lines = [f"- {n} (updated {m}): {f'{len(o)} render(s)' if o else 'in progress'}" for m, n, o in rows[:capped]]
    return f"{len(rows)} project(s); {min(capped, len(rows))} most recent:\n" + "\n".join(lines)


async def get_render_status(project_slug: str, tool_context: ToolContext) -> str:
    """One project's files on disk, its renders and the status fields of its run.json.

    Args:
      project_slug: One project folder name under /workspace/projects.
    """
    if not PROJECT_SLUG.fullmatch(project_slug):
        return "Project name is not allowed; use one project folder name."
    project = f"{PROJECTS_ROOT}/{project_slug}"
    env = await sandbox.for_workspace(tool_context.user_id)
    r = await env.execute(f"[ -d {shlex.quote(project)} ] || exit 3; ls -1 {shlex.quote(project)}; echo '@@renders@@'; "
                          f"ls -1 {shlex.quote(project)}/renders 2>/dev/null", timeout=30)
    if r.exit_code == 3:
        return f"No project named '{project_slug}'."
    top, _, renders = (r.stdout or "").partition("@@renders@@")
    names = set(top.split())
    stages = ", ".join(f"{n} {'yes' if n in names else 'no'}" for n in STAGE_FILES)
    summary = "not written yet"
    if "run.json" in names:
        try:
            data = json.loads(await env.read_file(Path(f"{project}/run.json")))
            picked = {k: data[k] for k in RUN_FIELDS if isinstance(data, dict) and k in data}
            summary = json.dumps(picked) if picked else "present"
        except (ValueError, UnicodeError, FileNotFoundError):
            summary = "present (unreadable)"
    return "\n".join([project_slug, f"files: {stages}",
                      "renders: " + (", ".join(_renders(renders.split())) or "none yet"), f"run.json: {summary}"])
