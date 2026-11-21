"""Several creative looks at once: copies of one project, made in parallel, one picked and merged.

Vendored from design-agent `src/design_agent/harness/branches.py` (Apache-2.0, LangChain), the
`Looks` core unchanged. Left out: `LookWatch` (DA's event bus; here each look's helper is a row in
`app.tasks`, and its end wakes the lead) and `SnapshotBranches` (a sandbox per branch; DA wires
nothing to it by default). The two protocols it needs are restated below.

A person picks between looks better than a model does: vision-language judges trail people on
video pairs and flip when the pair is swapped. So a helper makes each look, the looks go side by
side, and a person picks. Looks are made at the cheap stage (stills and a storyboard, not a
render), where running several is affordable, and only the winner is built.

Each look is a folder, `<project>/looks/<id>/`: a copy of the project without `looks/`, `out/` and
`node_modules/`. One helper works there in one direction and writes `look.json`
(`{id, direction, summary, stills}`) when it is done. `pick` backs the project up to
`looks/_backup-<ts>/`, then copies the chosen look's files over it, newer wins, so an edit made to
the project while the looks ran survives.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecResult:  # design_agent.harness.protocols.ExecResult
    output: str
    exit_code: int | None = 0


class SandboxExec(Protocol):  # design_agent.harness.protocols.SandboxExec
    async def execute(self, command: str, *, timeout: int | None = None) -> ExecResult: ...


class HelperRunner(Protocol):  # design_agent.harness.protocols.HelperRunner (start only)
    async def start(self, brief: str, *, effort: str, label: str = "", wave: str | None = None) -> str: ...

#: Top-level entries a look leaves out of its copy, and a pick leaves out of its merge.
EXCLUDED = ("looks", "out", "node_modules")
LOOKS_DIR = "looks"
LOOK_FILE = "look.json"
#: Where each look's direction is kept, outside the look's folder so a pick never merges it.
META_DIR = ".meta"
BACKUP_PREFIX = "_backup-"
#: A look id: short, one path segment, never `_backup-…` or `.meta`.
LOOK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
#: Terminal task events: how a look's helper can end.

_PREPARED = "@@prepared@@ "
_LOOK = "@@look@@ "
_META = "@@meta@@"
_JSON = "@@json@@"
_CHANGED = "@@changed@@ "
_BACKUP = "@@backup@@ "

DEFAULT_TEMPLATE = """\
ROLE
You make one look for the project "{project}": one creative direction, shown as a few stills or a
storyboard, so a person can put the looks side by side and pick one to build. This is the cheap
stage: stills now, the full build after the pick.

THE JOB
{context}

YOUR DIRECTION (look {id})
{direction}

WHERE YOU WORK
- {dir}/ is a full copy of the project, made for this look. Its node_modules, when the project has
  one, links to the project's, so the renderer runs from there as it does from the project.
- Other helpers are making the other looks in the folders beside yours, under {project_dir}/looks/,
  at the same time, and {project_dir}/ itself is the person's current version. So everything you
  change goes in {dir}/.
- If this look is picked, the files you changed in {dir}/ are copied over the project. So make the
  look in the project's own files (compositions, styles, copy, assets), the way the project would
  carry it.
- {dir}/out/ is yours for renders and stays out of that copy.

DELIVERABLES
1. The look, in the project's files under {dir}/, as far as the stills need.
2. Stills or a storyboard that show the direction at its key moments, rendered into {dir}/out/. The
   person compares the looks from these and your summary alone.
