terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "app" {
  account_id   = "chat-eval-app"
  display_name = "Chat evaluation harness"
}

resource "google_sql_database_instance" "postgres" {
  name             = "chat-eval-postgres"
  database_version = "POSTGRES_16"
  region           = var.region
  settings {
    tier              = "db-f1-micro"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10
    disk_autoresize   = true
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = false
    }
    ip_configuration { ipv4_enabled = true }
  }
  deletion_protection = var.deletion_protection
  depends_on          = [google_project_service.apis]
}

resource "google_sql_database" "app" {
  name     = "chat_evaluation"
  instance = google_sql_database_instance.postgres.name
}

resource "random_password" "database" {
  length  = 32
  special = false
}
resource "random_password" "tester" {
  length  = 32
  special = false
}
resource "random_password" "admin" {
  length  = 32
  special = false
}

resource "google_sql_user" "app" {
  name     = "chat_eval"
  instance = google_sql_database_instance.postgres.name
  password = random_password.database.result
}

locals {
  generated_secrets = {
    database-url       = "postgresql+psycopg://chat_eval:${random_password.database.result}@/chat_evaluation?host=/cloudsql/${google_sql_database_instance.postgres.connection_name}"
    tester-access-code = random_password.tester.result
    admin-access-code  = random_password.admin.result
  }
}

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(["database-url", "tester-access-code", "admin-access-code", "model-api-key"])
  secret_id = "chat-eval-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = local.generated_secrets
  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_cloud_run_v2_service" "app" {
  name     = "chat-evaluation-harness"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  template {
    service_account = google_service_account.app.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [google_sql_database_instance.postgres.connection_name] }
    }
    containers {
      image = var.image
      ports { container_port = 8080 }
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      env {
        name  = "COOKIE_SECURE"
        value = "true"
      }
      env {
        name  = "ENVIRONMENT"
        value = "production"
      }
      env {
        name  = "MODEL_PROVIDER"
        value = var.model_provider
      }
      env {
        name  = "MODEL_ENDPOINT"
        value = var.model_endpoint
      }
      dynamic "env" {
        for_each = var.model_api_key_secret_version == null ? toset([]) : toset([
          var.model_api_key_secret_version
        ])
        content {
          name = "MODEL_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets["model-api-key"].secret_id
              version = env.value
            }
          }
        }
      }
      dynamic "env" {
        for_each = {
          DATABASE_URL       = "database-url"
          TESTER_ACCESS_CODE = "tester-access-code"
          ADMIN_ACCESS_CODE  = "admin-access-code"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.access,
    google_secret_manager_secret_version.generated,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "cloud_run_url" { value = google_cloud_run_v2_service.app.uri }
output "cloud_sql_connection_name" { value = google_sql_database_instance.postgres.connection_name }
