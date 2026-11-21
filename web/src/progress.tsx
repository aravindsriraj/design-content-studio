// What the agent is doing, for the person watching: each tool call becomes a plain step in one of
// a few stages, timed from event timestamps. A small studio hand at a desk acts out the live stage.
import { useEffect, useState } from "react";
import type { Part } from "./api";

export type Phase = "plan" | "design" | "sound" | "render" | "check" | "team" | "deliver";
export type Step = { phase: Phase; label: string; detail?: string; at: number; count: number };

const STAGE: Record<Phase, { name: string; live: string }> = {
  plan: { name: "Planning", live: "Planning it out" },
  design: { name: "Designing", live: "Designing" },
  sound: { name: "Sound", live: "Working on the sound" },
  render: { name: "Rendering", live: "Rendering" },
  check: { name: "Checking", live: "Checking the work" },
  team: { name: "Helpers", live: "Working with helpers" },
  deliver: { name: "Delivering", live: "Delivering" },
};

const snip = (s: unknown, max = 64) => {
  const t = String(s ?? "").replace(/\s+/g, " ").trim();
  return t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t;
};
const base = (p: string) => p.split("/").pop() ?? p;
const words = (slug: string) => slug.replace(/[-_]+/g, " ").replace(/\.\w+$/, "");

type Said = Omit<Step, "at" | "count">;
const s = (phase: Phase, label: string, detail?: string): Said => ({ phase, label, ...(detail ? { detail } : {}) });

