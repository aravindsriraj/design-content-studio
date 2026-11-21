# Studio-specific infrastructure: Cloud SQL (sessions + usage ledger), Firebase sign-in,
# and the extra IAM the app needs (sandboxes, secrets, Cloud SQL).

variable "capture_template" {
  type        = string
  description = "Sandbox template WITH internet access for brand-onboarding website captures (scripts/sandbox_templates.py). Empty disables capture_site."
  default     = ""
}

variable "sandbox_template" {
  type        = string
  description = "Agent Runtime sandbox template (BYOC image) every workspace sandbox is created from."
}

variable "lead_model" {
  type    = string
  default = "gemini-3.8-flash"
}

locals {
  run_url = "${var.project_name}-${data.google_project.project.number}.${var.region}.run.app"
}

resource "google_project_service" "studio" {
  for_each = toset([
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "iamcredentials.googleapis.com",
    "firebase.googleapis.com",
    "identitytoolkit.googleapis.com",
    "storage.googleapis.com",
    "cloudtasks.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
  depends_on         = [google_project_service.bootstrap]
}

# ---------------------------------------------------------------- Cloud SQL

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "studio" {
  name             = "${var.project_name}-db"
  project          = var.project_id
  region           = var.region
  database_version = "POSTGRES_16"
  settings {
    tier              = "db-f1-micro" # ponytail: shared-core; move to db-custom-1-3840+ when traffic grows
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    ip_configuration {
      ipv4_enabled = true # reached only through the Cloud SQL connector (IAM-authorised), no authorised networks
    }
  }
  deletion_protection = true
  depends_on          = [google_project_service.studio]
}

resource "google_sql_database" "studio" {
  name     = "studio"
  instance = google_sql_database_instance.studio.name
  project  = var.project_id
}

resource "google_sql_user" "studio" {
  name     = "studio"
  instance = google_sql_database_instance.studio.name
  project  = var.project_id
  password = random_password.db.result
}

locals {
  db_url = "postgresql+asyncpg://studio:${random_password.db.result}@/studio?host=/cloudsql/${google_sql_database_instance.studio.connection_name}"
}

resource "google_secret_manager_secret" "db_url" {
  secret_id = "${var.project_name}-db-url"
  project   = var.project_id
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  depends_on = [google_project_service.studio]
}

resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = local.db_url
}

# ---------------------------------------------------------------- IAM for the app service account

resource "google_project_iam_member" "studio_app_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/secretmanager.secretAccessor",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app_sa.email}"
}

# The app signs sandbox JWTs as itself (STUDIO_SANDBOX_CALLER_SA = app SA).
resource "google_service_account_iam_member" "app_sa_signs_as_itself" {
  service_account_id = google_service_account.app_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.app_sa.email}"
}

# ---------------------------------------------------------------- Firebase sign-in

resource "google_firebase_project" "studio" {
  provider   = google-beta
  project    = var.project_id
  depends_on = [google_project_service.studio]
}

resource "google_firebase_web_app" "studio" {
  provider     = google-beta
  project      = var.project_id
  display_name = "Studio web"
  depends_on   = [google_firebase_project.studio]
}

data "google_firebase_web_app_config" "studio" {
  provider   = google-beta
  project    = var.project_id
  web_app_id = google_firebase_web_app.studio.app_id
}

resource "google_identity_platform_config" "studio" {
  provider           = google-beta
  project            = var.project_id
  authorized_domains = ["localhost", "${var.project_id}.firebaseapp.com", "${var.project_id}.web.app", local.run_url, var.domain, "www.${var.domain}"]
  depends_on         = [google_firebase_project.studio]
}

output "firebase_web_config" {
  value = {
    apiKey     = data.google_firebase_web_app_config.studio.api_key
    authDomain = data.google_firebase_web_app_config.studio.auth_domain
    appId      = google_firebase_web_app.studio.app_id
  }
}

output "cloud_sql_connection_name" {
  value = google_sql_database_instance.studio.connection_name
}

