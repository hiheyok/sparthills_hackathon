"""Amazon Bedrock layer for SupplyLine.

Four AI jobs, all through the Bedrock Converse API:

  understand    free text from a stressed person -> structured supply request,
                including things they did not say but obviously need
  alternatives  for items nobody nearby has: choose SAFE substitutes from items
                that are confirmed in stock nearby, with cautions
  brief         explain the computed route in calm, plain language
  followup      answer questions about the plan

Design rules (these are what make it an AI *application* rather than a chat box):

  * The model never invents inventory. It may only pick item ids that exist in
    the catalog, and substitutes only from the in-stock candidate pool. Every
    id is validated; engine.py re-checks real stock afterwards.
  * Safety policy is enforced in code, not left to the prompt: items marked
    policy "none" (prescriptions, medical oxygen) can never be substituted, and
    a keyword safety net can raise the 911 banner even if the model misses it.
  * Resilience: a chain of models (Claude Haiku 4.5 -> Nova 2 Lite -> in-region
    Nova) with one JSON-repair retry per model, and a rules-based fallback so
    the app still works when Bedrock is throttled or unreachable. Every stage
    returns a trace saying which model answered, how long it took and how many
    tokens it used; the UI shows it.
"""
import json
import logging
import os
import re
import time

from engine import CATALOG, CATALOG_LIST

log = logging.getLogger("supplyline.ai")

DEFAULT_MODELS = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",   # best JSON discipline; cross-region profile
    "us.amazon.nova-2-lite-v1:0",                    # Amazon Nova 2 Lite via US profile
    "amazon.nova-2-lite-v1:0",                       # in-region (works if cross-region is blocked by SCP)
    "amazon.nova-lite-v1:0",                         # last-resort in-region model
]

_client = None
_bad_until = {}          # model id -> unix time when we may try it again
_creds_bad_until = 0.0   # no credentials: skip Bedrock entirely for a short while


# ------------------------------------------------------------------ config
def ai_mode():
    return (os.environ.get("AI_MODE") or "auto").strip().lower()


def model_chain():
    raw = os.environ.get("BEDROCK_MODEL_IDS", "")
    ids = [m.strip() for m in raw.split(",") if m.strip()]
    return ids or list(DEFAULT_MODELS)


def region():
    return (os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")


def status():
    return {"mode": ai_mode(), "region": region(), "models": model_chain()}


def set_client(client):
    """Test hook: inject a fake bedrock-runtime client and reset health memory."""
    global _client, _creds_bad_until
    _client = client
    _creds_bad_until = 0.0
    _bad_until.clear()


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "bedrock-runtime", region_name=region(),
            config=Config(connect_timeout=3, read_timeout=20, retries={"max_attempts": 1, "mode": "standard"}))
    return _client


# ------------------------------------------------------------ small helpers
def _clip(v, n):
    return str(v if v is not None else "").strip()[:n]


def _b(v):
    return v is True or str(v).strip().lower() == "true"


def _short(exc):
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def extract_json(text):
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no JSON object found in the reply")
    return json.loads(t[i:j + 1])


def _classify(exc):
    name = type(exc).__name__
    resp = getattr(exc, "response", None)
    code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    if isinstance(exc, ImportError) or name in ("NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError"):
        return "no_creds"
    if code in ("ExpiredTokenException", "UnrecognizedClientException", "InvalidSignatureException"):
        return "no_creds"
    if code in ("AccessDeniedException", "ResourceNotFoundException", "ValidationException"):
        return "model_unavailable"
    if (code in ("ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException",
                 "ModelNotReadyException", "InternalServerException", "ModelErrorException",
                 "ServiceQuotaExceededException")
            or name in ("ReadTimeoutError", "ConnectTimeoutError", "EndpointConnectionError", "ConnectionClosedError")):
        return "transient"
    return "other"


class AIUnavailable(Exception):
    def __init__(self, tried):
        super().__init__("; ".join(f"{t.get('model')}: {t.get('error')}" for t in tried) or "AI unavailable")
        self.tried = tried


def _text_of(resp):
    blocks = (resp.get("output", {}).get("message", {}) or {}).get("content", []) or []
    return "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)


