# Objective 3: fault-injection engine.
resource "helm_release" "chaos_mesh" {
  count      = var.install_chaos_mesh ? 1 : 0
  name       = "chaos-mesh"
  repository = "https://charts.chaos-mesh.org"
  chart      = "chaos-mesh"
  version    = var.chart_versions.chaos_mesh
  namespace  = kubernetes_namespace_v1.ns["chaos-mesh"].metadata[0].name
  timeout    = 900

  # Kind nodes run containerd, not Docker
  set {
    name  = "chaosDaemon.runtime"
    value = "containerd"
  }
  set {
    name  = "chaosDaemon.socketPath"
    value = "/run/containerd/containerd.sock"
  }
  set {
    name  = "dashboard.securityMode"
    value = "false" # local evaluation cluster only
  }
}

# The remediation controller's ServiceAccount/Role live in
# deploy/platform/rbac.yaml (applied by `make app`), not here, so the RBAC
# design is in one reviewable file.
