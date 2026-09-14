#!/usr/bin/env python3
"""llm.py, the single path to the Anthropic API.

Every agent calls through here so that token spend is always recorded against
the day's budget. Standard library only, so there is nothing to install on the
host beyond Python itself.

Server-side web search is available via call(..., web_search=True), which gives
the research agent real, citable sources rather than model recall.
"""

import datetime, json, os, pathlib, time, urllib.request, urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
RETRY_STATUSES = {429, 500, 502, 503, 529}


class LLMError(RuntimeError):
    pass


def _post(payload, api_key, timeout=600):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


#: HTTP statuses that are a statement about the request, not the weather.
#: On 13 September strategy hit "credit balance too low" and cron retried it
#: every fifteen minutes for six attempts. Each was a paid call that could not
#: have succeeded. 429 is excluded on purpose: rate limiting is transient and
#: is the one 4xx worth waiting out.
NON_RETRIABLE = (400, 401, 403, 404, 413, 422)

#: A breaker file, written when the account itself is the problem.
#:
#: llm.call already refused to retry a 400, but nothing stopped cron from
#: starting the agent again fifteen minutes later. On 13 September strategy ran
#: six times against an empty credit balance, each run reaching the API, being
#: told the balance was too low, and exiting 1. Six identical failures, six
#: alerts, and every one of them knowable from the first.
#:
#: Account-level faults are the only ones worth a breaker: no amount of waiting
#: fixes them and no other agent will fare better, so the right behaviour is to
#: stop the whole system until a person adds credit or fixes the key. Anything
#: narrower -- a bad prompt, one oversized request -- must not stop other work.
BREAKER = pathlib.Path(__file__).resolve().parent.parent / "state" / ".breaker"
ACCOUNT_FAULT = ("credit balance", "billing", "invalid x-api-key",
                 "authentication_error", "permission_error")


def breaker_state():
    """(tripped, reason). Reads as data so callers can report it."""
    try:
        d = json.loads(BREAKER.read_text())
        return True, d.get("reason", "")
    except Exception:
        return False, ""


def _trip(code, body):
    low = body.lower()
    if not any(w in low for w in ACCOUNT_FAULT):
        return
    try:
        BREAKER.parent.mkdir(parents=True, exist_ok=True)
        BREAKER.write_text(json.dumps({
            "reason": "HTTP %s: %s" % (code, body[:200]),
            "tripped": datetime.datetime.now(datetime.timezone.utc).isoformat()}))
    except Exception:
        pass


def _clear():
    """Any successful call means the account is fine again."""
    try:
        BREAKER.unlink()
    except Exception:
        pass


def call(prompt, *, model, budget=None, agent="unknown", system=None,
         max_tokens=2000, web_search=False, max_searches=5, retries=3,
         timeout=600, thinking=None):
    """Send one message. Returns (text, citations, usage).

    budget: a core.orchestrator.Budget. Checked before the call and credited
            after it, so an agent cannot spend past the daily cap.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY not set (expected in /etc/marketing-agents.env)")

    if budget is not None:
        budget.check()   # raises BudgetExceeded

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        # Sent as a cacheable block rather than a bare string. The system
        # prompt now carries the craft skills, which are tens of kilobytes and
        # byte-identical across every call an agent makes for a week. Paying
        # full input price for that on each draft would be the whole cost of
        # the feature; marked this way the first call pays and the rest read
        # from cache at a tenth. A short system prompt falls under the minimum
        # cacheable length and the marker is simply ignored, so this is safe
        # for every existing caller.
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
    # Extended thinking can consume the whole max_tokens budget before any text
    # is produced, which silently returned empty drafts. Explicit beats default.
    if thinking is False:
        payload["thinking"] = {"type": "disabled"}
    elif isinstance(thinking, int):
        # Claude 5 models reject {"type": "enabled", "budget_tokens": N} and
        # want adaptive thinking with an effort band instead. Translate rather
        # than making every caller know which family it is talking to.
        if "-5" in model:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {
                "effort": "low" if thinking <= 4000
                else "medium" if thinking <= 12000 else "high"}
        else:
            payload["thinking"] = {"type": "enabled", "budget_tokens": thinking}
    if web_search:
        payload["tools"] = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_searches,
        }]

    last_err = None
    for attempt in range(retries):
        try:
            data = _post(payload, api_key, timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code in RETRY_STATUSES and attempt < retries - 1:
                time.sleep(2 ** attempt)
                last_err = f"{e.code}: {body}"
                continue
            if e.code in NON_RETRIABLE:
                _trip(e.code, body)
                raise LLMError(f"HTTP {e.code} (permanent, not retried): {body}")
            raise LLMError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                last_err = str(e)
                continue
            raise LLMError(f"network: {e}")
    else:
        raise LLMError(f"exhausted retries: {last_err}")

    # Collect text blocks and any web-search citations.
    text_parts, citations = [], []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
            for c in block.get("citations", []) or []:
                url = c.get("url")
                if url and url not in [x["url"] for x in citations]:
                    citations.append({"url": url, "title": c.get("title", "")})

    if os.environ.get("LLM_DEBUG_DUMP"):
        pathlib.Path(os.environ["LLM_DEBUG_DUMP"]).write_text(json.dumps(data, indent=2)[:20000])

    if not text_parts and data.get("content"):
        kinds = {b.get("type") for b in data["content"]}
        print(f"  WARNING: no text block returned (blocks: {kinds}, "
              f"stop_reason={data.get('stop_reason')})")

    _clear()
    usage = data.get("usage", {})
    # Cached input is reported in its own fields, not folded into
    # input_tokens. Counting only input_tokens once caching is on would make
    # the daily budget under-count every call, and a budget that under-counts
    # is worse than none: it stops enforcing at exactly the point spend rises.
    # Priced at the API's own rates: a cache write costs 1.25x the base input
    # rate, a cache read 0.1x, so both are converted to their base-rate
    # equivalent and Budget.record needs no knowledge of caching.
    cache_write = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    in_tok = usage.get("input_tokens", 0)
    billable_in = int(in_tok + cache_write * 1.25 + cache_read * 0.1)
    out_tok = usage.get("output_tokens", 0)
    cost = budget.record(agent, model, billable_in, out_tok) if budget is not None else 0.0

    return "\n".join(text_parts).strip(), citations, {
        "in": in_tok, "cache_write": cache_write, "cache_read": cache_read,
        "out": out_tok, "cost_usd": cost,
        "searches": usage.get("server_tool_use", {}).get("web_search_requests", 0),
        "stop_reason": data.get("stop_reason"),
        "block_types": [b.get("type") for b in data.get("content", [])],
    }


def extract_json(text):
    """Pull the most complete JSON object or array out of a model reply.

    Models sometimes emit a first partial attempt, reconsider, and emit a
    fuller one after it. Taking the FIRST valid object silently discarded six
    of seven drafts, so take the richest one instead.
    """
    dec = json.JSONDecoder()
    best, best_span = None, -1
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, end = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if not isinstance(obj, (dict, list)):
            continue
        # Score by how much text the object spans, so the outermost wins.
        #
        # The previous version scored on the longest list inside the object.
        # That made a nested item beat its own container: a wrapper holding
        # three assessments scored 3, while one assessment listing seven
        # domains scored 7, so the parser returned a single item and the rest
        # were silently dropped. Every agent using a wrapped result was
        # affected. Span cannot invert that way, because a container is always
        # longer than the things inside it, and it still prefers a model's
        # second fuller attempt over a truncated first one.
        if end > best_span:
            best, best_span = obj, end
    return best
