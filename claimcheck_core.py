"""ClaimCheck core logic — no UI code here, so it can be tested on its own."""
import csv, json, os, re, html, time
from datetime import datetime
import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "verified")
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]  # fallback chain: try the next model on quota/overload
TOP_K = 6


class FriendlyError(Exception):
    """An error whose message is safe and helpful to show a non-technical user."""


# ---------------- cleaning helpers ----------------
def clean(s):
    s = html.unescape(html.unescape(str(s or "")))
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\ufffd", " ")
    s = re.sub(r"[^\x20-\x7E\u20B9\u2013\u2014\u2018\u2019\u201C\u201D]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def to_number(s):
    m = re.sub(r"[^0-9.]", "", str(s or ""))
    try:
        return float(m) if m else None
    except ValueError:
        return None


STOP = set("the and for with that this from are was were has have its their they will into over under about also than then them such "
           "which while who our your not but all any can may one two inc llc ltd gb cm bit gen new".split())


def tokens(s):
    return [w for w in re.split(r"[^a-z0-9]+", clean(s).lower()) if len(w) > 2 and w not in STOP]


THEMES = {
    "pricing": (["off", "discount", "mrp", "price", "deal", "save", "sale", "was", "fee", "fees", "checkout"],
                ["price", "pricing", "prices", "fee", "fees", "discount", "charge", "charges", "cost", "billing", "advertised", "hidden"]),
    "warranty": (["warranty", "guarantee", "guaranteed", "replacement"], ["warranty", "guarantee", "refund", "repair"]),
    "reviews": (["ratings", "reviews", "rated", "verified", "buyers", "stars"], ["review", "reviews", "rating", "ratings", "fake", "endorsement", "testimonial"]),
    "performance": (["fastest", "fast", "battery", "hours", "powerful", "best", "proven", "clinically", "performance"],
                    ["claims", "unsubstantiated", "substantiation", "performance", "deceptive", "misleading", "false"]),
    "origin": (["made", "usa", "america", "american", "origin"], ["made", "usa", "origin", "american"]),
}

# Stricter than A3: a bare "consent order" no longer qualifies (A3 grading feedback).
RELEVANT = ["advertis", "deceptive", "unfair", "pricing", "price", "fee", "review", "endorse", "claim", "marketing", "retail",
            "e-commerce", "ecommerce", "online", "consumer product"]


# ---------------- evidence (data contract sources 1 and 2) ----------------
def load_evidence(data_dir=DATA_DIR):
    evidence, notes = [], []
    rss_path = os.path.join(data_dir, "ftc_rss.xml")
    if os.path.exists(rss_path):
        xml = open(rss_path, encoding="utf-8", errors="replace").read()
        for block in re.findall(r"<item>(.*?)</item>", xml, flags=re.S):
            def get(tag):
                m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, flags=re.S)
                return clean(m.group(1)) if m else ""
            title, link, pub = get("title"), get("link"), get("pubDate")
            if not (title and pub):
                continue
            try:
                date = datetime.strptime(pub[:16], "%a, %d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                date = ""
            evidence.append({"source": "FTC press release", "title": title, "date": date,
                             "summary": get("description")[:450], "link": link if link.startswith("https://www.ftc.gov") else "", "rich": True})
    else:
        notes.append("FTC press-release file not found — using enforcement cases only.")

    csv_path = os.path.join(data_dir, "FTC-Nonmerger-Enforcement-Actions.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                name = clean(r.get("MatterName"))
                etype, industry = clean(r.get("MatterEnforcementType")), clean(r.get("MatterIndustry"))
                blob = f"{name} {etype} {industry}".lower()
                if not name or not any(k in blob for k in RELEVANT):
                    continue
                d = to_date(r.get("MatterEnforcementDate"))
                link = clean(r.get("Matterhyperlink")).strip("#").strip()
                evidence.append({"source": "FTC enforcement action", "title": name, "date": d,
                                 "summary": f"{etype} — {industry}", "link": link if link.startswith("https://www.ftc.gov") else "", "rich": False})
    else:
        notes.append("FTC enforcement file not found — using press releases only.")

    seen, unique = set(), []
    for e in evidence:
        if e["title"] not in seen:
            seen.add(e["title"]); e["tokens"] = set(tokens(e["title"] + " " + e["summary"])); unique.append(e)
    return unique, notes


def to_date(s):
    s = clean(s).split(" ")[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


# ---------------- input validation (plain-English errors) ----------------
def validate(title, price, mrp, discount):
    if not clean(title):
        raise FriendlyError("Please enter the product title exactly as it appears on the listing — it's where most claims live.")
    if len(clean(title)) < 8:
        raise FriendlyError("That title looks too short to check. Paste the full listing title, for example "
                            "\"Lenovo IdeaPad 3 Core i3 11th Gen Thin and Light Laptop\".")
    p = to_number(price)
    if p is None or p <= 0:
        raise FriendlyError("The selling price needs to be a number greater than zero, like 36990. Leave out currency signs and commas if it complains.")
    m = to_number(mrp) if clean(mrp) else None
    if clean(mrp) and (m is None or m <= 0):
        raise FriendlyError("The original price (MRP / \"was\" price) should be a number, like 60890 — or leave it empty if the listing doesn't show one.")
    if clean(discount) and to_number(discount) is None:
        raise FriendlyError("The discount should include a number, like \"39% off\" — or leave it empty if there's no discount badge.")
    if clean(discount) and to_number(discount) > 100:
        raise FriendlyError("A discount above 100% isn't possible — check the badge text (for example \"39% off\").")
    return p, m


def price_check(p, m, discount):
    stated = to_number(discount) if clean(discount) else None
    if m is None:
        return None, "No original price listed, so the discount can't be checked."
    if m < p:
        return None, f"Unusual: the original price ({m:g}) is lower than the selling price ({p:g})."
    computed = round((m - p) / m * 100)
    if stated is None:
        return computed, f"No discount badge stated (prices imply {computed}% off)."
    if abs(computed - stated) <= 1:
        return computed, f"Consistent: {stated:g}% stated, {computed}% from the prices."
    return computed, f"MISMATCH: the badge says {stated:g}% off, but the prices only support {computed}%."


# ---------------- retrieval + prompt ----------------
def retrieve(text, evidence, k=TOP_K):
    tk = set(tokens(text))
    themes = [t for t, (claim_kw, _) in THEMES.items() if tk & set(claim_kw)]
    scored = []
    for e in evidence:
        s = len(tk & e["tokens"]) + sum(3 for t in themes for kw in THEMES[t][1] if kw in e["tokens"]) + (1 if e["rich"] else 0)
        if s > 0:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    return [dict(id=f"E{i+1}", **{k2: v for k2, v in e.items() if k2 != "tokens"}) for i, (_, e) in enumerate(scored[:k])], themes


SYSTEM = (
    "You are a marketing-claims compliance analyst for a brand team. Follow a falsification mindset: do not assume a claim is "
    "true; look for reasons it could be unsupported, misleading, or contradicted by enforcement precedent. You receive ONE product "
    "listing plus FTC precedents retrieved as possibly relevant. Relevant concerns: reference/former-price discounts that may not "
    "reflect real prior prices, hidden or mandatory fees, unsubstantiated performance claims, warranty disclosure, misleading review or "
    "rating claims, false origin claims. Scoring: risk_score 0-100; verdict FLAG if >= 70, REVIEW if 40-69, PASS below 40. Only cite "
    "evidence IDs from the list, and only if they genuinely relate. The suggested rewrite must not add facts, prices or terms that are "
    "not in the listing. Write the reasoning in plain English for a non-lawyer. Respond with JSON only.")

SCHEMA = {"type": "OBJECT", "properties": {
    "verdict": {"type": "STRING", "enum": ["FLAG", "REVIEW", "PASS"]}, "risk_score": {"type": "INTEGER"},
    "primary_claim": {"type": "STRING"},
    "claim_type": {"type": "STRING", "enum": ["pricing", "performance", "warranty", "reviews_endorsement", "origin", "other"]},
    "reasoning": {"type": "STRING"}, "evidence_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
    "falsification_test": {"type": "STRING"}, "suggested_rewrite": {"type": "STRING"}},
    "required": ["verdict", "risk_score", "primary_claim", "claim_type", "reasoning", "evidence_ids", "falsification_test", "suggested_rewrite"]}


def build_listing_text(title, p, m, discount, rating, claims):
    lines = [f'Product listing title: "{clean(title)}"', f"Selling price: {p:g}"]
    if m is not None: lines.append(f"Original / MRP / 'was' price: {m:g}")
    if clean(discount): lines.append(f"Discount badge: {clean(discount)}")
    if clean(rating): lines.append(f"Rating / reviews shown: {clean(rating)}")
    bullets = [clean(x) for x in str(claims or "").splitlines() if clean(x)]
    if bullets: lines.append("Other claims and feature bullets: " + "; ".join(bullets))
    return "\n".join(lines)


def build_request(listing_text, check_msg, ev):
    ev_block = "\n".join(f"[{e['id']}] ({e['source']}, {e['date']}) {e['title']}: {e['summary']}" for e in ev) or "(no relevant precedent retrieved)"
    user = f"LISTING TO REVIEW:\n{listing_text}\n\nAUTOMATED PRICE CHECK: {check_msg}\n\nFTC PRECEDENTS (cite by ID):\n{ev_block}"
    return {"systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "responseSchema": SCHEMA}}


# ---------------- AI call with fallback ----------------
def call_gemini(body, api_key, post=requests.post, models=MODELS):
    if not api_key:
        raise FriendlyError("The tool isn't connected to its AI service yet (no API key is set). If you're the owner, add GEMINI_API_KEY in the Space settings.")
    last = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = post(url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, data=json.dumps(body), timeout=90)
            except requests.exceptions.Timeout:
                last = "timeout"; continue
            except requests.exceptions.RequestException:
                last = "network"; continue
            if r.status_code == 200:
                return r.json(), model
            if r.status_code == 429:
                last = "quota"; break                      # daily quota won't recover on retry → next model
            if r.status_code in (500, 502, 503, 504):
                last = "busy"; time.sleep(3); continue
            if r.status_code in (401, 403):
                raise FriendlyError("The AI service rejected the tool's API key. If you're the owner, check GEMINI_API_KEY in the Space settings.")
            last = f"http {r.status_code}"; break
    msgs = {"quota": "The AI service's free daily limit has been used up. Please try again tomorrow, or ask the owner to switch to a paid key.",
            "busy": "The AI service is overloaded right now. Please wait a minute and click Check again.",
            "timeout": "The AI service took too long to answer. Please click Check again.",
            "network": "The tool couldn't reach the AI service. Check your connection and try again."}
    raise FriendlyError(msgs.get(last, "The AI service returned an unexpected error. Please try again in a minute."))


# ---------------- guardrails ----------------
EXTRA_TERMS = ["inclusive of all taxes", "free shipping", "free delivery", "money-back", "money back", "lifetime", "certified"]


def parse_verdict(resp, ev, listing_text):
    text = "".join(p.get("text", "") for p in (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", []))
    if not text.strip():
        raise FriendlyError("The AI returned an empty answer. Please click Check again.")
    try:
        ai = json.loads(re.sub(r"^```(json)?|```$", "", text.strip()).strip())
    except json.JSONDecodeError:
        raise FriendlyError("The AI's answer came back in a broken format, so it wasn't shown. Please click Check again.")
    try:
        score = max(0, min(100, int(round(float(ai.get("risk_score"))))))
    except (TypeError, ValueError):
        score = {"FLAG": 75, "REVIEW": 50}.get(ai.get("verdict"), 20)
    band = "FLAG" if score >= 70 else "REVIEW" if score >= 40 else "PASS"
    verdict = band  # verdict always follows the score band
    ids = {e["id"]: e for e in ev}
    cited = [ids[i] for i in (ai.get("evidence_ids") or []) if i in ids]  # invented IDs are dropped
    rewrite = clean(ai.get("suggested_rewrite"))
    # Unsupported-fact check (A5 "to build" item): numbers or terms in the rewrite that are not in the listing
    src = listing_text.lower().replace(",", "")
    new_nums = [n for n in re.findall(r"\d[\d,]*\.?\d*", rewrite) if n.replace(",", "") not in src]
    new_terms = [t for t in EXTRA_TERMS if t in rewrite.lower() and t not in src]
    return {"verdict": verdict, "corrected": ai.get("verdict") != band, "risk": score, "claim_type": ai.get("claim_type", "other"),
            "primary_claim": clean(ai.get("primary_claim")), "reasoning": clean(ai.get("reasoning")),
            "falsification": clean(ai.get("falsification_test")), "rewrite": rewrite, "cited": cited,
            "unsupported": new_nums + new_terms}


# ---------------- rendering ----------------
COLORS = {"FLAG": "#B42318", "REVIEW": "#B54708", "PASS": "#027A48"}
HEAD = {"FLAG": "High risk — fix before publishing", "REVIEW": "Needs a closer look before publishing", "PASS": "Low risk — looks okay to publish"}
CSS = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1B2430;line-height:1.5;"
       "background:#FAF8F4;border-radius:12px;padding:18px 20px")
e_ = html.escape


def render_result(res, check_msg, model, listing_title):
    c = COLORS[res["verdict"]]
    cited = "".join(f'<li><a href="{e_(x["link"])}" target="_blank" style="color:#1A56B0">{e_(x["title"])}</a> <span style="color:#5B6573">({e_(x["source"])}, {e_(x["date"])})</span></li>'
                    if x["link"] else f'<li>{e_(x["title"])} <span style="color:#5B6573">({e_(x["source"])}, {e_(x["date"])})</span></li>'
                    for x in res["cited"]) or "<li>No closely matching FTC case — the verdict is based on the listing and general FTC principles.</li>"
    mismatch = check_msg.startswith("MISMATCH") or check_msg.startswith("Unusual")
    warn = (f'<div style="margin-top:6px;padding:8px 10px;background:#FFF7E6;border:1px solid #F5C77E;border-radius:6px;font-size:14px">'
            f'⚠ Check facts before using: this rewrite mentions <b>{e_(", ".join(res["unsupported"]))}</b>, which is not in your listing.</div>'
            if res["unsupported"] else "")
    return f"""<div style="{CSS}">
<div style="background:#fff;border:1px solid #E3E0D8;border-left:8px solid {c};border-radius:10px;padding:16px 18px">
 <div style="font-size:13px;color:#5B6573">{e_(listing_title[:110])}</div>
 <div style="font-size:22px;font-weight:700;color:{c};margin:4px 0">{res['verdict']} · {HEAD[res['verdict']]}</div>
 <div style="font-size:15px">Risk score <b>{res['risk']}</b> / 100 · claim type: {e_(res['claim_type'].replace('_', ' '))}</div>
 <div style="height:8px;background:#eee;border-radius:4px;margin:8px 0 2px"><div style="height:8px;width:{res['risk']}%;background:{c};border-radius:4px"></div></div>
 <div style="font-size:13px;color:#3E4652;margin-top:6px"><b>What the score means:</b> how serious the problem would be if this claim is wrong
 (not how confident the AI is). 70+ = fix before publishing · 40–69 = have a person check · under 40 = low risk.</div>
</div>
<h4 style="margin:16px 0 4px;color:#1B2430">What was checked</h4>
<ul style="margin:0;padding-left:20px;font-size:14px">
 <li>✓ <b>Discount math</b>: calculated by the tool from the prices you entered, so you can re-check it yourself.</li>
 <li>✓ <b>Similar FTC cases</b>: your listing was compared with {res.get('evidence_total', 0)} FTC records; the {res.get('retrieved', 0)} closest were given to the AI.</li>
 <li>✓ <b>AI review</b>: the AI picked the single riskiest claim and explained it.</li>
 <li>✗ <b>Not checked</b>: whether your prices, specs or reviews are true. ClaimCheck only knows what you typed, so a PASS means no risky
 claim was found in that text, not that the product facts are verified.</li>
</ul>
<h4 style="margin:16px 0 4px;color:#1B2430">The riskiest claim</h4><div>“{e_(res['primary_claim'])}”</div>
<h4 style="margin:14px 0 4px;color:#1B2430">Why</h4><div>{e_(res['reasoning'])}</div>
<h4 style="margin:14px 0 4px;color:#1B2430">Price check</h4><div style="{'color:#B42318;font-weight:600' if mismatch else ''}">{e_(check_msg)}</div>
<h4 style="margin:14px 0 4px;color:#1B2430">Similar FTC cases</h4><ul style="margin:0;padding-left:20px">{cited}</ul>
<h4 style="margin:14px 0 4px;color:#1B2430">How to prove or disprove the claim</h4><div>{e_(res['falsification'])}</div>
<h4 style="margin:14px 0 4px;color:#1B2430">Suggested safer wording</h4>
<div style="padding:10px 12px;background:#F0F7F3;border:1px solid #CFE7DA;border-radius:6px">{e_(res['rewrite'])}</div>{warn}
<div style="margin-top:14px;font-size:12px;color:#5B6573">This is a first-pass risk check, not legal advice. Model: {e_(model)}{' · verdict adjusted to match its score' if res['corrected'] else ''}.</div>
</div>"""


def render_error(msg):
    return (f'<div style="{CSS};border:1px solid #F3C5BF;border-left:8px solid #B42318;background:#FFF4F2">'
            f'<div style="font-size:18px;font-weight:700;color:#B42318">Couldn\'t check this listing</div>'
            f'<div style="margin-top:6px;font-size:15px">{e_(msg)}</div></div>')


EMPTY_HTML = (f'<div style="{CSS};border:1px dashed #C9C3B5;color:#3E4652">'
              '<div style="font-size:17px;font-weight:600;color:#1B2430">Your result will appear here</div>'
              '<ol style="margin:8px 0 0;padding-left:20px"><li>Paste the listing details on the left — or pick an example.</li>'
              '<li>Click <b>Check this listing</b>.</li><li>You\'ll get a verdict (FLAG, REVIEW or PASS), the reason, similar FTC cases, '
              'and safer wording you can paste back into your listing.</li></ol>'
              '<div style="margin-top:10px;font-size:13px">Takes about 10 seconds. A first-pass risk check, not legal advice.</div></div>')


def check_listing(title, price, mrp, discount, rating, claims, api_key, evidence, post=requests.post):
    p, m = validate(title, price, mrp, discount)
    _, check_msg = price_check(p, m, discount)
    listing_text = build_listing_text(title, p, m, discount, rating, claims)
    ev, _ = retrieve(listing_text + " " + check_msg, evidence)
    resp, model = call_gemini(build_request(listing_text, check_msg, ev), api_key, post=post)
    res = parse_verdict(resp, ev, listing_text)
    res["evidence_total"], res["retrieved"] = len(evidence), len(ev)
    return res, check_msg, model


def memo_html(res, check_msg, model, title):
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>ClaimCheck memo</title></head>"
            f"<body style='max-width:820px;margin:30px auto;padding:0 20px;background:#FAF8F4'>"
            f"<h2 style='font-family:Arial'>ClaimCheck review memo</h2>{render_result(res, check_msg, model, clean(title))}"
            f"<p style='font-family:Arial;font-size:12px;color:#5B6573'>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p></body></html>")