# Bill google-beta calls (Firebase, Identity Platform) to this project, not the ADC default quota project.
provider "google-beta" {
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}

# ---------------------------------------------------------------- background worker (Cloud Run Job)

locals {
  # Settings the worker shares with the service (service.tf sets the same values inline).
  worker_env = {
    GOOGLE_CLOUD_PROJECT        = var.project_id
    GOOGLE_CLOUD_LOCATION       = "global"
    GOOGLE_GENAI_USE_ENTERPRISE = "True"
    LOGS_BUCKET_NAME            = google_storage_bucket.logs_data_bucket.name
    STUDIO_BUCKET               = google_storage_bucket.logs_data_bucket.name
    STUDIO_DATA_DIR             = "/tmp/studio"
    STUDIO_MODEL                = var.lead_model
    STUDIO_SANDBOX              = "vertex"
    STUDIO_SANDBOX_LOCATION     = var.region
    STUDIO_SANDBOX_TEMPLATE     = var.sandbox_template
    STUDIO_CAPTURE_TEMPLATE     = var.capture_template
    STUDIO_SANDBOX_CALLER_SA    = google_service_account.app_sa.email
    STUDIO_WORKER_MAX_RETRIES   = "2"
    APP_URL                     = "https://${var.domain}"
    STUDIO_EMAIL_FROM           = "Design Content Studio <hello@${var.domain}>"
    # Helpers, renders, pipelines and check-ins started inside a worker are jobs of their own.
    STUDIO_JOBS            = "cloudrun"
    STUDIO_REGION          = var.region
    STUDIO_WORKER_JOB      = "${var.project_name}-worker"
    STUDIO_SERVICE_ACCOUNT = google_service_account.app_sa.email
    STUDIO_WAKEUP_QUEUE    = google_cloud_tasks_queue.wakeups.name
  }
}

# Check-ins (schedule_wakeup): a task per check-in, due at its time, that calls the worker job's
# jobs:run API with the app service account's OAuth token. No endpoint in Studio is involved.
resource "google_cloud_tasks_queue" "wakeups" {
  name       = "${var.project_name}-wakeups"
  location   = var.region
  project    = var.project_id
  depends_on = [google_project_service.studio]
}

resource "google_cloud_tasks_queue_iam_member" "wakeups_enqueuer" {
  name     = google_cloud_tasks_queue.wakeups.name
  location = var.region
  project  = var.project_id
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.app_sa.email}"
}

resource "google_cloud_run_v2_job" "worker" {
  name                = "${var.project_name}-worker"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.app_sa.email
      timeout         = "86400s" # a lead turn, a helper, a render watch or a whole pipeline run
      max_retries     = 2        # each retry resumes the same ADK invocation
      containers {
        image   = "us-docker.pkg.dev/cloudrun/container/job:latest" # replaced by scripts/deploy.sh
        command = ["uv", "run", "python", "-m", "app.worker"]
        dynamic "env" {
          for_each = local.worker_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.billing_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = toset(["SESSION_SERVICE_URI", "STUDIO_DB_URL"])
          content {
            name = env.value
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.db_url.secret_id
                version = "latest"
              }
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
      }
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.studio.connection_name]
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_project_iam_member.studio_app_roles, google_secret_manager_secret_version.db_url]
}

# The service starts worker executions with per-job arguments (run.jobs.runWithOverrides) ...
resource "google_cloud_run_v2_job_iam_member" "service_runs_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.worker.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.app_sa.email}"
}

# ... as the worker's own service account.
resource "google_service_account_iam_member" "app_sa_acts_as_itself" {
  service_account_id = google_service_account.app_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.app_sa.email}"
}

# Resend API key for "your design is ready" emails. Terraform creates the secret only;
# add the key yourself: gcloud secrets versions add studio-resend-api-key --data-file=-
resource "google_secret_manager_secret" "resend" {
  secret_id = "${var.project_name}-resend-api-key"
  project   = var.project_id
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  depends_on = [google_project_service.studio]
}

output "worker_job_name" {
  value = google_cloud_run_v2_job.worker.name
}
