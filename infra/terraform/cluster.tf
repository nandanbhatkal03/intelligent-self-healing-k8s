# Objective 1: reproducible local Kubernetes environment.
# Same topology as cluster/kind-config.yaml (used by the script fallback).
resource "kind_cluster" "this" {
  name            = var.cluster_name
  node_image      = var.node_image
  kubeconfig_path = pathexpand(var.kubeconfig_path)
  wait_for_ready  = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"
    }

    dynamic "node" {
      for_each = range(var.worker_count)
      content {
        role = "worker"
      }
    }
  }
}

provider "kubernetes" {
  host                   = kind_cluster.this.endpoint
  client_certificate     = kind_cluster.this.client_certificate
  client_key             = kind_cluster.this.client_key
  cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
}

provider "helm" {
  kubernetes {
    host                   = kind_cluster.this.endpoint
    client_certificate     = kind_cluster.this.client_certificate
    client_key             = kind_cluster.this.client_key
    cluster_ca_certificate = kind_cluster.this.cluster_ca_certificate
  }
}

locals {
  namespaces = ["monitoring", "observability", "chaos-mesh", "shoplite", "selfheal"]
  obs_dir    = "${path.module}/../../deploy/observability"
}

resource "kubernetes_namespace_v1" "ns" {
  for_each = toset(local.namespaces)
  metadata {
    name   = each.key
    labels = { "app.kubernetes.io/part-of" = "selfheal-testbed" }
  }
}