def _converse(model, system, messages, max_tokens, temperature):
    kw = {
        "modelId": model,
        "system": [{"text": system}],
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    gid = os.environ.get("GUARDRAIL_ID")
    if gid:  # optional Amazon Bedrock Guardrails
        kw["guardrailConfig"] = {"guardrailIdentifier": gid,
                                 "guardrailVersion": os.environ.get("GUARDRAIL_VERSION", "DRAFT")}
    return _get_client().converse(**kw)


def call_model(stage, system, user, parse, max_tokens=1600, temperature=0.2, budget_s=24.0):
    """Try each model in the chain. Per model: one call, plus one repair retry if
    the reply fails `parse` (which must raise ValueError on bad output).
    Returns (parsed, trace) or raises AIUnavailable."""
    global _creds_bad_until
    t0 = time.monotonic()
    tried = []
    if ai_mode() == "off":
        raise AIUnavailable([{"model": None, "error": "AI_MODE=off (rules only)"}])
    if time.time() < _creds_bad_until:
        raise AIUnavailable([{"model": None, "error": "no usable AWS credentials (cached)"}])
    tok_in = tok_out = calls = 0

    for model in model_chain():
        if time.time() < _bad_until.get(model, 0):
            tried.append({"model": model, "error": "skipped: recently unavailable"})
            continue
        messages = [{"role": "user", "content": [{"text": user}]}]
        for attempt in (1, 2):
            if time.monotonic() - t0 > budget_s:
                tried.append({"model": model, "error": "time budget used up"})
                break
            try:
                calls += 1
                resp = _converse(model, system, messages, max_tokens, temperature)
            except Exception as exc:  # noqa: BLE001 - classify every SDK/network failure
                kind = _classify(exc)
                tried.append({"model": model, "error": _short(exc), "kind": kind})
                if kind == "no_creds":
                    _creds_bad_until = time.time() + 60
                    raise AIUnavailable(tried)
                _bad_until[model] = time.time() + (900 if kind == "model_unavailable" else 20)
                break
            usage = resp.get("usage", {}) or {}
            tok_in += int(usage.get("inputTokens", 0) or 0)
            tok_out += int(usage.get("outputTokens", 0) or 0)
            text = _text_of(resp)
            try:
                parsed = parse(text)
            except (ValueError, KeyError, TypeError) as exc:
                tried.append({"model": model, "error": "invalid output: " + _short(exc), "kind": "invalid"})
                if attempt == 1:
                    messages = messages + [
                        {"role": "assistant", "content": [{"text": text.strip() or "{}"}]},
                        {"role": "user", "content": [{"text": f"That reply was not usable ({exc}). "
                                                              "Return ONLY the corrected JSON object."}]},
                    ]
                    continue
                break
            trace = {"stage": stage, "mode": "bedrock", "model": model, "region": region(),
                     "ms": int((time.monotonic() - t0) * 1000), "calls": calls,
                     "tokens_in": tok_in, "tokens_out": tok_out,
                     "fell_back_from": [t["model"] for t in tried if t.get("model") and t["model"] != model]}
            return parsed, trace
    raise AIUnavailable(tried)


def _fallback_trace(stage, exc, t0):
    tried = getattr(exc, "tried", [])
    reason = tried[-1]["error"] if tried else str(exc)
    return {"stage": stage, "mode": "rules", "model": None, "ms": int((time.monotonic() - t0) * 1000),
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "reason": reason, "tried": tried[-6:]}


# -------------------------------------------------------------- sanitizing
ALLOWED_DISASTERS = {"wildfire", "earthquake", "flood", "storm", "hurricane", "winter_storm", "heat",
                     "power_outage", "outage", "supply_shortage", "other", "unknown"}


def clean_needs(raw):
    """Validate needs (from the model or from the client) against the catalog."""
    needs, seen, unmapped = [], set(), []
    for n in raw or []:
        if not isinstance(n, dict):
            continue
        iid = n.get("item_id")
        if not isinstance(iid, str) or iid not in CATALOG:
            lbl = n.get("label") or n.get("name") or (iid if isinstance(iid, str) else "")
            if lbl:
                unmapped.append(_clip(lbl, 80))
            continue
        if iid in seen:
            continue
        seen.add(iid)
        try:
            pr = int(n.get("priority", 2))
        except (TypeError, ValueError):
            pr = 2
        try:
            qty = max(1, min(99, int(n.get("qty", 1))))
        except (TypeError, ValueError):
            qty = 1
        needs.append({"item_id": iid, "label": _clip(n.get("label") or CATALOG[iid]["name"], 60),
                      "qty": qty, "priority": pr if pr in (1, 2, 3) else 2,
                      "inferred": _b(n.get("inferred")), "why": _clip(n.get("why"), 160)})
    needs.sort(key=lambda n: n["priority"])
    return needs[:12], unmapped[:8]


def clean_intent(d):
    d = d if isinstance(d, dict) else {}
    needs, unmapped = clean_needs(d.get("needs"))
    unmapped = (unmapped + [_clip(x, 80) for x in (d.get("unmapped") or []) if isinstance(x, str)])[:8]
    dis = d.get("disaster") if isinstance(d.get("disaster"), dict) else {}
    dtype = dis.get("type") if dis.get("type") in ALLOWED_DISASTERS else "unknown"
    hh = d.get("household") if isinstance(d.get("household"), dict) else {}
    try:
        people = int(hh.get("people")) if hh.get("people") is not None else None
        people = people if people is None or 1 <= people <= 99 else None
    except (TypeError, ValueError):
        people = None
    us = d.get("urgent_safety") if isinstance(d.get("urgent_safety"), dict) else {}
    return {
        "disaster": {"type": dtype, "summary": _clip(dis.get("summary"), 200)},
        "household": {"people": people, "infants": _b(hh.get("infants")), "elderly": _b(hh.get("elderly")),
                      "pets": _b(hh.get("pets")),
                      "medical": [_clip(x, 60) for x in (hh.get("medical") or []) if isinstance(x, str)][:5]},
        "urgent_safety": {"flag": _b(us.get("flag")), "reason": _clip(us.get("reason"), 240)},
        "needs": needs, "unmapped": unmapped,
    }


URGENT_PATTERNS = [
    (r"can'?t breathe|cannot breathe|trouble breathing|struggling to breathe|not breathing|stopped breathing",
     "Trouble breathing is a medical emergency."),
    (r"unconscious|unresponsive|passed out|seizure|having a stroke|chest pain|heart attack|overdos",
     "This sounds like a medical emergency."),
    (r"bleeding (heavily|badly|a lot)|won'?t stop bleeding|severe bleeding",
     "Heavy bleeding is a medical emergency."),
    (r"smell(s|ing)? (of )?gas|gas (leak|smell|odor)",
     "A gas smell can mean a leak. Leave the building, do not flip switches or use anything electrical inside, "
     "and call 911 or the gas utility from outside."),
    (r"\btrapped\b|stuck under|\bpinned\b", "Being trapped is an emergency."),
    (r"house (is )?on fire|flames|fire (is )?(next door|nearby|close|at my)|fire at my",
     "A fire close to you is an emergency."),
    (r"carbon monoxide|co alarm|co detector", "Carbon monoxide is dangerous. Get outside into fresh air."),
    (r"suicid|kill myself|want to die|end my life",
     "If you are thinking about harming yourself, call or text 988 (U.S.) now."),
]


def merge_safety(text, model_flag):
    flag = dict(model_flag or {"flag": False, "reason": ""})
    low = (text or "").lower()
    for pat, reason in URGENT_PATTERNS:
        if re.search(pat, low):
            if not flag.get("flag"):
                flag = {"flag": True, "reason": reason}
            elif reason not in flag.get("reason", "") and len(flag.get("reason", "")) < 200:
                flag["reason"] = (flag.get("reason", "") + " " + reason).strip()
            flag["source"] = "keyword+model" if model_flag and model_flag.get("flag") else "keyword"
            break
    return flag


# ------------------------------------------------------------ 1. understand
UNDERSTAND_SYSTEM = """You are the intake analyst for SupplyLine, a tool people use during disasters and supply disruptions to find nearby stores that have what they need.

Read the person's message and convert it into a structured supply request. The message may be stressed, informal, misspelled, or partly in another language. Read for meaning.

RULES
1. Use ONLY item_id values from the CATALOG below. Never invent ids. Anything requested that has no catalog match goes in "unmapped" as plain words.
2. Include what they ask for AND what they obviously need to make it usable. Mark those extras "inferred": true with a short "why". Examples: insulin that must stay cold during a power outage -> insulated cooler + bagged ice; cans of food with no mention of an opener -> manual can opener; phones to charge with no power -> USB power bank; wildfire smoke -> N95 masks. Be selective: at most 12 needs in total, no padding.
3. priority: 1 = health or life depends on it (medicine, medical devices, infant feeding, drinking water); 2 = needed within a day (food, light, warmth, charging, sanitation); 3 = nice to have.
4. qty: a whole number of units to look for. Use household size and duration when the person gives them (default 1).
5. Do not give medical advice. Set urgent_safety.flag to true only if someone seems to be in immediate danger right now (trouble breathing, severe bleeding, gas smell, trapped, fire close by, carbon monoxide). Put the reason in plain words.
6. Respond with ONE JSON object and nothing else. No markdown fences.

JSON SHAPE
{
  "disaster": {"type": "wildfire|earthquake|flood|storm|hurricane|winter_storm|heat|power_outage|outage|supply_shortage|other|unknown", "summary": "one short sentence"},
  "household": {"people": number or null, "infants": true or false, "elderly": true or false, "pets": true or false, "medical": ["short phrases"]},
  "urgent_safety": {"flag": true or false, "reason": "plain words, or empty"},
  "needs": [{"item_id": "...", "label": "the name the person would use", "qty": 1, "priority": 1, "inferred": false, "why": "short reason"}],
  "unmapped": ["..."]
}

CATALOG (item_id | name | what it is for)
{{CATALOG}}"""

_catalog_block = None


def catalog_block():
    global _catalog_block
    if _catalog_block is None:
        _catalog_block = "\n".join(f"{c['id']} | {c['name']} | {', '.join(c['tags'])}" for c in CATALOG_LIST)
    return _catalog_block


def understand(text):
    """-> (intent, trace)"""
    t0 = time.monotonic()
    system = UNDERSTAND_SYSTEM.replace("{{CATALOG}}", catalog_block())
    user = 'Message from the person:\n"""\n' + text + '\n"""'

    def parse(raw):
        intent = clean_intent(extract_json(raw))
        if not intent["needs"]:
            raise ValueError("needs is empty or contains no valid catalog item_id")
        return intent

    try:
        intent, trace = call_model("understand", system, user, parse, max_tokens=1500)
    except AIUnavailable as exc:
        intent = fallback_understand(text)
        trace = _fallback_trace("understand", exc, t0)
    intent["urgent_safety"] = merge_safety(text, intent["urgent_safety"])
    return intent, trace


_PRIORITY_ONE = {"Medical"}
_PRIORITY_ONE_ITEMS = {"water_bottled", "baby_formula_powder", "baby_formula_rtf", "insulin", "rx_medication",
                       "oxygen_cylinder", "water_filter", "water_tabs"}


def fallback_understand(text):
    """Keyword matching over catalog aliases. Much weaker than the model: it
    cannot read context, quantities or intent. It exists so the app degrades
    instead of failing."""
    low = " " + re.sub(r"[^a-z0-9' -]+", " ", text.lower()) + " "
    entries = sorted(((a, c["id"]) for c in CATALOG_LIST for a in c["aliases"]), key=lambda e: -len(e[0]))
    found = {}
    for alias, iid in entries:
        m = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?:s|es)?(?![a-z0-9])", low)
        if m:
            found.setdefault(iid, alias)
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
    needs = []

    def add(iid, inferred=False, why=""):
        if iid in CATALOG and not any(n["item_id"] == iid for n in needs):
            pr = 1 if (CATALOG[iid]["category"] in _PRIORITY_ONE or iid in _PRIORITY_ONE_ITEMS) else 2
            needs.append({"item_id": iid, "label": CATALOG[iid]["name"], "qty": 1, "priority": pr,
                          "inferred": inferred, "why": why})

    for iid in found:
        add(iid)
    if "insulin" in found:
        add("cooler", True, "Insulin needs to stay cool when the power is out.")
        add("ice_bag", True, "Ice keeps the cooler cold.")
    if "food_canned" in found:
        add("can_opener", True, "Cans need an opener if the power is out.")
    orig = text.lower()
    if re.search(r"wildfire|smoke|\bfire\b", orig):
        add("n95_mask", True, "Masks help with wildfire smoke.")
    dtype = "unknown"
    for pat, d in [(r"wildfire|smoke|\bfire\b", "wildfire"), (r"earthquake|quake", "earthquake"),
                   (r"flood", "flood"), (r"hurricane", "hurricane"), (r"storm|tornado", "storm"),
                   (r"snow|freez|winter|ice storm", "winter_storm"), (r"heat wave|heatwave", "heat"),
                   (r"power (is )?out|outage|blackout|shut ?off|no power", "power_outage")]:
        if re.search(pat, orig):
            dtype = d
            break
    m = re.search(r"\b(\d{1,2})\s*(people|of us|adults|family members|persons)", orig)
    hh = {"people": int(m.group(1)) if m else None,
          "infants": bool(re.search(r"\b(baby|infant|newborn|toddler)\b", orig)),
          "elderly": bool(re.search(r"elderly|grandm|grandp|senior|older (mom|dad|parent)", orig)),
          "pets": bool(re.search(r"\b(dog|cat|pet|pets|puppy|kitten)\b", orig)),
          "medical": []}
    needs.sort(key=lambda n: n["priority"])
    return {"disaster": {"type": dtype, "summary": ""}, "household": hh,
            "urgent_safety": {"flag": False, "reason": ""},
            "needs": needs[:12], "unmapped": []}


