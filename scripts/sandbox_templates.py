"""Create the two sandbox templates for a sandbox image and print their resource names.

  uv run --no-project --with "google-cloud-aiplatform[agent-engines]" python scripts/sandbox_templates.py \
      --project YOUR_PROJECT_ID --location asia-south1 \
      --image asia-south1-docker.pkg.dev/YOUR_PROJECT_ID/studio/sandbox:m1

- studio-hermetic-<tag>: the design sandbox (no internet). -> STUDIO_SANDBOX_TEMPLATE
- studio-capture-<tag>:  website capture for brand onboarding (internet on; holds no credentials,
                         one sandbox per capture, deleted after). -> STUDIO_CAPTURE_TEMPLATE

Templates are pinned by resource name (templates.get drops display_name), so put the printed names
in Terraform. Reuses a template when one with the same name and image already exists.
"""

from __future__ import annotations

import argparse

import vertexai
from vertexai._genai.types import AgentEngineConfig

ENGINE_NAME = "studio-sandbox-host"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--image", required=True)
    a = ap.parse_args()
    client = vertexai.Client(project=a.project, location=a.location, http_options={"api_version": "v1beta1"})
    engine = next((str(e.api_resource.name) for e in client.agent_engines.list()
                   if getattr(e.api_resource, "display_name", None) == ENGINE_NAME), None)
    engine = engine or str(client.agent_engines.create(config=AgentEngineConfig(display_name=ENGINE_NAME)).api_resource.name)
    tag = a.image.rsplit(":", 1)[-1]
    existing = {}
    for t in client.agent_engines.sandboxes.templates.list(name=engine):
        try:  # list() can still show templates that were deleted; get() then 404s
            t = client.agent_engines.sandboxes.templates.get(name=str(t.name))
        except Exception:
            continue
        spec = getattr(getattr(t, "custom_container_environment", None), "custom_container_spec", None)
        internet = bool(getattr(getattr(t, "egress_control_config", None), "internet_access", False))
        if getattr(spec, "image_uri", None) == a.image:
            existing[internet] = str(t.name)
    for env, internet in (("STUDIO_SANDBOX_TEMPLATE", False), ("STUDIO_CAPTURE_TEMPLATE", True)):
        name = existing.get(internet)
        if name is None:
            op = client.agent_engines.sandboxes.templates.create(
                name=engine,
                display_name=f"studio-{'capture' if internet else 'hermetic'}-{tag}",
                config={
                    "custom_container_environment": {
                        "custom_container_spec": {"image_uri": a.image},
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                    },
                    "egress_control_config": {"internet_access": internet},
                },
            )
            name = str(op.response.name)
        print(f"{env}={name}")


if __name__ == "__main__":
    main()
