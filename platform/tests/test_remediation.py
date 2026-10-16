import tempfile
import unittest
from pathlib import Path

from selfheal.config import DEFAULT_CONFIG, load_config
from selfheal.remediation.audit import AuditLog
from selfheal.remediation.controller import Controller
from selfheal.remediation.models import Action, ActionResult, Signal
from selfheal.remediation.policy import Policy
from selfheal.remediation.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeExecutor:
    def __init__(self, reversible=True, recent=False):
        self.executed, self.undone = [], []
        self.reversible, self.recent = reversible, recent

    def execute(self, action: Action) -> ActionResult:
        self.executed.append(action)
        return ActionResult(action, True, "ok", undo={"type": "scale", "replicas": 2} if self.reversible else None)

    def undo(self, result):
        self.undone.append(result)
        return "undone"

    def recent_rollout(self, ns, dep, within_s):
        return self.recent


def sig(reason="memory", dep="orders"):
    return Signal("anomaly", "shoplite", dep, reason, 1.0)


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = Policy.from_config(load_config(DEFAULT_CONFIG).remediation.policies)

    def test_shipped_policy_mapping(self):                         # FR-4.1
        self.assertEqual(self.policy.select(sig("memory")).type, "restart_pod")
        self.assertEqual(self.policy.select(sig("cpu")).type, "scale_deployment")
        self.assertEqual(self.policy.select(sig("unknown")).type, "alert_only")

    def test_errors_after_rollout_rolls_back(self):
        s = sig("errors")
        s.evidence["recent_rollout"] = True
        self.assertEqual(self.policy.select(s).type, "rollback_deployment")
        s2 = sig("errors")
        s2.evidence["recent_rollout"] = False
        self.assertEqual(self.policy.select(s2).type, "restart_pod")

    def test_invalid_action_rejected(self):
        with self.assertRaises(ValueError):
            Policy.from_config([{"name": "x", "action": "delete_cluster", "match": {}}])

    def test_catch_all_added(self):
        p = Policy.from_config([{"name": "m", "action": "restart_pod", "match": {"reason": "memory"}}])
        self.assertEqual(p.select(sig("cpu")).type, "alert_only")


class RateLimiterTest(unittest.TestCase):
    def test_sliding_window(self):                                 # FR-4.5
        clock = FakeClock()
        rl = RateLimiter(2, 60, clock)
        for _ in range(2):
            self.assertTrue(rl.allow("k"))
            rl.record("k")
        self.assertFalse(rl.allow("k"))
        self.assertTrue(rl.allow("other"))
        clock.t += 61
        self.assertTrue(rl.allow("k"))


class ControllerTest(unittest.TestCase):
    def make(self, health, executor=None, max_actions=3, pause=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = AuditLog(Path(self.tmp.name) / "audit.jsonl")
        self.clock = FakeClock()
        self.escalations = []
        self.executor = executor or FakeExecutor()
        return Controller(
            policy=Policy.from_config(load_config(DEFAULT_CONFIG).remediation.policies),
            rate_limiter=RateLimiter(max_actions, 600, self.clock), executor=self.executor,
            audit=self.audit, health_check=health,
            escalate=lambda s, m: self.escalations.append(m),
            verify_window_s=60, verify_poll_s=5, pause_flag=pause,
            clock=self.clock, sleep=self.clock.sleep)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        return [e["event"] for e in self.audit.read()]

    def test_recovers_and_audits(self):                            # FR-4.2, 4.3, 4.6
        c = self.make(health=lambda ns, d: True)
        out = c.handle(sig("cpu"))
        self.assertEqual(out.status, "recovered")
        self.assertEqual(self.executor.executed[0].type, "scale_deployment")
        self.assertEqual(self.events(), ["action_selected", "action_executed", "verified"])
        self.assertLessEqual(out.verify_seconds, 60)

    def test_no_improvement_undoes_and_escalates(self):            # FR-4.4
        c = self.make(health=lambda ns, d: False)
        out = c.handle(sig("cpu"))
        self.assertEqual(out.status, "escalated")
        self.assertEqual(len(self.executor.undone), 1)
        self.assertTrue(self.escalations)
        self.assertGreaterEqual(out.verify_seconds, 60)

    def test_irreversible_action_is_escalated_not_undone(self):     # NFR-4
        c = self.make(health=lambda ns, d: False, executor=FakeExecutor(reversible=False))
        out = c.handle(sig("memory"))
        self.assertEqual(out.status, "escalated")
        self.assertEqual(self.executor.undone, [])
        self.assertIn("irreversible", out.detail)

    def test_single_healthy_blip_is_not_recovery(self):
        answers = iter([True, False] * 20)
        c = self.make(health=lambda ns, d: next(answers))
        self.assertEqual(c.handle(sig("cpu")).status, "escalated")

    def test_rate_limit_blocks_action(self):                       # NFR-3
        c = self.make(health=lambda ns, d: True, max_actions=1)
        c.handle(sig("cpu"))
        out = c.handle(sig("cpu"))
        self.assertEqual(out.status, "rate_limited")
        self.assertEqual(len(self.executor.executed), 1)

    def test_pause_flag(self):                                     # SRS 3.1 manual override
        with tempfile.NamedTemporaryFile() as flag:
            c = self.make(health=lambda ns, d: True, pause=Path(flag.name))
            self.assertEqual(c.handle(sig("cpu")).status, "paused")
            self.assertEqual(self.executor.executed, [])

    def test_bad_release_detected_via_recent_rollout(self):
        c = self.make(health=lambda ns, d: True, executor=FakeExecutor(recent=True))
        c.handle(sig("errors"))
        self.assertEqual(self.executor.executed[0].type, "rollback_deployment")

    def test_alert_only_takes_no_action(self):
        c = self.make(health=lambda ns, d: True)
        self.assertEqual(c.handle(sig("unknown")).status, "alerted")
        self.assertEqual(self.executor.executed, [])

    def test_baseline_mode_disabled(self):                         # FR-6.3
        c = self.make(health=lambda ns, d: True)
        c.enabled = False
        self.assertEqual(c.handle(sig("cpu")).status, "disabled")