# --------------------------------------------------------- 2. alternatives
ALT_SYSTEM = """You help people find workable substitutes during emergencies. For each MISSING item, choose substitutes ONLY from the AVAILABLE list (items confirmed in stock near the person), best first, at most 3.

SAFETY RULES (never break these)
- Never suggest a substitute that could be dangerous for the stated use. Be explicit about compatibility (for example, butane canisters do not fit propane stoves), fire and carbon monoxide risks, and medical limits.
- A missing item with policy "none" (prescriptions, medical oxygen) has NO substitutes. Return an empty "options" list and put practical next steps in "escalate".
- A missing item with policy "caution" (infant formula, over-the-counter medicine, fuel) may be replaced ONLY by an id listed in that item's "allowed_substitutes" (an empty list means no substitute at all). Rank and explain within that list; never go outside it. Add a caution that names who to check with (pediatrician, pharmacist) when relevant.
- Think about the situation: if power or water is out, prefer substitutes that need neither.
- Do not give medication doses or chemical mixing ratios. Point to the product label, CDC, ready.gov, or a pharmacist.
- "no_purchase_tips" are short things they can do without buying anything. Only include well-established guidance, otherwise leave the list empty.
- fit: "strong" = does the same job, "partial" = covers part of it, "workaround" = temporary or improvised.
- If nothing available is a reasonable substitute, return no options and verdict "none_found". Do not stretch.

Respond with ONE JSON object and nothing else:
{"alternatives":[{"for_item":"<missing item_id>","verdict":"substitutes_available|none_found|no_safe_substitute","options":[{"item_id":"<id from AVAILABLE>","fit":"strong|partial|workaround","why":"one sentence on how it does the same job","how_to_use":"one sentence","caution":"one sentence, or empty"}],"no_purchase_tips":["..."],"escalate":"who to contact, or empty"}]}"""


