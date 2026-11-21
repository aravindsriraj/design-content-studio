# Porting the rest of design-agent: plan (user chose A → B → C → D, 2026-11-17)

DA source (read-only): scratchpad `repo/design-agent` (re-clone github.com/langchain-ai/design-agent if gone).

## Phase A: finish stills (in progress)
Status 2026-11-17:
- Done and tested locally: steps 1-5 below. Unit tests pass (29).
  - Chart run: delivered for $0.17, clean.
  - Illustrated Instagram post: generate_image, then HTML type and logo; delivered for $0.43.
  - The image tool now retries on 429; Vertex rate-limited one parallel image call in that run.
- Step 6 done: PDF embedded on the proof wall; GIFs already animate there.
- Step 7 done: tests/eval/datasets/phase_a.json (chart, PDF, illustrated post, GIF reframe) plus a held-out
  diagram in holdout.json. Chart, PDF and illustrated post score delivered 1, gates 1, reply 5/5, design 4/5
  ($0.16, $0.54, $0.52). The first GIF run failed because my fixture was static; it was regenerated with
  real motion and re-run.
- DA guard middleware ported (app/guards.py): tool_error_boundary, image_clamp, today, budget; retries
  raised to 5. Not ported, on purpose: step_budget (ADK allows 500 calls per turn), cache (Anthropic-only).
- Sandbox matches DA's apt list (no gifsicle, which DA's gif-edit skill says is absent; xz-utils added).
- Not done: step 8 (deploy, needs approval).
Findings so far:
- Image model: `gemini-nano-banana-2.1` works on Vertex `global` (also gemini-3.1-flash-image, gemini-3-pro-image).
  Price (ai.google.dev pricing, checked 2026-11-17): $1.50/1M input, $7.50/1M text+thinking out,
  $30/1M image out = $0.0336 per 1K image, $0.0504 per 2K, $0.113 per 4K.
- `slideshow` skill is HyperFrames-based → moved to Phase C. PDF one-pagers/static decks use
  `workspace/tools/html2pdf.mjs` (referenced by stills/design-wiki skills) → Phase A.
- `reference-study` is about video → Phase C.

To do:
1. `generate_image` tool in `app/tools.py`: port DA `tools/media.py` Google path (aspect ratio from size,
   quality=high → 2K, n ≤ 4 parallel calls, `_0/_1` suffixes, PNG alpha receipt), write the result into the sandbox,
   and meter it with role `image`. Add the price to `app/meter.py` (image-out rate ≠ text-out rate).
2. `fetch_asset` tool: port DA `tools/fetch.py` (SSRF guard: http(s) only, public IPs, re-check redirects, 50 MB cap, no SVG).
3. Skills → `app/skills/`: image-generation, data-viz, content-brief, gif-edit. Rewrite host-tool names
   (`upload_files` → `deliver`, drop `start_looks`/hyperframes mentions); update the media-production router.
4. Sandbox tools: copy `html2pdf.mjs` into `app/sandbox_tools`.
5. Sandbox image: add ffmpeg, gifsicle, d2 (pinned and checksummed, see DA `sandbox/setup.sh` install_d2), and echarts 6.1.0
   in `render/package.json`. Rebuild amd64, push `sandbox:m1`, then create a new sandbox template and point
   `STUDIO_SANDBOX_TEMPLATE` (Terraform) at it.
6. UI: proof wall tile for PDF and GIF deliverables (download link).
7. Evals: add chart, generated illustration with copy, PDF one-pager and GIF edit cases; keep one held out.
8. Deploy only after the user approves (`scripts/deploy.sh` + `terraform apply`).

## Brand from a website: BUILT and tested locally on stripe.com ($0.65: capture, licensed Söhne swapped for Inter, draft saved, proof card delivered) (user request, 2026-11-17; DA has this as a dev-time `brand-onboarding` skill)
Ours is an end-user feature: Brand panel → "Use my website" → a background job → a draft pack the
person reviews before it goes live.
1. Capture, deterministic: headless Chrome in a separate capture sandbox (template with
   internet_access=True, no credentials, separate from the design sandbox). It loads the home page and
   2-3 linked pages and records computed styles (body, headings, buttons, links, nav), CSS custom
   properties, @font-face sources, colours weighted by area, logo candidates (header svg/img, favicon,
   apple-touch-icon, og:image), desktop and mobile screenshots, and copy samples. Output: capture.json
   plus the screenshots.
2. Computer use only as a fallback: ADK `ComputerUseToolset` + a Playwright `BaseComputer`, on Gemini
   with ENVIRONMENT_BROWSER, to clear cookie walls or find a press kit or brand page. Step cap, in the
   same capture sandbox.
