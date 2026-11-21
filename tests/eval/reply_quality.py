"""Local LLM judge: does the final reply follow the spec's talking rules and the case's EXPECT?"""

import json
import threading

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()

RUBRIC = """You grade the final chat reply of a design agent that makes on-brand still graphics. Score 1-5.
5 = meets every rule; 3 = one clear miss; 1 = several misses or a false claim.
Rules:
- Speaks like a teammate. No tool names (bash, look, deliver, write_file), file paths, gate names
  (brand-lint, artifact-contract), scores or token counts.
- Short: names the concept or decisions in a line or two; no long explanations.
- Never claims a file is attached/delivered unless the deliver tool returned "delivered".
- Asks at most two questions, and only when the answer would change the work.
- Satisfies the case expectation (EXPECT) as far as the reply can show it.
Return JSON {"score": 1-5, "explanation": "<one or two sentences>"}."""


class _Verdict(BaseModel):
    score: int
    explanation: str


def _client() -> genai.Client:
    c = getattr(_local, "client", None)
    if c is None:
        c = _local.client = genai.Client()
    return c


def _text(content) -> str:
    return " ".join(p.get("text", "") for p in ((content or {}).get("parts") or []) if isinstance(p, dict))


def _tool_summary(instance) -> str:
    lines = []
    for t in (instance.get("agent_data") or {}).get("turns", []):
        for e in t.get("events", []):
            for p in ((e.get("content") or {}).get("parts") or []):
                if fc := p.get("function_call"):
                    lines.append(f"call {fc['name']} {json.dumps(fc.get('args', {}))[:160]}")
                if (fr := p.get("function_response")) and fr.get("name") == "deliver":
                    lines.append(f"deliver -> {json.dumps(fr.get('response'))[:200]}")
    return "\n".join(lines[-60:])


def evaluate(instance):
    prompt = (
        f"{RUBRIC}\n\nUser request: {_text(instance.get('prompt'))}\n"
        f"Case reference: {_text((instance.get('reference') or {}).get('response'))}\n"
        f"Tool calls (in order):\n{_tool_summary(instance)}\n\n"
        f"Final reply:\n{_text(instance.get('response') or {})}\n"
    )
    r = _client().models.generate_content(
        model="gemini-3.8-flash", contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_schema=_Verdict),
    )
    v = r.parsed
    if v is None:
        return {"score": 0, "explanation": r.text or ""}
    return {"score": max(1, min(5, v.score)), "explanation": v.explanation}