def _alt_validator(missing_ids, available_ids):
    def validate(d):
        if not isinstance(d, dict) or not isinstance(d.get("alternatives"), list):
            raise ValueError('expected {"alternatives": [ ... ]}')
        out = {}
        for a in d["alternatives"]:
            if not isinstance(a, dict) or a.get("for_item") not in missing_ids:
                continue
            fid = a["for_item"]
            policy = CATALOG[fid]["policy"]
            options = []
            allowed = CATALOG[fid].get("safe_subs") if policy == "caution" else None
            if policy != "none":  # enforced in code, not just in the prompt
                for o in a.get("options") or []:
                    iid = o.get("item_id") if isinstance(o, dict) else None
                    if iid not in available_ids or iid == fid or any(x["item_id"] == iid for x in options):
                        continue
                    if allowed is not None and iid not in allowed:
                        continue  # caution items: curated allowlist only
                    cat = CATALOG[iid]
                    caution = _clip(o.get("caution"), 220)
                    if not caution and cat["policy"] != "free" and cat["note"]:
                        caution = cat["note"]
                    options.append({"item_id": iid,
                                    "fit": o.get("fit") if o.get("fit") in ("strong", "partial", "workaround") else "partial",
                                    "why": _clip(o.get("why"), 220), "how_to_use": _clip(o.get("how_to_use"), 220),
                                    "caution": caution})
                    if len(options) == 3:
                        break
            tips = [_clip(t, 160) for t in (a.get("no_purchase_tips") or []) if isinstance(t, str) and t.strip()][:3]
            verdict = ("no_safe_substitute" if policy == "none"
                       else "substitutes_available" if options else "none_found")
            out[fid] = {"for_item": fid, "verdict": verdict, "options": options, "no_purchase_tips": tips,
                        "escalate": _clip(a.get("escalate"), 300) or CATALOG[fid].get("escalate") or None}
        if not out:
            raise ValueError("no entry matched a missing item_id")
        for mid in missing_ids:
            out.setdefault(mid, {"for_item": mid, "verdict": "no_safe_substitute" if CATALOG[mid]["policy"] == "none" else "none_found",
                                 "options": [], "no_purchase_tips": [], "escalate": CATALOG[mid].get("escalate") or None})
        return list(out.values())
    return validate