3. Last, {look_json}:
   {{"id": "{id}", "direction": "<the direction, as given>", "summary": "<two or three sentences:
   what this look does and why it fits>", "stills": ["<absolute paths, in viewing order>"]}}

REPORT
Your final message: the summary, the stills' paths, and what you would change with more time.
"""

#: What a look's brief says under THE JOB when the lead gives no shared context.
DEFAULT_CONTEXT = (
    "The direction below is the whole ask. The project's own brief files (BRIEF.md, brief.yaml, "
    "outline.yaml), where they exist, say what it is for."
)


class LooksError(RuntimeError):
    """A look could not be prepared, started, read or merged. The message is safe to show."""

    def __init__(self, message: str, *, started: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        #: Looks whose helpers did start before the failure (look id -> task id).
        self.started = dict(started or {})


@dataclass(frozen=True)
class LookSpec:
    """One look to make: its id (a folder name) and the creative direction its helper follows."""

    id: str
    direction: str


@dataclass(frozen=True)
class LookResult:
    """One look as it stands on disk. `ready` means its helper wrote a readable `look.json`."""

    id: str
    direction: str
    summary: str
    stills: list[str]
    dir: str
    ready: bool


@dataclass(frozen=True)
class Picked:
    """A merged look: the project files it changed (relative paths) and where the backup is."""

    look_id: str
    changed: list[str]
    backup: str


def looks_root(project_dir: str) -> str:
    return posixpath.join(_project(project_dir), LOOKS_DIR)


def look_dir(project_dir: str, look_id: str) -> str:
    return posixpath.join(looks_root(project_dir), _look_id(look_id))


def next_ids(existing: Iterable[str], count: int) -> list[str]:
    """`count` fresh look ids after the ones on disk: a..z, then a2..z2, and so on.

    Letters read well when a person picks ("b, but warmer"), and a second round continues where the
    first stopped, so every look in a thread keeps its own letter.
    """
    taken = set(existing)
    found: list[str] = []
    round_ = 1
    while len(found) < count:
        suffix = "" if round_ == 1 else str(round_)
        for letter in "abcdefghijklmnopqrstuvwxyz":
            candidate = f"{letter}{suffix}"
            if candidate not in taken:
                found.append(candidate)
                if len(found) == count:
                    break
        round_ += 1
    return found


def _project(project_dir: str) -> str:
    """An absolute, normalized project path at least two levels deep (never `/` or `/tmp`)."""
    if (
        not project_dir.startswith("/")
        or posixpath.normpath(project_dir) != project_dir
        or len([part for part in project_dir.split("/") if part]) < 2
        or any(ch in project_dir for ch in "\x00\n\r")
    ):
        raise LooksError(f"{project_dir!r} is not a project folder path.")
    return project_dir


def _look_id(look_id: str) -> str:
    if not LOOK_ID.fullmatch(look_id):
        raise LooksError(
            f"{look_id!r} is not a look id: use letters, digits, '-' or '_', starting with a "
            "letter or digit."
        )
    return look_id


def _tail(output: str, limit: int = 600) -> str:
    text = output.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _copy_function(use_rsync: bool | None) -> str:
    """Shell: `copy_tree SRC DEST` makes DEST a fresh copy of SRC without the excluded entries.

    rsync when the sandbox has it (anchored excludes, so `src/looks/` is still copied); otherwise
    `cp -a` of every other top-level entry into a folder emptied first.
    """
    if use_rsync is None:
        detect = "if command -v rsync >/dev/null 2>&1; then HAVE_RSYNC=1; else HAVE_RSYNC=0; fi"
    else:
        detect = f"HAVE_RSYNC={1 if use_rsync else 0}"
    excludes = " ".join(f"--exclude=/{name}" for name in EXCLUDED)
    names = "|".join(EXCLUDED)
    return f"""{detect}
copy_tree() {{
  if [ "$HAVE_RSYNC" = 1 ]; then
    mkdir -p "$2"
    rsync -a --delete --delete-excluded {excludes} "$1"/ "$2"/
  else
    rm -rf "$2"
    mkdir -p "$2"
    for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
      [ -e "$entry" ] || [ -L "$entry" ] || continue
      case "${{entry##*/}}" in {names}) continue ;; esac
      cp -a "$entry" "$2"/
    done
  fi
}}
"""


# The no-rsync merge: every file or link in the look (not the excluded entries, not look.json) that
# differs from the project's copy and is not older than it is copied over, and named.
_MERGE_FALLBACK = """cd "$look"
find . \\( -path ./look.json {prunes} \\) -prune -o \\( -type f -o -type l \\) -print |
while IFS= read -r f; do
  rel=${{f#./}}
  dst="$project/$rel"
  if [ -L "$f" ]; then
    if [ -L "$dst" ] && [ "$(readlink "$f")" = "$(readlink "$dst")" ]; then continue; fi
  elif [ -e "$dst" ] && [ ! -L "$dst" ]; then
    if cmp -s "$f" "$dst"; then continue; fi
    if [ "$dst" -nt "$f" ]; then continue; fi
  fi
  mkdir -p "$(dirname "$dst")"
  rm -f "$dst"
  cp -a "$f" "$dst"
  printf '{changed}%s\\n' "$rel"
done
"""


class _Keep(dict[str, str]):
    """Leaves an unknown `{placeholder}` in a custom template as it is."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Looks:
    """Make, read and merge looks in one project, through a `SandboxExec`.

    `use_rsync` forces the copy method; `None` (the default) uses rsync when the sandbox has it.
    """

    def __init__(
        self,
        sandbox: SandboxExec,
        *,
        use_rsync: bool | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sandbox = sandbox
        self._use_rsync = use_rsync
        self._clock = clock

    async def _run(self, script: str, *, timeout: int, what: str, ends_with: str = "") -> str:
        """Run a script; a non-zero exit, or an unknown one without the script's last marker,
        is a failure."""
        result = await self._sandbox.execute(script, timeout=timeout)
        output = result.output or ""
        finished = result.exit_code == 0 or (
            result.exit_code is None and (not ends_with or ends_with in output)
        )
        if not finished:
            raise LooksError(f"{what} failed (exit {result.exit_code}): {_tail(output)}")
        return output

    def _prepare_script(
        self, project_dir: str, look_ids: Sequence[str], directions: Mapping[str, str]
    ) -> str:
        project = _project(project_dir)
        q = shlex.quote
        lines = [
            "set -eu",
            _copy_function(self._use_rsync),
            f"project={q(project)}",
            '[ -d "$project" ] || { echo "no project folder at $project"; exit 3; }',
            f'mkdir -p "$project/{LOOKS_DIR}/{META_DIR}"',
        ]
        for look_id in look_ids:
            dest = f'"$project/{LOOKS_DIR}/{_look_id(look_id)}"'
            lines += [
                f'copy_tree "$project" {dest}',
                f"mkdir -p {dest}/out",
                'if [ -d "$project/node_modules" ]; then '
                f"ln -sfn ../../node_modules {dest}/node_modules; fi",
            ]
            if look_id in directions:
                meta = json.dumps(
                    {"id": look_id, "direction": directions[look_id], "at": self._clock()}
                )
                lines.append(
                    f"printf '%s\\n' {q(meta)} > \"$project/{LOOKS_DIR}/{META_DIR}/{look_id}.json\""
                )
            lines.append(f"echo {q(_PREPARED + look_id)}")
        return "\n".join(lines) + "\n"

    async def prepare(self, project_dir: str, look_ids: Sequence[str]) -> list[str]:
        """Copy the project into `looks/<id>/` for each id (a fresh copy each time) and return the
        folders. One sandbox command for all of them."""
        return await self._prepare(project_dir, look_ids, {})

    async def _prepare(
        self, project_dir: str, look_ids: Sequence[str], directions: Mapping[str, str]
    ) -> list[str]:
        if not look_ids or len(set(look_ids)) != len(look_ids):
            raise LooksError("Give each look its own id.")
        script = self._prepare_script(project_dir, look_ids, directions)
        await self._run(
            script, timeout=900, what="Preparing the looks", ends_with=_PREPARED + look_ids[-1]
        )
        return [look_dir(project_dir, look_id) for look_id in look_ids]

    def brief_for(
        self,
        spec: LookSpec,
        project_dir: str,
        template: str = DEFAULT_TEMPLATE,
        *,
        context: str = "",
    ) -> str:
        """The brief for the helper that makes one look: its direction, its folder and what it
        leaves there (stills in `out/`, then `look.json`)."""
        folder = look_dir(project_dir, spec.id)
        values = _Keep(
            id=spec.id,
            direction=spec.direction.strip(),
            dir=folder,
            project_dir=_project(project_dir),
            project=posixpath.basename(project_dir),
            look_json=posixpath.join(folder, LOOK_FILE),
            out=posixpath.join(folder, "out"),
            context=context.strip() or DEFAULT_CONTEXT,
        )
        return template.format_map(values)

    async def start(
        self,
        project_dir: str,
        specs: Sequence[LookSpec],
        runner: HelperRunner,
        effort: str = "xhigh",
        template: str = DEFAULT_TEMPLATE,
        *,
        context: str = "",
    ) -> dict[str, str]:
        """Prepare every look, then start one helper per look, all in one wave. Returns look id ->
        task id. A helper that cannot start raises `LooksError`, naming those already running."""
        if not specs:
            raise LooksError("Give at least one direction.")
        directions = {spec.id: spec.direction.strip() for spec in specs}
        await self._prepare(project_dir, [spec.id for spec in specs], directions)
        wave = f"looks {posixpath.basename(project_dir)}"
        started: dict[str, str] = {}
        for spec in specs:
            brief = self.brief_for(spec, project_dir, template, context=context)
            label = f"look {spec.id}: {_short(spec.direction)}"
            try:
                started[spec.id] = await runner.start(brief, effort=effort, label=label, wave=wave)
            except Exception as error:  # reported with what did start
                raise LooksError(
                    f"The helper for look {spec.id} did not start: {error}", started=started
                ) from error
        return started

    async def collect(self, project_dir: str) -> list[LookResult]:
        """Every look folder in the project and what its helper left there, sorted by id."""
        root = looks_root(project_dir)
        script = f"""set -u
cd {shlex.quote(root)} 2>/dev/null || exit 0
for d in */; do
  [ -d "$d" ] || continue
  id=${{d%/}}
  case "$id" in {BACKUP_PREFIX}*) continue ;; esac
  printf '\\n{_LOOK}%s\\n' "$id"
  if [ -f "{META_DIR}/$id.json" ]; then printf '{_META}\\n'; cat "{META_DIR}/$id.json"; echo; fi
  if [ -f "$id/{LOOK_FILE}" ]; then printf '{_JSON}\\n'; cat "$id/{LOOK_FILE}"; echo; fi
done
"""
        output = await self._run(script, timeout=60, what="Reading the looks")
        return _parse_looks(output, root)

    async def pick(self, project_dir: str, look_id: str) -> Picked:
        """Merge a look into the project: back the project up to `looks/_backup-<ts>/`, then copy
        the look's files over it (not `look.json`, `out/` or `node_modules/`), skipping any file
        the project holds a newer version of. Returns the changed files and the backup folder."""
        project = _project(project_dir)
        folder = look_dir(project, look_id)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._clock()))
        backup = posixpath.join(looks_root(project), f"{BACKUP_PREFIX}{stamp}")
        q = shlex.quote
        skipped = [LOOK_FILE, *EXCLUDED]
        excludes = " ".join(f"--exclude=/{name}" for name in skipped)
        prunes = " ".join(f"-o -path ./{name}" for name in EXCLUDED)
        fallback = _MERGE_FALLBACK.format(prunes=prunes, changed=_CHANGED)
        script = f"""set -eu
{_copy_function(self._use_rsync)}
project={q(project)}
look={q(folder)}
backup={q(backup)}
[ -d "$look" ] || {{ echo "no look folder at $look"; exit 3; }}
copy_tree "$project" "$backup"
if [ "$HAVE_RSYNC" = 1 ]; then
  rsync -a -u --out-format={q(_CHANGED + "%n")} {excludes} "$look"/ "$project"/
else
{fallback}fi
echo {q(_BACKUP)}"$backup"
"""
        output = await self._run(
            script, timeout=900, what=f"Merging look {look_id}", ends_with=_BACKUP
        )
        changed: list[str] = []
        for line in output.splitlines():
            if line.startswith(_CHANGED):
                name = line[len(_CHANGED) :].strip()
                if name and not name.endswith("/") and name not in changed:
                    changed.append(name)
        return Picked(look_id=look_id, changed=changed, backup=backup)


def _short(text: str, limit: int = 48) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def _load(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _sort_key(look_id: str) -> tuple[int, str]:
    return (len(look_id), look_id)


def _parse_looks(output: str, root: str) -> list[LookResult]:
    sections: dict[str, dict[str, list[str]]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        if line.startswith(_LOOK):
            look_id = line[len(_LOOK) :].strip()
            sections[look_id] = {"meta": [], "json": []}
            current = None
        elif line == _META and sections:
            current = sections[next(reversed(sections))]["meta"]
        elif line == _JSON and sections:
            current = sections[next(reversed(sections))]["json"]
        elif current is not None:
            current.append(line)
    results: list[LookResult] = []
    for look_id in sorted(sections, key=_sort_key):
        folder = posixpath.join(root, look_id)
        meta = _load("\n".join(sections[look_id]["meta"])) or {}
        written = _load("\n".join(sections[look_id]["json"]))
        stills: list[str] = []
        summary = ""
        direction = str(meta.get("direction") or "")
        if written is not None:
            summary = str(written.get("summary") or "").strip()
            direction = str(written.get("direction") or direction).strip()
            raw = written.get("stills")
            for still in raw if isinstance(raw, list) else []:
                if isinstance(still, str) and still.strip():
                    path = still.strip()
                    stills.append(path if path.startswith("/") else posixpath.join(folder, path))
        results.append(
            LookResult(
                id=look_id,
                direction=direction,
                summary=summary,
                stills=stills,
                dir=folder,
                ready=written is not None and bool(summary or stills),
            )
        )
    return results


# -- hearing the looks end -------------------------------------------------------------------------




# ---------------------------------------------------------------- Studio tools (DA middleware/looks_tools.py)

PROJECTS = "/workspace/projects"
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def _project_dir(slug: str) -> str | None:
    slug = slug.strip().strip("/")
    return f"{PROJECTS}/{slug}" if _SLUG.fullmatch(slug) and ".." not in slug else None


class _Exec:
    """SandboxExec over the workspace sandbox."""

    def __init__(self, env) -> None:
        self._env = env

    async def execute(self, command: str, *, timeout: int | None = None) -> ExecResult:
        r = await self._env.execute(command, timeout=timeout or 120)
        return ExecResult(output=(r.stdout or "") + (r.stderr or ""), exit_code=r.exit_code)


class _Runner:
    """HelperRunner over app.tasks: each look is a background helper on the lead's session."""

    def __init__(self, tool_context) -> None:
        self._ctx = tool_context

    async def start(self, brief: str, *, effort: str, label: str = "", wave: str | None = None) -> str:
        from app import tasks

        t = await tasks.start(self._ctx.user_id, self._ctx.session.id, brief, effort, label or "look",
                              self._ctx._invocation_context.session_service)
        return t["id"][:8]


async def start_looks(project_slug: str, directions: list[str], tool_context, effort: str = "xhigh",
                      context: str = "") -> str:
    """Make several looks for a project at once, one background helper per direction, so the
    person can compare them side by side and pick one to build.

    Each look is a copy of /workspace/projects/<project_slug>/ in its `looks/<id>/` folder (ids are
    letters: a, b, c, then d onward for a later round). Its helper makes the look in the project's
    own files, renders a few stills into `looks/<id>/out/`, and writes `looks/<id>/look.json`
    ({id, direction, summary, stills}). This is the cheap stage. Choosing is the person's:
    vision-language judges trail people by more than 30 points on pairs.

    It returns at once. Each helper's end wakes you. When the last has ended, deliver each look's
    stills (named so the look id shows) and ask the person to pick; merge their choice with pick_look.

    Args:
      project_slug: The project folder name under /workspace/projects.
      directions: One short line per look, each a different direction.
      effort: The helpers' effort: low, high, xhigh or max.
      context: What every look shares (the ask, the audience, the files to read first), written once.
    """
    from app import sandbox

    project = _project_dir(project_slug)
    if project is None:
        return "Project name is not allowed; use one project directory name."
    wanted = [" ".join(d.split()) for d in directions if d and d.strip()]
    if not wanted:
        return "Give at least one direction."
    looks = Looks(_Exec(await sandbox.for_workspace(tool_context.user_id)))
    try:
        existing = [result.id for result in await looks.collect(project)]
    except LooksError as error:
        return f"No looks were started: {error}"
    ids = next_ids(existing, len(wanted))
    specs = [LookSpec(look_id, d) for look_id, d in zip(ids, wanted, strict=True)]
    try:
        started = await looks.start(project, specs, _Runner(tool_context), effort, context=context)
        failure = ""
    except LooksError as error:
        started, failure = error.started, str(error)
    if failure:
        if not started:
            return f"No looks were started: {failure}"
        running = ", ".join(f"{i} ({t})" for i, t in started.items())
        return f"{failure}. Already running: {running}; each wakes you when it ends."
    rows = "\n".join(f"- {spec.id} ({started[spec.id]}): {_short(spec.direction)}" for spec in specs)
    return (
        f"Started {len(started)} looks for {project_slug} ({effort}, one wave):\n{rows}\n"
        f"Each works in {project}/looks/<id>/ and ends with stills in its out/ and a look.json. Each "
        "one's end wakes you; when the last has ended, show the person every look's stills and ask "
        "them to pick. Tell them what is being made and when to expect it, then end your turn."
    )


async def _record(workspace_id: str, skill: str, slug: str, chosen: LookResult, results: list, reason: str) -> str:
    """DA looks_tools._record: the pick as a kept observation (the person's own words), so later
    jobs in this skill see what they chose and why."""
    from app.harness.learning import ObservationStore
    from app.learning import WorkspaceStore

    others = [f'"{_short(r.direction, 80)}"' for r in results if r.id != chosen.id and r.ready and r.direction]
    text = f'The person picked the look "{_short(chosen.direction or chosen.id, 120)}"'
    if others:
        text += f" over {', '.join(others)}"
    said = " ".join(reason.split())
    if said:
        text += f', saying "{_short(said, 200)}"'
    evidence = [f"{slug}, {time.strftime('%Y-%m-%d', time.gmtime())}: look {chosen.id}"]
    try:
        observation, merged = await ObservationStore(WorkspaceStore(workspace_id)).add(skill or "looks", text, evidence, status="kept")
    except ValueError as error:
        return f"The pick was not recorded: {error}"
    if merged:
        return f"Recorded under {observation.skill}: it matches an earlier pick ({observation.id}), now +{observation.helpful}/−{observation.harmful}."
    return f"Recorded the pick and reason under {observation.skill} ({observation.id})."


async def pick_look(project_slug: str, look_id: str, reason: str, tool_context, skill: str = "stills") -> str:
    """Merge the look the person picked into the project.

    The project is backed up first to `looks/_backup-<time>/`. Then the look's files are copied over
    it (not its look.json or out/), except files the project changed more recently than the look
    did. The other looks stay in `looks/`.

    Args:
      project_slug: The project folder name under /workspace/projects.
      look_id: The letter the person picked.
      reason: The person's own words for why they picked it ("" if they gave none).
      skill: The skill this job runs under (stills, thumbnails, launch-film...); the pick and reason
        are recorded as an observation there, so the next job in it reads what they chose and why.
    """
    from app import sandbox, tasks

    project = _project_dir(project_slug)
    if project is None:
        return "Project name is not allowed; use one project directory name."
    wanted = look_id.strip()
    for t in await tasks.running(tool_context.user_id, tool_context.session.id):
        if t["label"].lower().startswith(f"look {wanted.lower()}:"):
            return f"Look {wanted} is still being made ({t['id'][:8]}). Pick it once it has ended, or cancel_task {t['id'][:8]} first."
    looks = Looks(_Exec(await sandbox.for_workspace(tool_context.user_id)))
    try:
        results = await looks.collect(project)
        chosen = next((r for r in results if r.id == wanted), None) or next(
            (r for r in results if r.id.lower() == wanted.lower()), None)
        if chosen is None:
            there = ", ".join(r.id for r in results) or "none yet"
            return f"No look {wanted!r} in {project_slug}. Its looks: {there}."
        picked = await looks.pick(project, chosen.id)
    except LooksError as error:
        return f"Nothing was merged: {error}"
    backup = posixpath.relpath(picked.backup, project)
    name = f"look {chosen.id}" + (f' ("{_short(chosen.direction, 80)}")' if chosen.direction else "")
    why = f" Their reason: {reason.strip()}." if reason.strip() else ""
    why += " " + await _record(tool_context.user_id, skill, project_slug, chosen, results, reason)
    if not picked.changed:
        return f"Look {chosen.id} matched the project already; nothing changed. Backup in {backup}/.{why}"
    count = len(picked.changed)
    listed = ", ".join(picked.changed[:20]) + (f", and {count - 20} more" if count > 20 else "")
    return (f"Merged {name} into {project_slug}: {count} file{'' if count == 1 else 's'} changed ({listed}). "
            f"The version before is in {backup}/.{why} Build the final from the project now.")