function fromBash(cmd: string): Said {
  // Most specific first: one command often chains a check after a render.
  if (/hyperframes\s+render/.test(cmd)) return s("render", /--quality\s+draft/.test(cmd) ? "Rendering a draft" : "Rendering the film");
  if (/html2pdf/.test(cmd)) return s("render", "Making the PDF");
  if (/html2png/.test(cmd)) return s("render", "Rendering the design");
  if (/ffmpeg/.test(cmd)) return /amix|loudnorm|\.mp3|\.wav|-filter_complex/.test(cmd) ? s("sound", "Mixing the sound") : s("render", "Encoding the video");
  if (/hyperframes\s+snapshot|grab\.mjs|review-thumb/.test(cmd)) return s("check", "Pulling frames to review");
  if (/video-gate|video-score|hyperframes\s+(check|lint)/.test(cmd)) return s("check", "Checking the motion");
  if (/audio-lint/.test(cmd)) return s("check", "Checking audio levels");
  if (/text-fidelity/.test(cmd)) return s("check", "Proofreading the text");
  if (/brand-lint/.test(cmd)) return s("check", "Checking it against the brand");
  if (/artifact-contract/.test(cmd)) return s("check", "Checking sizes and formats");
  if (/viz\.mjs/.test(cmd)) return s("design", "Drawing the chart");
  if (/analyze-audio|breakdown\.mjs/.test(cmd)) return s("sound", "Finding the beats");
  if (/sharp/.test(cmd)) return s("design", "Preparing images");
  if (/brand-draft/.test(cmd)) return s("plan", "Studying the website");
  if (/\/workspace\/brand\b|brand\/(tokens|graphic|logos|README)/.test(cmd)) return s("plan", "Studying the brand");
  if (/tools\/README|\/skills\//.test(cmd)) return s("plan", "Reading up on the tools");
  if (/^\s*(set -e\s*)?(cd \S+\s*(&&)?\s*)?(mkdir|ls|cp|rm)\b/.test(cmd)) return s("plan", "Setting up the project");
  if (/fc-list|fc-match|\.ttf|\.woff/.test(cmd)) return s("plan", "Checking the fonts");
  if (/\/attachments\//.test(cmd)) return s("plan", "Studying the attachment");
  if (/sound-assets|\.mp3|\.wav/.test(cmd)) return s("sound", "Preparing the sound");
  if (/^\s*(cat|head|tail|jq)\b/.test(cmd)) return s("design", "Reviewing the files");
  return s("design", "Working on the files");
}

function fromWrite(path: string): Said {
  const name = base(path);
  if (/brief/i.test(name)) return s("plan", "Writing the brief");
  if (/storyboard|animatic/i.test(path)) return s("plan", "Writing the storyboard");
  if (/script|copy|vo\b/i.test(name)) return s("plan", "Writing the words");
  if (/timeline.*\.js$/.test(name)) return s("design", "Timing the animation");
  if (/compositions\//.test(path) && name.endsWith(".html")) return s("design", "Building a scene", words(name));
  if (name.endsWith(".html")) return s("design", "Laying out the design", words(name));
  if (/contract/.test(name)) return s("plan", "Setting the specs");
  if (name.endsWith(".json")) return s("design", "Shaping the data", words(name));
  return s("plan", "Writing notes", name);
}

/** One tool call as a stage and a plain step. */
export function describe(call: NonNullable<Part["functionCall"]>): Said {
  const a = (call.args ?? {}) as Record<string, unknown>;
  switch (call.name) {
    case "load_skill":
    case "load_skill_resource":
      return s("plan", "Reading up on the craft", a.skill_name ? words(String(a.skill_name)) : undefined);
    case "list_skills":
      return s("plan", "Reading up on the craft");
    case "read_observations":
    case "list_recent_projects":
      return s("plan", "Remembering past work");
    case "save_observation":
    case "rate_observation":
      return s("deliver", "Making notes for next time");
    case "capture_site":
      return s("plan", "Visiting the website", snip(a.url));
    case "fetch_asset":
      return s("plan", "Fetching a file", snip(a.url));
    case "write_file":
      return fromWrite(String(a.path ?? ""));
    case "bash":
      return fromBash(String(a.command ?? ""));
    case "generate_image":
      return s("design", "Generating an image", snip(a.prompt));
    case "pick_look":
      return s("design", "Picking a direction");
    case "generate_voiceover":
      return s("sound", "Recording the voiceover", `“${snip(a.text, 56)}”`);
    case "generate_music":
      return s("sound", "Composing the music", snip(a.prompt));
    case "generate_sound_effect":
      return s("sound", "Making a sound effect");
    case "transcribe_audio":
      return s("sound", "Timing the words");
    case "start_render_job":
      return s("render", "Rendering in the background", a.label ? words(String(a.label)) : undefined);
    case "watch_progress":
      return s("render", "Watching a render", a.label ? words(String(a.label)) : undefined);
    case "look":
      return s("check", "Looking it over", snip(a.question));
    case "start_looks":
      return s("team", "Briefing helpers on directions", Array.isArray(a.directions) ? `${a.directions.length} directions` : undefined);
    case "start_task":
    case "task":
    case "general_purpose":
    case "run_pipeline":
      return s("team", "Briefing a helper", snip(a.label ?? a.request ?? a.description ?? a.pipeline));
    case "check_tasks":
    case "get_render_status":
    case "resume_task":
    case "cancel_task":
    case "answer_pipeline":
      return s("team", "Checking on helpers");
    case "schedule_wakeup":
      return s("team", "Setting a check-in");
    case "save_brand_draft":
      return s("deliver", "Saving the brand draft");
    case "deliver":
      return s("deliver", "Sending the files", Array.isArray(a.paths) ? a.paths.map((p) => base(String(p))).join(", ") : undefined);
    default:
      return s("design", "Working");
  }
}

/** Adds a turn's calls to its steps; a repeat of the last step counts up instead of adding a line. */
export function addSteps(steps: Step[], parts: Part[], at: number) {
  for (const p of parts) {
    if (!p.functionCall) continue;
    const said = describe(p.functionCall);
    const last = steps[steps.length - 1];
    if (last && last.label === said.label && last.phase === said.phase) {
      last.count += 1;
      last.detail = said.detail ?? last.detail;
    } else steps.push({ ...said, at, count: 1 });
  }
}

export function duration(seconds: number): string {
  const t = Math.max(0, Math.round(seconds));
  if (t < 60) return `${t} s`;
  const m = Math.round(t / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** Details of folded steps, each once: "index, card, outro". */
function join(a?: string, b?: string): string | undefined {
  const all = [...new Set([...(a ?? "").split(", "), b ?? ""].filter(Boolean))];
  return all.length ? snip(all.join(", "), 72) : undefined;
}

type Stage = { phase: Phase; seconds: number; steps: (Step & { seconds: number })[] };

/** Steps grouped by stage in first-seen order, each timed until the next step (or `end`). */
function stagesOf(steps: Step[], end: number): Stage[] {
  const out = new Map<Phase, Stage>();
  steps.forEach((st, i) => {
    const seconds = Math.max(0, (steps[i + 1]?.at ?? end) - st.at);
    const g = out.get(st.phase) ?? { phase: st.phase, seconds: 0, steps: [] };
    g.seconds += seconds;
    // The same step again later in a stage folds into its first line: count and time add up.
    const same = g.steps.find((x) => x.label === st.label);
    if (same) Object.assign(same, { count: same.count + st.count, seconds: same.seconds + seconds, detail: join(same.detail, st.detail) });
    else g.steps.push({ ...st, seconds });
    out.set(st.phase, g);
  });
  return [...out.values()];
}

function useNow(live: boolean) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [live]);
  return now;
}

export function WorkLog({ steps, working, end }: { steps: Step[]; working: boolean; end: number }) {
  const now = useNow(working);
  const stop = working ? Math.max(now, end) : end;
  const stages = stagesOf(steps, stop);
  const current = steps[steps.length - 1];
  const total = steps.length ? stop - steps[0].at : 0;

  if (working) {
    const phase = current?.phase ?? "plan";
    const since = current ? stop - current.at : 0;
    return (
      <div className="cooking" role="status" aria-live="polite">
        <Hand phase={phase} />
        <div className="cooking-text">
          <p className="cooking-head">{current ? STAGE[phase].live : "Getting started"}</p>
          {current && (
            <p className="cooking-step">
              {current.label}
              {current.count > 1 && <span className="times"> ×{current.count}</span>}
              {current.detail && <span className="cooking-detail"> · {current.detail}</span>}
              {since >= 20 && <span className="cooking-time"> · {duration(since)}</span>}
            </p>
          )}
          {stages.length > 0 && <Rail stages={stages} live={phase} />}
        </div>
      </div>
    );
  }
  if (!steps.length) return null;
  return (
    <details className="worklog">
      <summary>
        Show the work · {stages.length} {stages.length === 1 ? "stage" : "stages"}
        {total >= 1 && ` · ${duration(total)}`}
      </summary>
      <div className="worklog-body">
        <Rail stages={stages} />
        {stages.map((g) => (
          <section key={g.phase} className="stage">
            <h4>
              {STAGE[g.phase].name}
              <span>{duration(g.seconds)}</span>
            </h4>
            <ol>
              {g.steps.map((st, i) => (
                <li key={i}>
                  <span className="step-label">
                    {st.label}
                    {st.count > 1 && <span className="times"> ×{st.count}</span>}
                    {st.detail && <span className="step-detail"> · {st.detail}</span>}
                  </span>
                  {st.seconds >= 5 && <span className="step-time">{duration(st.seconds)}</span>}
                </li>
              ))}
            </ol>
          </section>
        ))}
      </div>
    </details>
  );
}

function Rail({ stages, live }: { stages: Stage[]; live?: Phase }) {
  return (
    <ol className="stages" aria-label="Stages">
      {stages.map((g) => (
        <li key={g.phase} className={g.phase === live ? "now" : ""}>
          {STAGE[g.phase].name}
          <span>{duration(g.seconds)}</span>
        </li>
      ))}
    </ol>
  );
}

/** A small studio hand at a desk; the prop in front of it says what is cooking. */
export function Hand({ phase }: { phase: Phase }) {
  return (
    <svg className={`hand ${phase}`} viewBox="0 0 96 72" width="96" height="72" aria-hidden="true">
      <line x1="4" y1="64" x2="92" y2="64" className="desk-line" />
      <g className="figure">
        <path d="M14 64 C14 50 20 43 30 43 C40 43 46 50 46 64 Z" className="body" />
        <g className="head">
          <circle cx="30" cy="27" r="10" className="face" />
          <path d="M20.5 24 A10 10 0 0 1 39 22.5 L35 21 Q28 18 20.5 24 Z" className="hair" />
          <circle cx="34" cy="27" r="1.4" className="eye" />
          <path d="M32.5 31.5 q2 1.4 4 -0.2" className="smile" />
        </g>
        <path d="M38 51 L51 53" className="arm" />
      </g>
      {phase === "plan" && (
        <g className="prop book">
          <path d="M50 58 L62 54 L74 58 L74 48 L62 44 L50 48 Z" />
          <line x1="62" y1="44" x2="62" y2="54" />
        </g>
      )}
      {phase === "design" && (
        <g className="prop sketch">
          <rect x="54" y="40" width="30" height="22" rx="2" />
          <path className="stroke" d="M58 56 C 64 44, 70 60, 80 46" />
          <line className="pencil" x1="50" y1="50" x2="58" y2="40" />
        </g>
      )}
      {phase === "sound" && (
        <g className="prop mic">
          <rect x="52" y="40" width="8" height="12" rx="4" />
          <line x1="56" y1="52" x2="56" y2="62" />
          <path className="wave w1" d="M66 42 q4 4 0 8" />
          <path className="wave w2" d="M72 38 q7 8 0 16" />
          <path className="wave w3" d="M78 34 q10 12 0 24" />
        </g>
      )}
      {phase === "render" && (
        <g className="prop reel">
          <g className="spin">
            <circle cx="68" cy="48" r="12" />
            <circle cx="68" cy="48" r="2.5" />
            <circle cx="68" cy="41" r="2.5" />
            <circle cx="68" cy="55" r="2.5" />
            <circle cx="61" cy="48" r="2.5" />
            <circle cx="75" cy="48" r="2.5" />
          </g>
        </g>
      )}
      {phase === "check" && (
        <g className="prop loupe">
          <rect x="54" y="42" width="30" height="20" rx="2" className="sheet" />
          <g className="sweep">
            <circle cx="62" cy="48" r="6" />
            <line x1="66" y1="52" x2="71" y2="57" />
          </g>
        </g>
      )}
      {phase === "team" && (
        <g className="prop team">
          <circle cx="62" cy="50" r="4" className="buddy b1" />
          <circle cx="72" cy="50" r="4" className="buddy b2" />
          <circle cx="82" cy="50" r="4" className="buddy b3" />
          <path d="M57 62 a5 5 0 0 1 10 0 M67 62 a5 5 0 0 1 10 0 M77 62 a5 5 0 0 1 10 0" />
        </g>
      )}
      {phase === "deliver" && (
        <g className="prop plane">
          <path d="M54 52 L84 40 L72 60 L68 52 Z M68 52 L84 40" />
        </g>
      )}
    </svg>
  );
}