RELATED_TAGS = {
    "drinking-water": {"water-treatment", "water-storage", "hydration"},
    "portable-power": {"light", "phone-charging", "rechargeable", "info-radio"},
    "fuel-propane": {"cooking-fuel", "cooking", "no-cook"},
    "cooking-fuel": {"cooking", "no-cook"},
    "baby-feeding": {"infant"},
    "keep-cold": {"keep-cold"},
    "backup-power": {"phone-charging", "rechargeable"},
    "light": {"light"},
}


def alternatives(gaps, pool, intent):
    """gaps: [{item_id,label,priority,why}], pool: {item_id: nearest store view}.
    -> (list of alternative entries, trace)"""
    t0 = time.monotonic()
    gap_ids = [g["item_id"] for g in gaps]
    gap_tags = set()
    for g in gaps:
        for tg in CATALOG[g["item_id"]]["tags"]:
            gap_tags |= {tg} | RELATED_TAGS.get(tg, set())
    allowed_all = set()
    for g in gaps:
        if CATALOG[g["item_id"]]["policy"] == "caution":
            allowed_all |= set(CATALOG[g["item_id"]].get("safe_subs") or [])
    candidates = [k for k in pool if CATALOG[k]["policy"] != "none" and k not in gap_ids]
    candidates.sort(key=lambda k: (k not in allowed_all, -len(gap_tags & set(CATALOG[k]["tags"])), pool[k]["dist_km"]))
    candidates = candidates[:45]
    available = [{"item_id": k, "name": CATALOG[k]["name"], "tags": CATALOG[k]["tags"],
                  "note": CATALOG[k]["note"], "km_away": pool[k]["dist_km"]} for k in candidates]
    missing = []
    for g in gaps:
        cat = CATALOG[g["item_id"]]
        m = {"item_id": g["item_id"], "name": cat["name"], "policy": cat["policy"],
             "person_called_it": g["label"], "priority": g["priority"], "why_needed": g["why"],
             "catalog_note": cat["note"]}
        if cat["policy"] == "caution":
            m["allowed_substitutes"] = cat.get("safe_subs") or []
        missing.append(m)
    situation = {"disaster": intent.get("disaster"), "household": intent.get("household")}
    user = ("SITUATION\n" + json.dumps(situation, ensure_ascii=False) +
            "\n\nMISSING (nobody nearby has these in stock)\n" + json.dumps(missing, ensure_ascii=False) +
            "\n\nAVAILABLE (confirmed in stock nearby)\n" + json.dumps(available, ensure_ascii=False))
    try:
        alts, trace = call_model("alternatives", ALT_SYSTEM, user,
                                 lambda raw: _alt_validator(set(gap_ids), set(candidates))(extract_json(raw)),
                                 max_tokens=1800)
    except AIUnavailable as exc:
        alts = fallback_alternatives(gaps, candidates, pool)
        trace = _fallback_trace("alternatives", exc, t0)
    return alts, trace


