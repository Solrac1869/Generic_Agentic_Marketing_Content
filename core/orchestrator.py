#!/usr/bin/env python3
"""orchestrator.py, brand loader, budget enforcement, agent dispatch.

Every agent runs through here so that spend is capped, state is consistent,
and each run is logged with its cost. Brand-agnostic: the only brand-specific
input is brands/<id>/brand.yaml.

Usage:
  ./orchestrator.py --brand arp --agent research
  ./orchestrator.py --brand arp --agent strategy --dry-run
  ./orchestrator.py --brand arp --status
"""

import argparse, json, os, sys, datetime, pathlib, importlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
ENV_FILE = "/etc/marketing-agents.env"

# Rough USD per million tokens. Used for the budget ledger, not billing.
MODEL_COSTS = {
    "claude-opus-5":            {"in": 5.00,  "out": 25.00},
    "claude-sonnet-5":          {"in": 3.00,  "out": 15.00},
    "claude-haiku-4-5-20251001":{"in": 1.00,  "out": 5.00},
}


# ─── Config ────────────────────────────────────────────────────────

def load_env():
    """Load secrets from the root-owned env file, if present."""
    if os.path.exists(ENV_FILE):
        try:
            for line in open(ENV_FILE):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        except PermissionError:
            pass  # running unprivileged; key may come from the environment


def load_brand(brand_id):
    """The brand config, merged from both tiers into one dict.

    brand.yaml holds the invariants: the products, the pillars, the audience,
    the permitted claims, the banned language, and the bounds that govern the
    other file. Only a person changes those.

    expression.yaml holds how the brand is expressed this week: channel mix,
    cadence, formats, posting times, calls to action, budget split. strategy
    may change those week to week without asking, provided the result stays
    inside the bounds.

    The tiers differ in who may write them, not in how they are read, so this
    stays the single entry point and every agent sees the same shape it always
    saw. Expression cannot override an invariant: a key present in both is
    taken from brand.yaml and the collision is reported, because a silent
    override would let expression edit an invariant by restating it.
    """
    d = ROOT / "brands" / brand_id
    path = d / "brand.yaml"
    if not path.exists():
        sys.exit(f"No brand config at {path}")
    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml required: pip3 install pyyaml --break-system-packages")

    cfg = yaml.safe_load(path.read_text()) or {}
    expr_path = d / "expression.yaml"
    if expr_path.exists():
        expr = yaml.safe_load(expr_path.read_text()) or {}
        clash = sorted(set(expr) & set(cfg))
        if clash:
            print(f"  WARNING: expression.yaml restates invariant key(s) "
                  f"{clash}, the value in brand.yaml is used")
        for k, v in expr.items():
            cfg.setdefault(k, v)
        cfg["_expression_keys"] = sorted(expr)

    cfg["_dir"] = path.parent
    cfg["_id"] = brand_id
    return cfg


# ─── Budget ledger ─────────────────────────────────────────────────

class Budget:
    """Hard daily spend cap. Agents check before calling, record after."""

    def __init__(self, brand_id, cap_usd):
        self.brand_id = brand_id
        self.cap = float(cap_usd)
        STATE.mkdir(exist_ok=True)
        self.path = STATE / f"budget-{brand_id}.json"
        self.today = datetime.date.today().isoformat()
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            d = json.loads(self.path.read_text())
            if d.get("date") == self.today:
                return d
            # The day has rolled over and this file is about to be replaced.
            # Everything spent yesterday lived only here, so it was lost every
            # midnight and no question about cost over time could be answered.
            self._archive(d)
        return {"date": self.today, "spent_usd": 0.0, "runs": []}

    def _archive(self, day):
        """Append a closed day to the persistent ledger. Never raises.

        Writing the ledger must not be able to stop an agent running, so a
        failure here is swallowed. A missing day in a cost report is a smaller
        problem than a marketing system that will not start.
        """
        try:
            date = day.get("date")
            if not date:
                return
            path = STATE / f"budget-ledger-{self.brand_id}.json"
            ledger = {"days": {}}
            if path.exists():
                try:
                    ledger = json.loads(path.read_text())
                except ValueError:
                    ledger = {"days": {}}
            days = ledger.setdefault("days", {})
            if date in days:
                return                      # already archived, do not double count
            by_agent = {}
            for r in day.get("runs", []):
                # produce:blog and produce:x are one agent for cost purposes.
                agent = str(r.get("agent", "unknown")).split(":", 1)[0]
                slot = by_agent.setdefault(agent, {"cost_usd": 0.0, "runs": 0,
                                                   "in": 0, "out": 0})
                slot["cost_usd"] = round(slot["cost_usd"] + float(r.get("cost_usd") or 0), 4)
                slot["runs"] += 1
                slot["in"] += int(r.get("in") or 0)
                slot["out"] += int(r.get("out") or 0)
            days[date] = {"spent_usd": round(float(day.get("spent_usd") or 0), 4),
                          "runs": len(day.get("runs", [])),
                          "by_agent": by_agent}
            path.write_text(json.dumps(ledger, indent=2))
        except Exception:
            pass

    def _save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    @property
    def spent(self):
        return round(self.data["spent_usd"], 4)

    @property
    def remaining(self):
        return round(max(0.0, self.cap - self.spent), 4)

    def check(self, need=0.0):
        """Raise if the cap is already reached."""
        if self.spent >= self.cap:
            raise BudgetExceeded(
                f"daily cap ${self.cap:.2f} reached (spent ${self.spent:.2f}), halting")
        return True

    def record(self, agent, model, in_tokens, out_tokens):
        rate = MODEL_COSTS.get(model, {"in": 3.0, "out": 15.0})
        cost = (in_tokens / 1e6) * rate["in"] + (out_tokens / 1e6) * rate["out"]
        self.data["spent_usd"] += cost
        self.data["runs"].append({
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
            "agent": agent, "model": model,
            "in": in_tokens, "out": out_tokens, "cost_usd": round(cost, 4),
        })
        self._save()
        return round(cost, 4)


