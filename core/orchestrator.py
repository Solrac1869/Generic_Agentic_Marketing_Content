#!/usr/bin/env python3
"""orchestrator.py, brand loader, budget enforcement, agent dispatch.

Every agent runs through here so that spend is capped, state is consistent,
and each run is logged with its cost. Brand-agnostic: the only brand-specific
input is brands/<id>/brand.yaml.

Usage:
  ./orchestrator.py --brand <id> --agent research
  ./orchestrator.py --brand <id> --agent strategy --dry-run
  ./orchestrator.py --brand <id> --status
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


def default_brand_id():
    """The brand to act on when the caller did not name one.

    Nine call sites across three agents and four scripts used this before it
    existed: the de-branding pass replaced an inlined brand id with a call and
    never wrote the function. It imports fine and raises AttributeError the
    first time blog, refresh or publish actually runs, which is the worst
    possible moment to find out.

    BRAND_ID wins if set. Otherwise, if exactly one brand is configured that
    is obviously the one meant. More than one is ambiguous and says so rather
    than picking alphabetically, because publishing one brand's plan under
    another brand's name is not a failure anyone notices quickly.
    """
    import os
    env = os.environ.get("BRAND_ID", "").strip()
    if env:
        return env
    base = ROOT / "brands"
    ids = sorted(p.name for p in base.iterdir()
                 if p.is_dir() and (p / "brand.yaml").exists()) \
        if base.exists() else []
    if len(ids) == 1:
        return ids[0]
    if not ids:
        # The single-brand layout. load_brand resolves this to config/.
        if (ROOT / "config" / "brand.yaml").exists():
            return "default"
        raise RuntimeError(
            "No brand is configured. Copy config/brand.example.yaml to "
            "config/brand.yaml, then run: python3 setup.py")
    raise RuntimeError(
        "%d brands are configured (%s). Set BRAND_ID to choose one."
        % (len(ids), ", ".join(ids)))


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
    # A single-brand install configures config/, which is where setup.py writes
    # and checks. A multi-brand install uses brands/<id>/. Both were supported
    # by different halves of this system and neither knew about the other, so a
    # completed setup left every agent with nothing to load.
    if not path.exists() and (ROOT / "config" / "brand.yaml").exists():
        d = ROOT / "config"
        path = d / "brand.yaml"
    if not path.exists():
        sys.exit(
            f"No brand config at {path}. Copy config/brand.example.yaml to "
            f"config/brand.yaml and run: python3 setup.py")
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

AGENTS = ["research", "strategy", "produce", "publish", "analyse", "site", "blog", "engage", "verify", "video", "seo", "status", "report", "refresh", "critic", "review", "crm", "media", "remedy"]


_MODEL_CALLERS = None


def _model_callers():
    """Agents that make a model call, read once from their own source.

    A list kept by hand goes stale the moment an agent gains or loses its
    llm.call, and the failure is silent in both directions: a new model-caller
    burns money against a dead account, or a mechanical agent is stopped by a
    fault that cannot affect it.
    """
    global _MODEL_CALLERS
    if _MODEL_CALLERS is None:
        import ast as _ast
        found = set()
        for f in (ROOT / "agents").glob("*.py"):
            # Parsed, not grepped. A substring search for "llm.call" misses
            # `from core.llm import call` used as a bare call(), and an agent
            # written that way would keep spending against a dead account --
            # the exact failure this exists to prevent. bin/conformance.py
            # already learned this lesson; this had not.
            try:
                src = f.read_text(errors="replace")
                tree = _ast.parse(src)
            except Exception:
                found.add(f.stem)   # unreadable or unparsable: assume it calls
                continue
            direct = {a.asname or a.name
                      for n in _ast.walk(tree) if isinstance(n, _ast.ImportFrom)
                      and (n.module or "").endswith("llm") for a in n.names}
            for n in _ast.walk(tree):
                if not isinstance(n, _ast.Call):
                    continue
                fn = n.func
                if isinstance(fn, _ast.Attribute) and fn.attr == "call":
                    found.add(f.stem); break
                if isinstance(fn, _ast.Name) and fn.id in direct:
                    found.add(f.stem); break
        _MODEL_CALLERS = found
    return _MODEL_CALLERS


def _breaker_blocks(agent):
    """True when the account itself is broken, so starting is pointless.

    Cron has no memory. Without this, every agent scheduled after an account
    fault runs, reaches the API, is refused, and exits 1 -- turning one
    fixable problem into a stream of identical alerts that bury it.

    verify and status are exempt: they are how the fault gets reported, and
    silencing the reporter is how an outage becomes invisible.
    """
    # Only agents that actually call the model. The first version listed
    # verify, status and report by hand, reasoning about who reports the
    # fault and forgetting who does not need the API at all. So a dead key
    # also stopped publish and crm, neither of which makes a single model
    # call: nothing published for sixteen hours and the contact ingest went
    # stale, while every run exited 0 and the breaker logged that it was
    # working as designed.
    #
    # Derived from the source rather than maintained as a list, because the
    # list was the bug.
    if agent not in _model_callers():
        return False
    from core import llm
    tripped, reason = llm.breaker_state()
    if not tripped:
        return False
    print("  skipped: the API account is not usable, so this cannot succeed.")
    print("  %s" % reason)
    print("  Any successful call clears this automatically.")
    return True


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
    if _breaker_blocks(name):
        return "skipped: API account unusable"
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
    ap.add_argument("--brand", default=None)
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