def fallback_alternatives(gaps, candidates, pool):
    out = []
    for g in gaps:
        cat = CATALOG[g["item_id"]]
        if cat["policy"] == "none":
            out.append({"for_item": g["item_id"], "verdict": "no_safe_substitute", "options": [],
                        "no_purchase_tips": [], "escalate": cat.get("escalate") or None})
            continue
        want = set()
        for tg in cat["tags"]:
            want |= {tg} | RELATED_TAGS.get(tg, set())
        pool_ids = candidates
        if cat["policy"] == "caution":  # curated allowlist only, no tag guessing
            pool_ids = [k for k in candidates if k in (cat.get("safe_subs") or [])]
        scored = sorted(((len(want & set(CATALOG[k]["tags"])), -pool[k]["dist_km"], k) for k in pool_ids), reverse=True)
        options = []
        for score, _, k in scored:
            if (score == 0 and cat["policy"] != "caution") or len(options) == 3:
                break
            c = CATALOG[k]
            shared = sorted(want & set(c["tags"]))
            options.append({"item_id": k, "fit": "partial",
                            "why": ("Serves a similar purpose (" + ", ".join(shared) + ")." if shared else "Approved alternative for this item."),
                            "how_to_use": "",
                            "caution": c["note"] if c["policy"] != "free" else ("Ask a professional if unsure." if cat["policy"] == "caution" else "")})
        out.append({"for_item": g["item_id"], "verdict": "substitutes_available" if options else "none_found",
                    "options": options, "no_purchase_tips": [], "escalate": cat.get("escalate") or None})
    return out


