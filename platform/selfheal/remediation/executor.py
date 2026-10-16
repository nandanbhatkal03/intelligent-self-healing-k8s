"""Executes remediation actions against the Kubernetes API (FR-4.2).

``DryRunExecutor`` records what *would* happen — the default mode while
the detector is being tuned. ``KubernetesExecutor`` performs real actions
with the least-privilege service account from deploy/platform/rbac.yaml
(NFR-5) and returns the information needed to undo each one (FR-4.4).

Action          | API call                                   | Undo
----------------|--------------------------------------------|---------------------------
restart_pod     | delete one pod (ReplicaSet recreates it)    | none — logged only (NFR-4)
scale_deployment| patch deployments/scale                     | scale back to old replicas
rollback_deploy.| patch template to previous ReplicaSet's     | patch template back
"""
from __future__ import annotations

import copy
import time
from typing import Protocol

from .models import Action, ActionResult

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


class Executor(Protocol):
    def execute(self, action: Action) -> ActionResult: ...
    def undo(self, result: ActionResult) -> str: ...
    def recent_rollout(self, namespace: str, deployment: str, within_s: float) -> bool: ...


class DryRunExecutor:
    def __init__(self):
        self.executed: list[Action] = []

    def execute(self, action: Action) -> ActionResult:
        self.executed.append(action)
        return ActionResult(action, executed=False, detail=f"dry-run: would {action.type} {action.deployment}")

    def undo(self, result: ActionResult) -> str:
        return "dry-run: nothing to undo"

    def recent_rollout(self, namespace: str, deployment: str, within_s: float) -> bool:
        return False


class KubernetesExecutor:
    def __init__(self, in_cluster: bool | None = None):
        from kubernetes import client, config  # imported lazily: optional dependency
        if in_cluster is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        elif in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.api = client.ApiClient()

    # ------------------------------------------------------------ dispatch
    def execute(self, action: Action) -> ActionResult:
        handler = {
            "restart_pod": self._restart_pod,
            "scale_deployment": self._scale,
            "rollback_deployment": self._rollback,
        }.get(action.type)
        if handler is None:
            return ActionResult(action, executed=False, detail=f"no executor for {action.type}")
        return handler(action)

    def undo(self, result: ActionResult) -> str:
        u = result.undo
        if not u:
            return "irreversible action — see audit log for manual reversal"
        ns, dep = result.action.namespace, result.action.deployment
        if u["type"] == "scale":
            self.apps.patch_namespaced_deployment_scale(dep, ns, {"spec": {"replicas": u["replicas"]}})
            return f"scaled {dep} back to {u['replicas']}"
        if u["type"] == "template":
            self.apps.patch_namespaced_deployment(dep, ns, _replace_template(u["template"]))
            return f"restored {dep} to revision {u.get('revision')}"
        return f"unknown undo type {u['type']}"

    # ------------------------------------------------------------- actions
    def _restart_pod(self, action: Action) -> ActionResult:
        pods = self._pods(action.namespace, action.deployment)
        if not pods:
            return ActionResult(action, executed=False, detail="no pods found")
        target = action.params.get("pod")
        if target is None:
            # Prefer the pod that is struggling most: most restarts, then oldest.
            def restarts(p):
                return sum(cs.restart_count for cs in (p.status.container_statuses or []))
            pods.sort(key=lambda p: (-restarts(p), p.metadata.creation_timestamp))
            target = pods[0].metadata.name
        self.core.delete_namespaced_pod(target, action.namespace)
        return ActionResult(action, executed=True, detail=f"deleted pod {target}", undo=None)

    def _scale(self, action: Action) -> ActionResult:
        ns, dep = action.namespace, action.deployment
        current = self.apps.read_namespaced_deployment_scale(dep, ns).spec.replicas
        step = int(action.params.get("step", 1))
        max_r = int(action.params.get("max_replicas", 5))
        desired = min(current + step, max_r)
        if desired == current:
            return ActionResult(action, executed=False, detail=f"already at max_replicas={max_r}")
        self.apps.patch_namespaced_deployment_scale(dep, ns, {"spec": {"replicas": desired}})
        return ActionResult(action, executed=True, detail=f"scaled {dep} {current}->{desired}",
                            undo={"type": "scale", "replicas": current})

    def _rollback(self, action: Action) -> ActionResult:
        """Equivalent of `kubectl rollout undo` (which is client-side only)."""
        ns, dep_name = action.namespace, action.deployment
        dep = self.apps.read_namespaced_deployment(dep_name, ns)
        current_rev = int(dep.metadata.annotations.get(REVISION_ANNOTATION, "0"))
        rs_list = self._replicasets(ns, dep)
        older = [rs for rs in rs_list if _rev(rs) < current_rev]
        if not older:
            return ActionResult(action, executed=False, detail="no previous revision to roll back to")
        previous = max(older, key=_rev)
        template = self.api.sanitize_for_serialization(previous.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        current_template = copy.deepcopy(self.api.sanitize_for_serialization(dep.spec.template))
        self.apps.patch_namespaced_deployment(dep_name, ns, _replace_template(template))
        return ActionResult(action, executed=True,
                            detail=f"rolled {dep_name} back from revision {current_rev} to {_rev(previous)}",
                            undo={"type": "template", "template": current_template, "revision": current_rev})

    # ------------------------------------------------------------- queries
    def recent_rollout(self, namespace: str, deployment: str, within_s: float) -> bool:
        dep = self.apps.read_namespaced_deployment(deployment, namespace)
        rs_list = self._replicasets(namespace, dep)
        if len(rs_list) < 2:
            return False          # first revision: nothing to blame on a release
        newest = max(rs_list, key=_rev)
        age = time.time() - newest.metadata.creation_timestamp.timestamp()
        return age <= within_s

    def _pods(self, ns: str, deployment: str) -> list:
        dep = self.apps.read_namespaced_deployment(deployment, ns)
        selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
        return [p for p in self.core.list_namespaced_pod(ns, label_selector=selector).items
                if p.metadata.deletion_timestamp is None]

    def _replicasets(self, ns: str, dep) -> list:
        selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
        return [rs for rs in self.apps.list_namespaced_replica_set(ns, label_selector=selector).items
                if any(o.uid == dep.metadata.uid for o in (rs.metadata.owner_references or []))]


def _replace_template(template: dict) -> list[dict]:
    """JSON patch (a list body makes the client send application/json-patch+json).
    A full *replace* — not a strategic merge — so fields added by the bad
    revision are removed too, exactly like `kubectl rollout undo`."""
    return [{"op": "replace", "path": "/spec/template", "value": template}]


def _rev(rs) -> int:
    return int((rs.metadata.annotations or {}).get(REVISION_ANNOTATION, "0"))
