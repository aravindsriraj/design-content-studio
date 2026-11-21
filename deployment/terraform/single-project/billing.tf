# Plans and credits (app/billing.py). Dodo Payments keys are read at runtime from Secret Manager
# (`studio-dodo-api-key`, `studio-dodo-webhook-key`, added by hand); the app's service account already
# holds secretAccessor. Until they exist, checkout reports "not set up" and everyone is on Free.

variable "owner_emails" {
  type        = string
  description = "Comma-separated emails that are always Pro with unlimited credits (the owner)."
  default     = ""
}

variable "dodo_environment" {
  type        = string
  description = "test_mode until go-live, then live_mode (keys and products differ per mode)."
  default     = "test_mode"
}

variable "dodo_products" {
  type        = object({ pro_monthly = string, pro_annual = string, topup = string })
  description = "Dodo product ids (pdt_...) for Pro monthly, Pro annual and the 10-credit top-up."
  default     = { pro_monthly = "", pro_annual = "", topup = "" }
}

locals {
  billing_env = {
    STUDIO_OWNER_EMAILS       = var.owner_emails
    DODO_PAYMENTS_ENVIRONMENT = var.dodo_environment
    DODO_PRODUCT_PRO_MONTHLY  = var.dodo_products.pro_monthly
    DODO_PRODUCT_PRO_ANNUAL   = var.dodo_products.pro_annual
    DODO_PRODUCT_TOPUP        = var.dodo_products.topup
  }
}