# ---------------------------------------------------------------- 3. brief
BRIEF_SYSTEM = """You write the short action briefing in SupplyLine for a person who is stressed and may be reading on a weak phone signal.

Use ONLY the facts in PLAN. Do not invent stores, hours, stock, phone numbers or prices. If a fact is missing, leave it out.
Voice: calm, direct, plain words (about 7th-grade reading level), sentence case, no exclamation marks, no emoji.
Safety first: if situation.urgent_safety.flag is true, the FIRST safety note must tell them to get safe and call 911 before driving for supplies. Add 1 to 3 more safety notes that are well established for this disaster and these items (for example: wildfire smoke -> N95 masks help, keep windows closed; generators and charcoal outdoors only; no candles if you smell gas; keep insulin cool but never frozen). No medication doses or chemical mixing ratios.
If an item was swapped for an alternative, say so in that stop's text and what to check (ask the pharmacist, confirm the stove type, and so on).
If items could not be found, use "if_stuck" for what to do next (call ahead, ask staff for alternatives, dial 211), at most 3 short bullets.
If PLAN.partly_available lists an item, the route cannot supply the full amount: say how many are still missing in the summary, and never say everything is covered. Use quantity_here for how many to buy at that stop.

Respond with ONE JSON object and nothing else:
{"headline":"max 90 characters: what to do first","summary":"2 sentences at most","steps":[{"stop":1,"say":"1-2 sentences: what to get here and anything to ask or check"}],"safety_notes":["..."],"if_stuck":["..."]}
Include exactly one "steps" entry per stop in PLAN, in the same order."""


def plan_for_prompt(plan, intent):
    stops = [{
        "stop": s["order"], "store": s["store"]["name"], "type": s["store"]["type"],
        "minutes_to_get_there": s["leg_min"], "arrive": s["arrive"], "hours": s["open_note"],
        "store_note": s["store"]["store_note"],
        "get": [{"item": i["name"], "person_called_it": i["label"], "swapped_in_for": i["replaces_name"],
                 "quantity_here": i["qty"], "quantity_needed": i["qty_needed"],
                 "low_stock": i["low"], "info_may_be_stale": i["confidence"] == "stale",
                 "neighbor_reported": i["source"] == "community"} for i in s["items"]],
    } for s in plan["route"]["stops"]]
    return {
        "situation": {"disaster": intent.get("disaster"), "household": intent.get("household"),
                      "urgent_safety": intent.get("urgent_safety")},
        "stops": stops,
        "not_found_nearby": [{"item": u["name"], "priority": u["priority"]} for u in plan["unresolved"]],
        "partly_available": [{"item": s["name"], "needed": s["qty"], "found": s["found"], "still_missing": s["missing"],
                              "priority": s["priority"]} for s in plan.get("shortfalls", [])],
        "no_substitute_policy": [a["for_name"] for a in plan["alternatives"] if a["policy"] == "none"],
    }


def _brief_validator(n_stops):
    def validate(d):
        if not isinstance(d, dict):
            raise ValueError("expected a JSON object")
        headline = _clip(d.get("headline"), 120)
        if not headline:
            raise ValueError("headline is missing")
        by_stop = {}
        for s in d.get("steps") or []:
            try:
                k = int(s.get("stop"))
            except (AttributeError, TypeError, ValueError):
                continue
            say = _clip(s.get("say"), 360)
            if 1 <= k <= n_stops and say:
                by_stop[k] = say
        return {"headline": headline, "summary": _clip(d.get("summary"), 320),
                "steps": [{"stop": k, "say": by_stop.get(k, "")} for k in range(1, n_stops + 1)],
                "safety_notes": [_clip(x, 240) for x in (d.get("safety_notes") or []) if isinstance(x, str) and x.strip()][:5],
                "if_stuck": [_clip(x, 200) for x in (d.get("if_stuck") or []) if isinstance(x, str) and x.strip()][:3]}
    return validate