3. Brand agent (`brand-onboarding` skill, ported from DA `.agents/skills/brand-onboarding`): writes
   tokens.json (roles, WCAG checks), logos, fonts.toml (open fonts fetched; licensed fonts are
   asked for, with the closest open fallback meanwhile), voice.md and graphic-language.md, each with
   its sources. Validate with `app/brand/store.validate`, render one sample card, list the open
   questions.
4. Review: the Brand panel shows the draft (palette, type, logo, sample card, questions), with
   Activate and Edit.

## Phase B: helpers and parallel looks: BUILT (app/tasks.py, app/helpers.py, app/looks.py vendored from DA branches.py; UI helper list and notices; 4 unit tests plus a sandbox integration test). Verified end to end locally: 3 looks in parallel, each finish woke the lead, the final turn delivered look-a/b/c and asked for a pick; "B" → pick_look merged it (backup kept) and the final was delivered. About $1.70 in total. Helpers now meter as role `helper`, and the UI adds helper costs to the request's cost. (design, from DA subagents.py, middleware/background.py, looks_tools.py, harness/branches.py)
DA: effort workers (low / xhigh / max, plus general-purpose) run detached via `start_task`, and the
lead's turn ends with an ETA. A helper's end wakes the lead with a notice. `report_progress`,
`check_tasks`, `cancel_task` and `resume_task` support them; `start_looks`/`pick_look` make N looks in
`looks/<id>/` copies, and a person picks.

ADK mapping on our job infrastructure:
- Workers are `LlmAgent`s with `WORKER_PROMPT` (verbatim) and the lead's tools minus `deliver`, plus
  `report_progress`. Effort maps to Gemini `thinking_level`: low → LOW, high → MEDIUM, xhigh/max → HIGH.
- `task` (synchronous, general-purpose): `AgentTool(general)`. It shares the workspace sandbox
  (same user_id); parallel function calls run concurrently.
- `start_task`: a `tasks` row plus its own Cloud Run Job execution (job kind "helper") running the
  worker in a child ADK session (durable: same retries and resumable invocation as lead jobs). On
  finish it records the report and dispatches a "wake" job on the parent session whose message is the
  notice. `resume_task` is not needed: retries resume automatically.
- `report_progress`: written to the task row, shown in the UI; urgent → wake now.
- `check_tasks` / `cancel_task`: read the table / cancel the execution.
- Email: a lead turn that ends while its helpers still run does not send "Ready"; the wake turn
  that ends with nothing running does.
- `start_looks` / `pick_look`: port branches.py (folders, next_ids, prepare, brief_for, pick with
  backup and newer-wins) on top of `start_task`. The UI shows the looks side by side with Pick buttons.

## Phase C: video (HyperFrames): BUILT, verified locally
- Sandbox: DA's render package.json + lockfile (`npm ci`), the `hyperframes` wrapper, DA's probe at build
  (hyperframes 0.8.169 renders 1080p in the image). Commands run under bash, since DA's skills use bash.
- Skills: all of DA's video skills plus the HyperFrames skills, verbatim except `/skills/` →
  `/workspace/skills/` (whole skill folders are staged, as DA mounts them) and `upload_files` → `deliver`.
  The media-production router is DA's original plus brand-onboarding.
