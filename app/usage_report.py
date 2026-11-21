"""Operator report: per-request usage and job time, for pricing reviews. Read-only (SELECTs only).

Run as one worker execution: `gcloud run jobs execute studio-worker --args=--usage-report`; rows go to
the job's logs as `USAGEROW <json>` lines. Workspace ids are hashed.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from app import meter

Q = {
 "usage": """WITH t AS (SELECT session_id, parent_session_id FROM tasks)
   SELECT COALESCE(t.parent_session_id, u.session_id) AS req, md5(u.workspace_id) AS wsh, u.role, u.model,
          count(*) AS calls, sum(u.in_tok) AS in_tok, sum(u.cached_tok) AS cached, sum(u.out_tok) AS out_tok,
          sum(u.cost_usd) AS cost, min(u.at)::text AS first_at, max(u.at)::text AS last_at
   FROM usage u LEFT JOIN t ON t.session_id = u.session_id GROUP BY 1,2,3,4""",
 "title": "SELECT id, (state::json->>'title') AS title, create_time::text FROM sessions WHERE app_name = 'app'",
 "jobs": """WITH t AS (SELECT session_id, parent_session_id FROM tasks)
   SELECT COALESCE(t.parent_session_id, j.session_id) AS req, count(*) AS jobs,
          sum(extract(epoch FROM (coalesce(j.finished_at, j.updated_at) - j.created_at)))::float AS secs,
          string_agg(DISTINCT j.status, ',') AS statuses
   FROM jobs j LEFT JOIN t ON t.session_id = j.session_id WHERE j.status <> 'scheduled' GROUP BY 1""",
}

async def report() -> None:
    eng = await meter.engine()
    async with eng.connect() as c:  # read-only: SELECTs only
        for name, q in Q.items():
            for row in (await c.execute(text(q))).mappings():
                print("USAGEROW " + json.dumps({"t": name, **{k: (float(v) if hasattr(v, "is_finite") else v) for k, v in row.items()}}, default=str), flush=True)
    print("USAGEROW DONE", flush=True)