class BudgetExceeded(RuntimeError):
    pass


# ─── Dispatch ──────────────────────────────────────────────────────

AGENTS = ["research", "strategy", "produce", "publish", "analyse", "site", "blog", "engage", "verify", "video", "seo", "status", "report", "refresh", "critic", "review", "crm", "media"]


def run_agent(name, brand, budget, dry_run=False, from_raw=False, only_channel=None, mode=None):
    if name not in AGENTS:
        sys.exit(f"Unknown agent '{name}'. Available: {', '.join(AGENTS)}")
    try:
        mod = importlib.import_module(f"agents.{name}")
    except ModuleNotFoundError:
        sys.exit(f"agents/{name}.py not implemented yet")
    if not hasattr(mod, "run"):
        sys.exit(f"agents/{name}.py has no run(brand, budget, dry_run)")
    import inspect
    kw = {}
    params = inspect.signature(mod.run).parameters
    if from_raw and "from_raw" in params:
        kw["from_raw"] = True
    if only_channel and "only_channel" in params:
        kw["only_channel"] = only_channel
    if mode and "mode" in params:
        kw["mode"] = mode
    return mod.run(brand, budget, dry_run=dry_run, **kw)


def status(brand, budget):
    bid = brand["_id"]
    print(f"=== {brand.get('name', bid)} ===")
    print(f"budget:    ${budget.spent:.2f} spent / ${budget.cap:.2f} cap "
          f"(${budget.remaining:.2f} left today)")
    print(f"runs today: {len(budget.data['runs'])}")
    d = brand["_dir"]
    for sub in ("research", "briefs", "outputs", "analytics"):
        files = sorted((d / sub).glob("*")) if (d / sub).exists() else []
        latest = files[-1].name if files else ", "
        print(f"{sub+':':11} {len(files)} files, latest: {latest}")
    key = "set" if os.environ.get("ANTHROPIC_API_KEY") else "MISSING"
    print(f"api key:   {key}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="arp")
    ap.add_argument("--agent", help=f"one of: {', '.join(AGENTS)}")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--channel", help="produce: draft only this channel")
    ap.add_argument("--mode", default="notify",
                    choices=["notify", "ship", "discover", "follow", "digest", "scout", "review", "watch", "ask", "listen", "full", "propose", "apply"],
                    help="publish: notify sends the nudge, ship posts after the veto window")
    ap.add_argument("--from-raw", action="store_true",
                    help="replay the last saved model reply, no API call, no cost")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    load_env()
    brand = load_brand(args.brand)
    budget = Budget(args.brand, brand.get("budget", {}).get("daily_usd_cap", 5.00))

    if args.status or not args.agent:
        status(brand, budget)
        return

    try:
        budget.check()
    except BudgetExceeded as e:
        print(f"HALTED: {e}")
        sys.exit(2)

    result = run_agent(args.agent, brand, budget, dry_run=args.dry_run,
                       from_raw=args.from_raw, only_channel=args.channel,
                       mode=args.mode)
    print(f"\n{args.agent}: done | spent today ${budget.spent:.2f} / ${budget.cap:.2f}")
    if result:
        print(result if isinstance(result, str) else json.dumps(result, indent=2)[:800])
        # An agent that returns a string starting with FAILED has not done its
        # job. Without this it printed the message and exited 0, so the runner
        # saw success and nobody was told.
        if isinstance(result, str) and result.startswith("FAILED"):
            sys.exit(1)


if __name__ == "__main__":
    main()
