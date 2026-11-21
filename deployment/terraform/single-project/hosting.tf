# Custom domain: Firebase Hosting in front of the Cloud Run service. Cloud Run's own domain
# mapping is not offered in asia-south1. Hosting forwards every path to the service (the app only
# polls, so Hosting's 60 s limit per forwarded request is fine) and provides the HTTPS certificate.
# DNS is at the registrar: `terraform output domain_dns_records` lists what to add there.

variable "domain" {
  type        = string
  description = "The app's public domain (apex), e.g. example.com; www redirects to it."
}

resource "google_project_service" "hosting" {
  project            = var.project_id
  service            = "firebasehosting.googleapis.com"
  disable_on_destroy = false
}

resource "google_firebase_hosting_version" "app" {
  provider = google-beta
  site_id  = var.project_id
  config {
    rewrites {
      glob = "**"
      run {
        service_id = google_cloud_run_v2_service.app.name
        region     = var.region
      }
    }
  }
  depends_on = [google_project_service.hosting]
}

resource "google_firebase_hosting_release" "app" {
  provider     = google-beta
  site_id      = var.project_id
  version_name = google_firebase_hosting_version.app.name
  message      = "Forward every path to Cloud Run ${google_cloud_run_v2_service.app.name}"
}

resource "google_firebase_hosting_custom_domain" "apex" {
  provider              = google-beta
  project               = var.project_id
  site_id               = var.project_id
  custom_domain         = var.domain
  wait_dns_verification = false
  depends_on            = [google_firebase_hosting_release.app]
}

resource "google_firebase_hosting_custom_domain" "www" {
  provider              = google-beta
  project               = var.project_id
  site_id               = var.project_id
  custom_domain         = "www.${var.domain}"
  redirect_target       = var.domain
  wait_dns_verification = false
  depends_on            = [google_firebase_hosting_custom_domain.apex]
}

output "domain_dns_records" {
  description = "Records to add at the registrar for the custom domain."
  value = {
    for d in [google_firebase_hosting_custom_domain.apex, google_firebase_hosting_custom_domain.www] :
    d.custom_domain => d.required_dns_updates
  }
}