- Gates: video-gate, video-score, film-probe, film-analysis, audio-lint, fetch-asset.
- Tools: start_render_job / watch_progress (durable watcher task with DA's vendored parsers);
  run_pipeline / answer_pipeline (DA's WorkflowRunner vendored unchanged on SqlStore, steps run inline,
  ask parks the run and wakes the lead, answers replay the memo); resume_task.
- Verified: a 6 s 1920x1080 sting via the jobs API (start_render_job → delivered MP4 with sound, $1.40);
  render-watch integration tests (success and failure); pipeline park/answer/replay unit test. A 1-minute
  launch film ran end to end in production (2026-11-18, the user's run).

## Phase D: sound: BUILT, verified locally
generate_voiceover (Gemini 3.8 Flash TTS; delivery in speech_metadata.style, DA [tags] → <tags>; words.json
from transcription), generate_music (Lyria 3 clip / 3.5, trimmed with a fade), transcribe_audio (Gemini, with
word timings), generate_sound_effect (no Google model: says so and points to the synth cue library).
Lyria price is an UNVERIFIED stand-in ($0.06/clip).

## Phase E: learning: BUILT
DA harness/learning.py vendored; save/read/rate_observation + learn_from_job; pick_look records the pick.
Observations are namespaced per workspace (DA is single-tenant).

## Phase F: check-ins: BUILT
schedule_wakeup → a "scheduled" job fired by Cloud Tasks (jobs:run) at its time; a busy lead defers a
minute; `[quiet]` replies send nothing and the UI hides them; check_tasks/cancel_task cover them.

## Phase G: Slack: dropped by the user (2026-11-17).

## Skipped on purpose: langsmith-trace, paper-design, remotion-to-hyperframes.

## Deploy checklist (needs the user's approval; nothing below has run)
1. Sandbox image (amd64) with HyperFrames, ffmpeg, d2, the capture script and probe:
   `docker buildx build --platform linux/amd64 -t asia-south1-docker.pkg.dev/YOUR_PROJECT_ID/studio/sandbox:m2 --push sandbox/`
   (the build runs DA's probe and fails if a render does not work).
2. Templates: `uv run --no-project --with "google-cloud-aiplatform[agent-engines]" python scripts/sandbox_templates.py
   --project YOUR_PROJECT_ID --location asia-south1 --image <the m2 image>` → put the printed
   `STUDIO_SANDBOX_TEMPLATE` and `STUDIO_CAPTURE_TEMPLATE` into Terraform vars `sandbox_template` and `capture_template`.
3. `terraform apply` in deployment/terraform/single-project: the Cloud Tasks queue and IAM, worker env
   (STUDIO_JOBS=cloudrun etc.), the 24 h worker timeout, the capture template env.
4. `scripts/deploy.sh` (service + worker on one image digest).
5. Smoke: a still, a chart, a PDF, brand-from-website on a real site, a 3-look request, and a short sting.
Existing workspace sandboxes run the old image until their 24 h TTL ends (or delete them).

## Final verification (2026-11-17, local, final code)
- Tests: 48 pass (unit and integration, including the sandbox-backed looks, render-watch and memory tests).
- Evals: stills.json (5) plus the held-out thumbnail and diagram cases: all delivered, all gates ran,
  design 4/5 each, reply 5/5 (thumbnail 4/5). Mean cost $0.53 per request (up from about $0.40 before the
  full DA instructions and the ~31-tool surface). Phase A set: chart, PDF, illustrated post and GIF all pass.
- Live runs through the jobs API: 3 parallel looks + pick, a 6 s sting via start_render_job, and
  brand-from-website on stripe.com.
- UI: background work shows as plain status lines plus one card with live progress (reports stay with
  the agent); PDF, video and GIF tiles; brand draft review.

## Deployed (2026-11-18)
- Sandbox image `sandbox:m2` (Cloud Build, amd64; DA probe passed). Templates: design `…/YOUR_TEMPLATE_ID`
  (no internet), capture `…/YOUR_TEMPLATE_ID` (internet; verified: reaches public sites, not the metadata server).
- Terraform applied: Cloud Tasks queue + IAM, worker dispatch env, 24 h worker timeout, new templates,
  lead model **gemini-3.8-flash** (the user's choice).
- `scripts/deploy.sh` (service + worker on one digest). `.gcloudignore` now keeps Terraform state out of uploads.
- Fixed in production after smoke tests:
  - Workspace sandboxes created from an older template are replaced on reattach (`app/sandbox.py`); before
    this, every existing workspace ran the old image (no ffmpeg/HyperFrames/d2) for up to 24 h.
  - `start_render_job` now says its output is not usable until the lead is woken (the local run had mixed
    audio onto an unfinished render).
- Production smoke (worker `--smoke`, Flash): still, chart, PDF, brand-from-website (linear.app), 3 looks with
  helpers and wake-ups, check-in fired through Cloud Tasks, voiceover MP3: all delivered.
- With no person to reply, Flash keeps DA's "storyboard first" rule for motion and stops at the storyboard.

## After the port (2026-11-18, deployed)
- Transcription → `gemini-3.5-transcribe` (Vertex `generate_content` with `audio_transcription_config`, word
  timestamps; custom vocabulary is refused alongside timestamps). Voiceover timings keep the script's spelling.
- UI: new sign-in page (proof board of the full range), full-range examples and empty state, "Attach file"
  (images, video, audio, PDF; 20 MB cap), a nudge to set up the brand from a website.
- Delete a request: `DELETE /api/requests/{id}` (app/cleanup.py) stops its work and removes its conversation,
  every file version, helper sessions and files, jobs, tasks, payloads and pipeline memos; the usage ledger and
  user-scoped files stay. ADK's own session DELETE route is refused (it would orphan files). UI: hover trash in the
  sidebar and Delete in the header, both with an inline confirmation.
- Email/password sign-in alongside Google: sign up, sign in, reset password; new accounts must verify their email
  (verify screen in the UI; the server rejects unverified password accounts).
