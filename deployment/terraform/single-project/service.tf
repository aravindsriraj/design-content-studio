# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


resource "google_cloud_run_v2_service" "app" {
  provider            = google-beta
  name                = var.project_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels = {
    "created-by" = "agents-cli"
  }

  template {
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      env {
        name  = "APP_URL"
        value = "https://${var.domain}"
      }
      env {
        name  = "STUDIO_EMAIL_FROM"
        value = "Design Content Studio <hello@${var.domain}>"
      }
      dynamic "env" {
        for_each = local.billing_env
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }

      env {
        name  = "GOOGLE_GENAI_USE_ENTERPRISE"
        value = "True"
      }

      env {
        name  = "STUDIO_AUTH"
        value = "firebase"
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIREBASE_API_KEY"
        value = data.google_firebase_web_app_config.studio.api_key
      }
      env {
        name  = "FIREBASE_AUTH_DOMAIN"
        value = data.google_firebase_web_app_config.studio.auth_domain
      }
      env {
        name  = "FIREBASE_APP_ID"
        value = google_firebase_web_app.studio.app_id
      }
      env {
        name  = "STUDIO_MODEL"
        value = var.lead_model
      }
      env {
        name  = "STUDIO_SANDBOX"
        value = "vertex"
      }
      env {
        name  = "STUDIO_SANDBOX_LOCATION"
        value = var.region
      }
      env {
        name  = "STUDIO_SANDBOX_TEMPLATE"
        value = var.sandbox_template
      }
      env {
        name  = "STUDIO_CAPTURE_TEMPLATE"
        value = var.capture_template
      }
      env {
        name  = "STUDIO_SANDBOX_CALLER_SA"
        value = google_service_account.app_sa.email
      }
      env {
        name  = "STUDIO_BUCKET"
        value = google_storage_bucket.logs_data_bucket.name
      }
      env {
        name  = "STUDIO_JOBS"
        value = "cloudrun"
      }
      env {
        name  = "STUDIO_REGION"
        value = var.region
      }
      env {
        name  = "STUDIO_WORKER_JOB"
        value = google_cloud_run_v2_job.worker.name
      }
      env {
        name  = "STUDIO_SERVICE_ACCOUNT"
        value = google_service_account.app_sa.email
      }
      env {
        name  = "STUDIO_WAKEUP_QUEUE"
        value = google_cloud_tasks_queue.wakeups.name
      }
      env {
        name  = "STUDIO_DATA_DIR"
        value = "/tmp/studio"
      }
      env {
        name = "SESSION_SERVICE_URI"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "STUDIO_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      env {
        name  = "OTEL_SERVICE_NAME"
        value = "studio"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }

      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "false"
      }

      env {
        name  = "OTEL_SEMCONV_STABILITY_OPT_IN"
        value = "gen_ai_latest_experimental"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT"
        value = "jsonl"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK"
        value = "upload"
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH"
        value = "gs://${google_storage_bucket.logs_data_bucket.name}/completions"
      }
    }

    service_account = google_service_account.app_sa.email
    timeout         = "3600s" # long agent turns stream over one request
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.studio.connection_name]
      }
    }
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = 0 # scale to zero while there are no customers; raise to 1 to avoid cold starts
      max_instance_count = 10
    }

    session_affinity = true
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # This lifecycle block prevents Terraform from overwriting the container image when it's
  # updated by Cloud Run deployments outside of Terraform (e.g., via CI/CD pipelines)
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }

  # Make dependencies conditional to avoid errors.
  depends_on = [
    resource.google_project_service.services,
    google_project_iam_member.studio_app_roles,
    google_secret_manager_secret_version.db_url,
  ]
}