GENERIC_SAFETY = {
    "wildfire": ["Smoke is harmful. An N95 mask helps; keep windows and doors closed, and be ready to leave if told to."],
    "earthquake": ["Expect aftershocks. If you smell gas, leave the building and call 911 or the gas utility from outside."],
    "power_outage": ["Use flashlights, not candles. Run generators and charcoal outdoors only, never in a garage or home."],
    "storm": ["Stay away from downed power lines and flooded roads. Run generators outdoors only."],
    "flood": ["Do not drive through flood water. Stay away from downed power lines."],
}


def fallback_brief(plan, intent):
    stops = plan["route"]["stops"]
    if stops:
        first = stops[0]
        headline = f"Start at {first['store']['name']}, about {first['leg_min']} minutes away."
        short = [f"{s['name'].lower()} ({s['found']} of {s['qty']})" for s in plan.get("shortfalls", [])]
        problems = ((f"{len(plan['unresolved'])} item(s) could not be found nearby. " if plan["unresolved"] else "")
                    + ("Not enough in stock for the full amount: " + ", ".join(short) + "." if short else ""))
        summary = (f"{len(stops)} stop{'s' if len(stops) != 1 else ''}, about {plan['route']['total_min']} minutes in all. "
                   + (problems.strip() or "Everything on your list is covered."))
    else:
        headline = "No open store near you has these items right now."
        summary = "Try a wider search area, or see the alternatives below."
    steps = []
    for s in stops:
        swaps = [i for i in s["items"] if i["is_alt"]]
        # only say something the ticket list does not already show
        say = ""
        if swaps:
            say = ("Ask staff to confirm that " + " and ".join(i["name"].lower() for i in swaps) +
                   " will work in place of " + " and ".join(i["replaces_name"].lower() for i in swaps) + ".")
        steps.append({"stop": s["order"], "say": say})
    safety = []
    if intent.get("urgent_safety", {}).get("flag"):
        safety.append("If anyone is in danger, call 911 and get to safety before you go shopping.")
    safety += GENERIC_SAFETY.get(intent.get("disaster", {}).get("type"), [])
    if not safety:
        safety = ["Stock levels are estimates. Call ahead if you can."]
    return {"headline": headline, "summary": summary, "steps": steps, "safety_notes": safety[:4],
            "if_stuck": ["Call the store before you leave if you can.", "Dial 211 for local help finding supplies."]}


def brief(plan, intent):
    """-> (brief, trace)"""
    t0 = time.monotonic()
    n = len(plan["route"]["stops"])
    user = "PLAN\n" + json.dumps(plan_for_prompt(plan, intent), ensure_ascii=False)
    try:
        b, trace = call_model("brief", BRIEF_SYSTEM, user,
                              lambda raw: _brief_validator(n)(extract_json(raw)), max_tokens=1400, temperature=0.3)
    except AIUnavailable as exc:
        b = fallback_brief(plan, intent)
        trace = _fallback_trace("brief", exc, t0)
    if intent.get("urgent_safety", {}).get("flag") and not (b["safety_notes"] and "911" in b["safety_notes"][0]):
        b["safety_notes"].insert(0, "If anyone is in danger, call 911 and get to safety before you go shopping.")
        b["safety_notes"] = b["safety_notes"][:5]
    return b, trace


# -------------------------------------------------------------- 4. follow-up
FOLLOWUP_SYSTEM = """You answer follow-up questions about a SupplyLine emergency supply plan.
Use only the PLAN text and well-established safety guidance. If you do not know, say so and suggest calling the store or dialing 211.
Keep the answer under 120 words, in plain words, with no markdown. If the question suggests someone is in danger, begin with: Call 911."""


def followup(question, plan_text):
    t0 = time.monotonic()

    def parse(raw):
        ans = (raw or "").strip()
        if not ans:
            raise ValueError("empty answer")
        return ans[:900]

    try:
        ans, trace = call_model("followup", FOLLOWUP_SYSTEM,
                                "PLAN\n" + plan_text[:6000] + "\n\nQUESTION\n" + question,
                                parse, max_tokens=500, temperature=0.3)
    except AIUnavailable as exc:
        ans = ("The AI model is not reachable right now, so I can't answer that. "
               "Your plan above still works. Call the store to confirm stock, or dial 211 for local help.")
        trace = _fallback_trace("followup", exc, t0)
    return ans, trace
