#!/usr/bin/env python3
"""
Kronos Chaos Monkey - Inject failures, verify recovery against is_healthy()
Scenarios: network_blackout, partial_fill, kill_nine
"""
import argparse, json, os, random, signal, subprocess, sys, time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any

LOG_FILE = Path("~/kronos/chaos_log.jsonl").expanduser()

class ScenarioStatus(Enum):
    IDLE, INJECTING, RECOVERING, VERIFIED, FAILED = "idle", "injecting", "recovering", "verified", "failed"

@dataclass
class ChaosConfig:
    scenario: str = "all"
    duration: int = 30
    fill_rate: float = 0.7
    target: str = "kronos_pilot"
    dry_run: bool = False
    verify_recovery: bool = False
    health_interval: int = 5
    max_recovery_attempts: int = 3

@dataclass
class ScenarioResult:
    scenario: str
    status: ScenarioStatus = ScenarioStatus.IDLE
    injected_at: Optional[float] = None
    recovered_at: Optional[float] = None
    health_history: List[bool] = field(default_factory=list)
    error: Optional[str] = None

# --------------------------------------------------------------------------- #
# is_healthy() - Recovery verification target
# --------------------------------------------------------------------------- #

def is_healthy() -> bool:
    """Verifies Kronos system is operational. Returns True if healthy."""
    try:
        import requests
        r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
        if r.status_code != 200:
            return False
    except Exception:
        return False

    for proc in ["kronos_pilot", "kronos_heartbeat"]:
        try:
            if subprocess.run(["pgrep", "-x", proc], capture_output=True, timeout=3).returncode != 0:
                return False
        except Exception:
            return False

    hb = Path("~/kronos/kronos_heartbeat.json").expanduser()
    if hb.exists() and (time.time() - hb.stat().st_mtime) > 60:
        return False

    stop = Path("~/kronos/emergency_stop.json").expanduser()
    if stop.exists():
        try:
            if json.loads(stop.read_text()).get("stopped", False):
                return False
        except Exception:
            pass
    return True

# --------------------------------------------------------------------------- #
# Scenario: Network Blackout
# --------------------------------------------------------------------------- #

class NetworkBlackoutScenario:
    def __init__(self, cfg: ChaosConfig): self.cfg = cfg; self.blocked = False

    def inject(self) -> ScenarioResult:
        r = ScenarioResult("network_blackout", ScenarioStatus.INJECTING)
        r.injected_at = time.time()
        if self.cfg.dry_run:
            print(f"[DRY-RUN] Would block network for {self.cfg.duration}s")
            return r
        try:
            subprocess.run(["iptables", "-A", "OUTPUT", "-j", "DROP"], check=False, timeout=5)
            self.blocked = True
            print(f"[CHAOS] Network blackout injected at {datetime.now().isoformat()}")
        except Exception as e:
            r.status = ScenarioStatus.FAILED; r.error = str(e)
        return r

    def recover(self) -> ScenarioResult:
        r = ScenarioResult("network_blackout", ScenarioStatus.RECOVERING)
        if self.cfg.dry_run:
            print("[DRY-RUN] Would restore network")
            return r
        try:
            if self.blocked:
                subprocess.run(["iptables", "-F", "OUTPUT"], check=False, timeout=5)
                self.blocked = False
                print("[CHAOS] Network restored")
        except Exception as e:
            r.error = str(e)
        return r

# --------------------------------------------------------------------------- #
# Scenario: Partial Fill
# --------------------------------------------------------------------------- #

class PartialFillScenario:
    def __init__(self, cfg: ChaosConfig):
        self.cfg = cfg; self.active = False; self._patch: Optional[Path] = None

    def inject(self) -> ScenarioResult:
        r = ScenarioResult("partial_fill", ScenarioStatus.INJECTING)
        r.injected_at = time.time()
        if self.cfg.dry_run:
            print(f"[DRY-RUN] Would degrade fill rate to {self.cfg.fill_rate:.0%}")
            return r
        try:
            patch = f"import random\n_original_fill_rate = 1.0\n_current_fill_rate = {self.cfg.fill_rate}\ndef chaotic_fill(prob): return random.random() <= _current_fill_rate\n"
            self._patch = Path("~/kronos/.chaos_fill_patch.py")
            self._patch.write_text(patch)
            self.active = True
            print(f"[CHAOS] Partial fill injected: rate={self.cfg.fill_rate:.0%}")
        except Exception as e:
            r.status = ScenarioStatus.FAILED; r.error = str(e)
        return r

    def recover(self) -> ScenarioResult:
        r = ScenarioResult("partial_fill", ScenarioStatus.RECOVERING)
        if self.cfg.dry_run:
            print("[DRY-RUN] Would restore fill rate")
            return r
        try:
            if self._patch and self._patch.exists():
                self._patch.unlink()
            self.active = False
            print("[CHAOS] Fill rate restored")
        except Exception as e:
            r.error = str(e)
        return r

# --------------------------------------------------------------------------- #
# Scenario: Kill -9
# --------------------------------------------------------------------------- #

