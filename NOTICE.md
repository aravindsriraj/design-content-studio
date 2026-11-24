# Notices

Design Content Studio is licensed under Apache-2.0 (see [LICENSE](LICENSE)). It is inspired by LangChain's
open-source design agent and includes the third-party components below.

## Third-party components

| Component | Where | License |
| --- | --- | --- |
| design-agent (LangChain, Inc.): skills, workspace gates and tools, the brand schema, the agent instructions, guard middleware, the looks core (`harness/branches.py`), the render-log parsers (`harness/watch.py`), the pipeline runner (`harness/workflow.py`, `harness/events.py`, `harness/protocols.py`), the learning loop (`harness/learning.py`), the launch-film pipeline, the brand-onboarding skill, the worker prompt and the sandbox render lockfile and probe; included with changes noted in each file | `app/skills/`, `app/sandbox_tools/`, `app/brand/schema.py`, `app/prompt.md`, `app/guards.py`, `app/looks.py`, `app/watch.py`, `app/harness/`, `app/pipelines/launch_film.py`, `app/helpers.py`, `app/learning.py`, `app/fetch.py`, `app/tools.py`, `sandbox/render/`, `sandbox/probe/` | Apache-2.0, [licenses/design-agent-LICENSE.txt](licenses/design-agent-LICENSE.txt). Copyright 2026 LangChain, Inc. |
| HyperFrames agent skills (HeyGen), vendored by design-agent unmodified at v0.8.169 | `app/skills/hyperframes*`, `app/skills/media-use`, and the video recipe skills design-agent lists in its `skills-lock.json` | Apache-2.0, [licenses/hyperframes-LICENSE.txt](licenses/hyperframes-LICENSE.txt) |
| HyperFrames CLI | installed in the sandbox image, not redistributed here | Apache-2.0 |
| GSAP | installed in the sandbox image, never vendored | GSAP Standard "no charge" license |
| The Example Studio sample brand (from design-agent) | `app/brand/sample/` | CC0-1.0 |
| Sandbox runtime shim from google/adk-samples `long-horizon-harness` | `sandbox/runtime/` | Apache-2.0, [licenses/adk-samples-LICENSE.txt](licenses/adk-samples-LICENSE.txt) |
| Geist, Fragment Mono, Inter, IBM Plex Mono, Fira Code, Fraunces | `sandbox/fonts/` | SIL Open Font License 1.1, [sandbox/fonts/licenses/](sandbox/fonts/licenses/) |
| sharp, puppeteer-core, Apache ECharts | installed in the sandbox image | Apache-2.0 |
| D2 | binary installed in the sandbox image | MPL-2.0 |
| Chrome Headless Shell | binary installed in the sandbox image | Chromium licenses |
| FFmpeg | Debian package in the sandbox image | LGPL/GPL per the Debian build |

Design Content Studio is an independent project, not affiliated with, endorsed by or sponsored by
LangChain, Inc. All trademarks belong to their owners.
