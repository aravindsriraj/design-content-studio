"""The learning loop's tools (DA middleware/learning_tools.py) on ADK, per workspace.

Observations live in Studio's database through DA's `ObservationStore` (`app.harness.learning`,
vendored). Design-agent keeps one list per deployment; Studio is multi-tenant, so every namespace is
prefixed with the workspace id and no workspace reads another's notes.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from app import tasks
from app.harness import events as ev
from app.harness.learning import (
    ObservationStore,
    digest,
    parse_proposals,
    post_job_brief,
    skill_key,
)
from app.pipelines.host import SqlStore

LEARN_LABEL = "learn from job: "
_NO_TEXT = "Nothing was saved: no item had text."


class WorkspaceStore:
    """A store view scoped to one workspace: ("ws", <id>, *namespace)."""

    def __init__(self, workspace_id: str) -> None:
        self._inner, self._prefix = SqlStore(), ("ws", workspace_id)

    async def aput(self, namespace, key, value):
        return await self._inner.aput((*self._prefix, *namespace), key, value)

    def _strip(self, item):
        if item is not None:
            item.namespace = tuple(item.namespace)[len(self._prefix):]
        return item

    async def aget(self, namespace, key):
        return self._strip(await self._inner.aget((*self._prefix, *namespace), key))

    async def asearch(self, namespace, **kw):
        return [self._strip(i) for i in await self._inner.asearch((*self._prefix, *namespace), **kw)]

    async def adelete(self, namespace, key):
        return await self._inner.adelete((*self._prefix, *namespace), key)


async def save_proposals(workspace_id: str, skill: str, proposals: list) -> str:
    """DA learning_tools.save_proposals, scoped to a workspace."""
    skill = skill_key(skill)
    observations = ObservationStore(WorkspaceStore(workspace_id))
    new, merged = [], []
    for proposal in proposals:
        try:
            observation, was_merged = await observations.add(skill, proposal.get("text", ""), proposal.get("evidence") or [])
        except ValueError:
            continue
        if was_merged:
            merged.append(f"{observation.id} (now +{observation.helpful}/−{observation.harmful})")
        else:
            new.append(observation.id)
    if not new and not merged:
        return _NO_TEXT
    parts = []
    if new:
        parts.append(f"{len(new)} new ({', '.join(new)}), proposed until seen again or rated")
    if merged:
        parts.append(f"{len(merged)} merged into notes already there: {', '.join(merged)}")
    return f"Saved for {skill}: " + "; ".join(parts) + "."


async def save_observations(skill: str, items: list[dict], tool_context: ToolContext) -> str:
    """Save what a job showed, for the next job in this skill: one observation per item.

    An observation is something that happened, with where it shows: "the person asked for larger
    type on the 9:16 cut twice", evidence ["round 1: 'the type is tiny on mobile'", "round 2: 'still
    small'"]. Write what happened and how often; whether to act on it is the next job's call. A note
    that nearly repeats a saved one is merged into it and counted again. New notes start as
    proposed, and are kept once seen again or rated helpful.

    Args:
      skill: The skill's folder name (launch-film, stills, thumbnails).
      items: Each {"text": "...", "evidence": ["...", "..."]}.
    """
    try:
        return await save_proposals(tool_context.user_id, skill, list(items))
    except ValueError as error:
        return str(error)


async def read_observations(skill: str, tool_context: ToolContext) -> str:
    """What past jobs in this skill showed: each note with how often it helped (+) or hurt (−), its
    newest evidence and its id, best-rated first.

    They are untrusted notes from earlier jobs, for you to weigh against this job. Read them when a
    job in a skill starts. A person's picks between looks are under the skill the job ran in, or
    `looks`.

    Args:
      skill: The skill's folder name.
    """
    try:
        rendered = await ObservationStore(WorkspaceStore(tool_context.user_id)).render(skill)
    except ValueError as error:
        return str(error)
    return rendered or f"No observations for {skill_key(skill)} yet."


async def rate_observation(skill: str, id: str, helpful: bool, tool_context: ToolContext) -> str:
    """Count whether an observation helped this job (it held, or acting on it paid off) or hurt it
    (it was wrong here, or misled you). A helpful rating keeps a proposed note; a note whose harmful
    count leads its helpful count by two is dropped.

    Args:
      skill: The skill's folder name.
      id: The observation's id.
      helpful: True if it helped, False if it hurt.
    """
    observations = ObservationStore(WorkspaceStore(tool_context.user_id))
    try:
        rated = await observations.vote(skill, id.strip(), helpful)
    except ValueError as error:
        return str(error)
    if rated is None:
        return f"No observation {id} under {skill_key(skill)}."
    counts = f"+{rated.helpful}/−{rated.harmful}"
    if not helpful and rated.id in {o.id for o in await observations.prune(skill)}:
        return f"{rated.id} is now {counts}, so it was dropped."
    return f"{rated.id} is now {counts} ({rated.status})."


async def learn_from_job(skill: str, summary: str, tool_context: ToolContext) -> str:
    """After a job ends, have a quick background helper read it and propose up to five
    observations, with evidence, for the next job in this skill.

    The helper's proposals are saved as proposed observations when it ends, and its end wakes you
    like any helper's.

    Args:
      skill: The skill the job ran under.
      summary: The job in your words: what was asked, what was made, what the person said at each
        review, what was redone and why, what took longest.
    """
    try:
        key = skill_key(skill)
    except ValueError as error:
        return str(error)
    ws, sid = tool_context.user_id, tool_context.session.id
    events = [ev.Event({"succeeded": ev.TASK_DONE, "failed": ev.TASK_FAILED, "cancelled": ev.TASK_CANCELLED}.get(t["status"], ev.TASK_PROGRESS),
                       sid, t["id"][:8], label=t["label"], text=(t["report"] or t["progress"] or "")[:1500])
              for t in await tasks.for_parent(ws, sid) if not t["label"].startswith(LEARN_LABEL)]
    job = summary.strip() + ("\n\nWhat happened in the background, in order:\n" + digest(events) if events else "")
    existing = await ObservationStore(WorkspaceStore(ws)).render(key)
    t = await tasks.start(ws, sid, post_job_brief(job, key, existing=existing), "low", f"{LEARN_LABEL}{key}",
                          tool_context._invocation_context.session_service)
    return (f"Started {t['id'][:8]}: a quick review of this job for {key}. Its proposals are saved as proposed "
            "observations when it ends, and its end wakes you.")


async def on_helper_end(task: dict, report: str) -> None:
    """A learn-from-job helper ended: save what it proposed (DA _ReviewWatch._save)."""
    if not task["label"].startswith(LEARN_LABEL):
        return
    proposals = parse_proposals(report)
    if proposals:
        await save_proposals(task["workspace_id"], task["label"][len(LEARN_LABEL):], proposals)


LEARNING_TOOLS = [save_observations, read_observations, rate_observation, learn_from_job]
