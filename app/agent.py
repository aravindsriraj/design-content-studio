"""The design agent: one Gemini lead with a sandbox, skills, look and deliver."""

import os
from pathlib import Path

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from app import billing
from app.brand.onboard import capture_site, save_brand_draft
from app.fetch import fetch_asset
from app.guards import clamp_images, tool_error_boundary
from app.helpers import LEAD_TOOLS
from app.memory import load_memory, save_memory
from app.meter import meter_lead
from app.staging import SKILLS, prepare_workspace
from app.tasks import fold_notices
from app.tools import (
    bash,
    deliver,
    generate_image,
    look,
    write_file,
)

MODEL = os.environ.get("STUDIO_MODEL", "gemini-3.8-flash")

root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml.
    name="studio",
    model=Gemini(model=MODEL, retry_options=types.HttpRetryOptions(attempts=5)),
    # Stable prefix (cacheable); per-turn workspace problems ride the dynamic instruction.
    static_instruction=(Path(__file__).parent / "prompt.md").read_text(),
    instruction="{today_note?}{budget_note?}{plan_note?}{workspace_status?}{brand_brief?}{attachments_note?}{memory_note?}{tasks_note?}",
    tools=[
        bash,
        write_file,
        look,
        generate_image,
        fetch_asset,
        capture_site,
        save_brand_draft,
        *LEAD_TOOLS,
        deliver,
        SkillToolset(skills=[load_skill_from_dir(d) for d in sorted(SKILLS.iterdir()) if d.is_dir()]),
    ],
    before_agent_callback=[prepare_workspace, fold_notices, load_memory, billing.plan_note],
    after_agent_callback=save_memory,
    before_model_callback=[billing.credit_gate, clamp_images],
    before_tool_callback=billing.plan_gate,
    after_model_callback=meter_lead,
    on_tool_error_callback=tool_error_boundary,
)

app = App(
    root_agent=root_agent,
    name="app",
    # No ContextCacheConfig: gemini-4-hydrogen rejected explicit caching (400); implicit caching
    # still discounts the stable prefix (static_instruction first, per-turn facts after).
    events_compaction_config=EventsCompactionConfig(compaction_interval=8, overlap_size=2),
    # Background jobs resume an interrupted run by invocation_id instead of starting over.
    resumability_config=ResumabilityConfig(is_resumable=True),
)
