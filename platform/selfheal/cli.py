"""`selfheal` command-line interface (SRS §3.1: policy config + manual override).

    selfheal check                        connectivity + data sanity
    selfheal features                     current feature vector per deployment
    selfheal collect --minutes 120        export normal-operation training data
    selfheal train   [--data data/normal-*.csv]   train one detector per deployment (all sessions)
    selfheal detect                       one detection pass, no remediation
    selfheal run                          observe -> detect -> classify (Semester 3)
    selfheal run --mode dry_run|enforce   + remediation (Objective 5, Semester 4)
    selfheal pause | resume | status      manual override of automated remediation
    selfheal experiment run <manifest> --mode baseline|selfheal
    selfheal experiment mark --fault bad-release --target shoplite/orders --mode selfheal
    selfheal experiment end <id> | list
    selfheal report [--hours 24]          Availability / MTTR / MTBF / timings
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time

import pandas as pd

from .config import load_config


def _clients(cfg, loki: bool = True):
    from .collectors.loki import LokiClient
    from .collectors.prometheus import PrometheusClient
    return PrometheusClient(cfg.prometheus_url), (LokiClient(cfg.loki_url) if loki else None)


# --------------------------------------------------------------- commands
def cmd_check(cfg, args) -> int:
    from .engine import load_models
    from .features import FEATURES, render
    prom, loki = _clients(cfg)
    ok = True
    p_ok, l_ok = prom.healthy(), loki.healthy()
    print(f"Prometheus {cfg.prometheus_url}: {'OK' if p_ok else 'UNREACHABLE'}")
    print(f"Loki       {cfg.loki_url}: {'OK' if l_ok else 'UNREACHABLE'}")
    ok &= p_ok and l_ok
    if p_ok:
        print("\nFeature queries (first deployment):")
        dep = cfg.target.deployments[0]
        for f in FEATURES:
            n = len(prom.query(render(f, cfg.target.namespace, dep)))
            print(f"  {'ok ' if n else '-- '} {f:<14} {n} series")
    models = load_models(cfg)
    print(f"\nModels: {sorted(models) or 'none (run `selfheal train`)'}")
    print(f"Remediation mode: {cfg.remediation.mode}   paused: {cfg.path(cfg.paths.pause_flag).exists()}")
    return 0 if ok else 1


def cmd_features(cfg, args) -> int:
    from .features import instant_features
    prom, _ = _clients(cfg, loki=False)
    df = instant_features(prom, cfg.target.namespace, cfg.target.deployments)
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.4g}".format):
        print(df)
    return 0


def cmd_collect(cfg, args) -> int:
    from .features import range_features
    prom, _ = _clients(cfg, loki=False)
    end = time.time() if args.end is None else args.end
    start = end - args.minutes * 60 if args.start is None else args.start
    print(f"Collecting {(end - start) / 60:.0f} min at {args.step}s step ...")
    df = range_features(prom, cfg.target.namespace, cfg.target.deployments, start, end, args.step)
    out = cfg.path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} rows ({df['deployment'].nunique() if len(df) else 0} deployments) to {out}")
    return 0 if len(df) else 1


def load_training_data(cfg, patterns: list[str], keep_unhealthy: bool = False) -> tuple[pd.DataFrame, dict]:
    """Combine one or more collected CSVs (sessions need not be contiguous)
    and drop windows that are not 'normal': missing replicas, recent
    restarts, or elevated errors — e.g. the minutes right after `make resume`."""
    import glob as _glob
    files = sorted({f for p in patterns for f in _glob.glob(str(cfg.path(p)))})
    if not files:
        raise FileNotFoundError(f"no CSV files match {patterns} (run `selfheal collect` first)")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp", "deployment"]).sort_values(["deployment", "timestamp"])
    info = {"files": files, "rows_total": len(df)}
    if not keep_unhealthy:
        bad = (df["ready_ratio"] < 1.0) | (df["restarts_5m"] > 0) | (df["error_ratio"] > 0.05)
        if "oom_kills_5m" in df:
            bad |= df["oom_kills_5m"] > 0
        info["rows_dropped_unhealthy"] = int(bad.sum())
        df = df[~bad]
    ts = df["timestamp"].drop_duplicates().sort_values()
    gaps = ts.diff().fillna(0)
    step = gaps[gaps > 0].median() if (gaps > 0).any() else 15
    info["hours_covered"] = round(float(gaps[gaps <= 3 * step].sum()) / 3600, 2)
    info["sessions"] = int((gaps > 3 * step).sum()) + 1 if len(ts) else 0
    return df, info


def cmd_train(cfg, args) -> int:
    from .detection.detector import train_per_deployment
    df, info = load_training_data(cfg, args.data, args.keep_unhealthy)
    print(f"Training data: {len(info['files'])} file(s), {info['sessions']} session(s), "
          f"{info['hours_covered']} h of normal operation")
    if "rows_dropped_unhealthy" in info:
        print(f"  dropped {info['rows_dropped_unhealthy']} of {info['rows_total']} windows that were not "
              f"healthy (restarts, missing replicas, errors)")
    if info["hours_covered"] < 2:
        print("  WARNING: less than 2 h of data; collect more for a reliable detector (aim for 6+ h)")
    models = train_per_deployment(df, cfg.path(cfg.detection.model_dir),
                                  cfg.detection.contamination, cfg.detection.threshold_quantile,
                                  cfg.detection.zscore_limit)
    for dep, m in models.items():
        print(f"{dep:<12} trained on {m.n_train_} windows, threshold={m.threshold:.4f}")
    return 0


def _engine(cfg, mode: str):
    from .engine import Engine, HealthChecker, pause_flag
    from .remediation.audit import AuditLog
    from .remediation.controller import Controller, Escalator
    from .remediation.executor import DryRunExecutor, KubernetesExecutor
    from .remediation.policy import Policy
    from .remediation.ratelimit import RateLimiter

    prom, loki = _clients(cfg)
    audit = AuditLog(cfg.path(cfg.paths.audit_log))
    r = cfg.remediation
    executor = KubernetesExecutor() if mode == "enforce" else DryRunExecutor()
    controller = Controller(
        policy=Policy.from_config(r.policies),
        rate_limiter=RateLimiter(r.rate_limit["max_actions"], r.rate_limit["per_seconds"]),
        executor=executor, audit=audit, health_check=HealthChecker(cfg, prom),
        escalate=Escalator(r.escalation_webhook, audit),
        verify_window_s=r.verify_window_seconds, verify_poll_s=r.verify_poll_seconds,
        recent_rollout_s=r.recent_rollout_seconds, enabled=(mode != "off"),
        pause_flag=pause_flag(cfg))
    # mode "off": no controller at all — observe, detect, classify (Objective 2)
    return Engine(cfg, prom, loki, audit, controller if mode != "off" else None), audit


def cmd_detect(cfg, args) -> int:
    engine, audit = _engine(cfg, "off")
    signals = engine.tick()
    if not engine.models:
        print("No trained models — run `selfheal collect` then `selfheal train`.")
        return 1
    print(f"{len(signals)} signal(s)")
    for s in signals:
        print(f"  {s.kind:<14} {s.key:<22} reason={s.reason:<12} score={s.score:.3f}")
    return 0


def cmd_run(cfg, args) -> int:
    mode = args.mode or cfg.remediation.mode
    engine, _ = _engine(cfg, mode)
    print(f"selfheal running: remediation={mode}, prediction={'on' if cfg.prediction.enabled else 'off'}, "
          f"interval={cfg.detection.interval_seconds}s, models={sorted(engine.models)}  (Ctrl-C to stop)")
    try:
        engine.run()
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_pause(cfg, args) -> int:
    flag = cfg.path(cfg.paths.pause_flag)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    print("automated remediation PAUSED (detection continues)")
    return 0


def cmd_resume(cfg, args) -> int:
    cfg.path(cfg.paths.pause_flag).unlink(missing_ok=True)
    print("automated remediation RESUMED")
    return 0


def cmd_status(cfg, args) -> int:
    from .remediation.audit import AuditLog
    print(f"mode={cfg.remediation.mode} paused={cfg.path(cfg.paths.pause_flag).exists()}")
    events = list(AuditLog(cfg.path(cfg.paths.audit_log)).read())
    interesting = [e for e in events if e["event"] != "detection" or e.get("is_anomaly")]
    for e in interesting[-args.last:]:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        rest = {k: v for k, v in e.items() if k not in ("ts", "actor", "event", "signal", "contributors")}
        print(f"{ts} {e['event']:<16} {json.dumps(rest, default=str)[:150]}")
    return 0


def cmd_detections(cfg, args) -> int:
    """Compact table of anomaly detections for the C4 evidence table:
    time, deployment, trigger, class, basis, score/threshold."""
    from .remediation.audit import AuditLog
    since = time.time() - args.minutes * 60 if args.minutes else 0
    rows = [e for e in AuditLog(cfg.path(cfg.paths.audit_log)).read()
            if e["event"] == "detection" and e["ts"] >= since
            and (e.get("is_anomaly") or (args.all and e.get("raw_anomaly")))]
    if not rows:
        print("no detections" + (f" in the last {args.minutes:g} min" if args.minutes else ""))
        return 0
    print(f"{'time':<8}  {'deployment':<10}  {'confirmed':<9}  {'trigger':<7}  {'class':<11}  "
          f"{'score/thr':<11}  basis")
    for e in rows[-args.last:]:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        dep = e.get("deployment") or str(e.get("target", "")).split("/")[-1]
        score = f"{e.get('score', 0):.3f}/{e.get('threshold', 0):.3f}"
        print(f"{ts:<8}  {dep:<10}  {'yes' if e.get('is_anomaly') else 'no':<9}  {str(e.get('trigger')):<7}  "
              f"{str(e.get('reason')):<11}  {score:<11}  {e.get('basis')}")
    return 0


def cmd_experiment(cfg, args) -> int:
    from .evaluation.experiments import Experiment, ExperimentLog, run_chaos
    elog = ExperimentLog(cfg.path(cfg.paths.experiments_log))
    if args.action == "run":
        run_chaos(args.manifest, args.mode, elog, observe_s=args.observe)
    elif args.action == "mark":
        exp = Experiment(fault=args.fault, target=args.target, mode=args.mode,
                         injected_at=time.time(), notes=args.notes or "")
        elog.save(exp)
        print(f"marked experiment {exp.id} — run `selfheal experiment end {exp.id}` when the fault is removed")
    elif args.action == "end":
        exp = next((e for e in elog.all() if e.id == args.id), None)
        if exp is None:
            print(f"no experiment {args.id}")
            return 1
        exp.ended_at = time.time()
        elog.save(exp)
        print(f"ended {exp.id}")
    else:  # list
        for e in elog.all():
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.injected_at))
            dur = f"{(e.ended_at - e.injected_at):.0f}s" if e.ended_at else "open"
            print(f"{e.id}  {ts}  {e.mode:<9} {e.fault:<20} {e.target:<22} {dur}")
    return 0


def cmd_report(cfg, args) -> int:
    from .evaluation.experiments import ExperimentLog, analyse, summarise
    from .evaluation.metrics import availability, downtime_incidents, mtbf, mttr
    from .remediation.audit import AuditLog

    prom, _ = _clients(cfg, loki=False)
    end = time.time()
    start = end - args.hours * 3600
    series = prom.query_range(f'min(probe_success{{job=~"{args.probe_job}"}})', start, end, 5)
    probe = series[0].points if series else []
    if not probe:
        print("No probe_success data — is the blackbox Probe installed?")
        return 1

    incidents = downtime_incidents(probe, merge_gap_s=10)
    print(f"Period: last {args.hours} h   samples={len(probe)}")
    print(f"  Availability : {availability(probe) * 100:.3f} %")
    print(f"  Incidents    : {len(incidents)}")
    m = mttr(incidents)
    b = mtbf(incidents, probe[0][0], probe[-1][0])
    print(f"  MTTR         : {'n/a' if math.isnan(m) else f'{m:.1f} s'}")
    print(f"  MTBF         : {'no failures' if math.isinf(b) else f'{b / 60:.1f} min'}")

    exps = [e for e in ExperimentLog(cfg.path(cfg.paths.experiments_log)).all() if e.injected_at >= start]
    audit = list(AuditLog(cfg.path(cfg.paths.audit_log)).read())
    ns = cfg.target.namespace
    alert_series = prom.query_range(
        f'count(ALERTS{{alertstate="firing",namespace="{ns}",alertname=~"ShopLite.*"}}) or vector(0)',
        start, end, 5)
    alerts = alert_series[0].points if alert_series else []
    exps.sort(key=lambda e: e.injected_at)
    nxt = [e.injected_at for e in exps[1:]] + [None]
    rows = [analyse(e, probe, audit, alerts, until=n) for e, n in zip(exps, nxt, strict=True)]
    summaries = {}
    if rows:
        print("\nExperiments:")
        print(pd.DataFrame(rows).drop(columns=["id"]).to_string(index=False))
        print("\nSummary by mode (Objective 3 baseline vs. self-healing):")
        for mode in sorted({r["mode"] for r in rows}):
            mode_rows = [r for r in rows if r["mode"] == mode]
            mode_exps = [e for e in exps if e.mode == mode]
            summaries[mode] = summarise(mode_rows, probe, mode_exps)
            print(f"  [{mode}]")
            for k, v in summaries[mode].items():
                print(f"      {k:<34} {v}")
        faults = sorted({r["fault"] for r in rows})
        if len(faults) > 1:
            print("\nBy fault:")
            for fault in faults:
                for mode in sorted({r["mode"] for r in rows if r["fault"] == fault}):
                    sel = [r for r in rows if r["fault"] == fault and r["mode"] == mode]
                    sm = summarise(sel)
                    print(f"  {fault:<20} {mode:<9} n={sm['experiments']:<3} "
                          f"MTTD(alerts)={_s(sm['MTTD_alerts_s'])} MTTD(ml)={_s(sm['MTTD_ml_s'])} "
                          f"MTTR={_s(sm['MTTR_s'])} outage={_s(sm['mean_outage_s'])}")
    out = cfg.path(f"reports/report-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"period_hours": args.hours, "availability": availability(probe),
                               "incidents": len(incidents), "mttr_s": m,
                               "mtbf_s": None if math.isinf(b) else b, "summary_by_mode": summaries,
                               "experiments": rows},
                              indent=2, default=str))
    print(f"\nSaved {out}")
    return 0


def _s(v) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v}s"


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="selfheal", description="Intelligent self-healing for Kubernetes")
    p.add_argument("-c", "--config", help="config file (default platform/config/selfheal.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="connectivity and data sanity").set_defaults(fn=cmd_check)
    sub.add_parser("features", help="current feature vector").set_defaults(fn=cmd_features)

    c = sub.add_parser("collect", help="export training data from Prometheus")
    c.add_argument("--minutes", type=float, default=120)
    c.add_argument("--start", type=float, help="unix ts (overrides --minutes)")
    c.add_argument("--end", type=float, help="unix ts (default now)")
    c.add_argument("--step", type=float, default=15)
    c.add_argument("--out", default="data/normal.csv")
    c.set_defaults(fn=cmd_collect)

    t = sub.add_parser("train", help="train detectors from collected data")
    t.add_argument("--data", nargs="+", default=["data/normal*.csv"],
                   help="CSV files or globs, e.g. data/normal-*.csv (combined; gaps are fine)")
    t.add_argument("--keep-unhealthy", action="store_true",
                   help="do not drop windows with restarts / missing replicas / errors")
    t.set_defaults(fn=cmd_train)

    sub.add_parser("detect", help="one detection pass, no remediation").set_defaults(fn=cmd_detect)

    r = sub.add_parser("run", help="run the closed loop")
    r.add_argument("--mode", choices=["off", "dry_run", "enforce"])
    r.set_defaults(fn=cmd_run)

    sub.add_parser("pause", help="pause automated remediation").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume automated remediation").set_defaults(fn=cmd_resume)
    s = sub.add_parser("status", help="mode, pause state and recent audit events")
    s.add_argument("--last", type=int, default=20)
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("detections", help="table of anomaly detections: class, basis, trigger (Phase C4)")
    d.add_argument("--minutes", type=float, help="only the last N minutes (default: all)")
    d.add_argument("--last", type=int, default=30)
    d.add_argument("--all", action="store_true", help="include anomalies not confirmed by logs")
    d.set_defaults(fn=cmd_detections)

    e = sub.add_parser("experiment", help="fault-injection experiments")
    esub = e.add_subparsers(dest="action", required=True)
    er = esub.add_parser("run", help="apply a Chaos Mesh manifest and record it")
    er.add_argument("manifest")
    er.add_argument("--mode", choices=["baseline", "selfheal"], required=True)
    er.add_argument("--observe", type=float, help="seconds to keep the fault (default: manifest duration)")
    em = esub.add_parser("mark", help="record a fault injected by other means (app-fault.sh, bad-release.sh)")
    em.add_argument("--fault", required=True)
    em.add_argument("--target", required=True, help="namespace/deployment")
    em.add_argument("--mode", choices=["baseline", "selfheal"], required=True)
    em.add_argument("--notes")
    ee = esub.add_parser("end", help="close a marked experiment")
    ee.add_argument("id")
    esub.add_parser("list")
    e.set_defaults(fn=cmd_experiment)

    rp = sub.add_parser("report", help="Availability / MTTR / MTBF and per-experiment timings")
    rp.add_argument("--hours", type=float, default=24)
    rp.add_argument("--probe-job", default=".*shoplite-availability.*")
    rp.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        return args.fn(cfg, args)
    except Exception as exc:  # friendly one-line errors for expected failures
        from .collectors.prometheus import PrometheusError
        if isinstance(exc, (PrometheusError, FileNotFoundError)):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