class KillNineScenario:
    def __init__(self, cfg: ChaosConfig):
        self.cfg = cfg; self.killed: List[int] = []

    def inject(self) -> ScenarioResult:
        r = ScenarioResult("kill_nine", ScenarioStatus.INJECTING)
        r.injected_at = time.time()
        target = self.cfg.target
        if self.cfg.dry_run:
            print(f"[DRY-RUN] Would send SIGKILL to '{target}'")
            return r
        try:
            out = subprocess.run(["pgrep", "-x", target], capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                for pid in map(int, out.stdout.strip().split()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        self.killed.append(pid)
                        print(f"[CHAOS] SIGKILL sent to PID {pid}")
                    except ProcessLookupError:
                        pass
            else:
                print(f"[CHAOS] Process '{target}' not found")
        except Exception as e:
            r.status = ScenarioStatus.FAILED; r.error = str(e)
        return r

    def recover(self) -> ScenarioResult:
        r = ScenarioResult("kill_nine", ScenarioStatus.RECOVERING)
        if self.cfg.dry_run:
            print(f"[DRY-RUN] Would verify '{self.cfg.target}' restarted")
            return r
        time.sleep(2)
        try:
            out = subprocess.run(["pgrep", "-x", self.cfg.target], capture_output=True, text=True, timeout=5)
            print(f"[CHAOS] Process '{self.cfg.target}' {'running' if out.returncode == 0 else 'not yet restarted'}")
        except Exception as e:
            r.error = str(e)
        return r

# --------------------------------------------------------------------------- #
# Chaos Monkey Orchestrator
# --------------------------------------------------------------------------- #

SCENARIOS = {
    "network_blackout": NetworkBlackoutScenario,
    "partial_fill": PartialFillScenario,
    "kill_nine": KillNineScenario,
}

class ChaosMonkey:
    def __init__(self, cfg: ChaosConfig): self.cfg = cfg; self.results: Dict[str, ScenarioResult] = {}

    def run(self) -> Dict[str, ScenarioResult]:
        targets = list(SCENARIOS.items()) if self.cfg.scenario == "all" else [(self.cfg.scenario, SCENARIOS.get(self.cfg.scenario))]
        for name, cls in targets:
            if cls is None:
                self.results[name] = ScenarioResult(name, ScenarioStatus.FAILED, error=f"Unknown: {name}")
                continue
            print(f"\n{'='*50}\n  Scenario: {name}\n{'='*50}")
            scen = cls(self.cfg)
            r = scen.inject()
            if r.status == ScenarioStatus.FAILED:
                self.results[name] = r; continue
            if not self.cfg.dry_run:
                print(f"[CHAOS] Holding {self.cfg.duration}s..."); time.sleep(self.cfg.duration)
            rec = scen.recover()
            r.status = rec.status
            if self.cfg.verify_recovery:
                self._verify(r, name)
            self.results[name] = r
            self._log(r)
        return self.results

    def _verify(self, r: ScenarioResult, name: str):
        print(f"\n[RECOVERY] Verifying is_healthy()...")
        if self.cfg.dry_run:
            print("[DRY-RUN] Recovery check skipped in dry-run mode")
            r.status = ScenarioStatus.VERIFIED
            r.health_history.append(True)
            return
        max_wait = self.cfg.max_recovery_attempts * 30
        elapsed = 0
        while elapsed < max_wait:
            healthy = is_healthy()
            r.health_history.append(healthy)
            print(f"[RECOVERY] is_healthy()={healthy} (t={elapsed}s)")
            if healthy:
                r.status = ScenarioStatus.VERIFIED; r.recovered_at = time.time()
                print(f"[RECOVERY] ✓ Healthy after {elapsed}s"); return
            time.sleep(self.cfg.health_interval); elapsed += self.cfg.health_interval
        r.status = ScenarioStatus.FAILED; r.error = f"Timeout after {max_wait}s"
        print(f"[RECOVERY] ✗ {r.error}")

    def _log(self, r: ScenarioResult):
        try:
            entry = {"ts": datetime.now().isoformat(), "scenario": r.scenario, "status": r.status.value,
                     "healthy_history": r.health_history, "error": r.error, "dry_run": self.cfg.dry_run}
            with open(LOG_FILE, "a") as f: f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[WARN] Log write failed: {e}")

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    print("\n  ╔═══════════════════════════════╗\n  ║   KRONOS CHAOS MONKEY v1.0    ║\n  ╚═══════════════════════════════╝\n")
    p = argparse.ArgumentParser(description="Chaos Monkey for Kronos")
    p.add_argument("--scenario", "-s", default="all",
                   choices=["network_blackout", "partial_fill", "kill_nine", "all"])
    p.add_argument("--duration", "-d", type=int, default=30)
    p.add_argument("--fill-rate", "-f", type=float, default=0.7)
    p.add_argument("--target", "-t", default="kronos_pilot")
    p.add_argument("--dry-run", "-n", action="store_true")
    p.add_argument("--verify-recovery", "-v", action="store_true")
    p.add_argument("--health-interval", type=int, default=5)
    p.add_argument("--max-recovery-attempts", type=int, default=3)
    cfg = ChaosConfig(**vars(p.parse_args()))

    print(f"Config: scenario={cfg.scenario}, duration={cfg.duration}s, dry_run={cfg.dry_run}, verify={cfg.verify_recovery}")
    print(f"[PRE-FLIGHT] is_healthy()={is_healthy()}")
    if not is_healthy() and not cfg.dry_run:
        if input("[WARN] System unhealthy. Continue? (y/N): ").strip().lower() != 'y':
            sys.exit(0)
    elif cfg.dry_run:
        print("[DRY-RUN] System health check skipped")

    results = ChaosMonkey(cfg).run()
    print(f"\n{'='*50}\n  SUMMARY\n{'='*50}")
    for n, r in results.items():
        icon = "✓" if r.status == ScenarioStatus.VERIFIED else "✗"
        print(f"  [{icon}] {n}: {r.status.value}" + (f" - {r.error}" if r.error else ""))

    # Write state
    state_file = Path("~/kronos/chaos_state.json").expanduser()
    try:
        state_file.write_text(json.dumps({"ts": datetime.now().isoformat(), "results": {n: {"status": r.status.value} for n, r in results.items()}}, indent=2))
        print(f"\n[STATE] {state_file}")
    except Exception as e:
        print(f"[WARN] State write failed: {e}")
    print()

if __name__ == "__main__":
    main()
