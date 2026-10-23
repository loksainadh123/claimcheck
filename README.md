# ClaimCheck

Checks a product listing for discount, review and performance claims that resemble ones the FTC has taken action against,
and suggests safer wording. First-pass risk check, not legal advice.

- App: `streamlit_app.py` (interface) + `claimcheck_core.py` (logic)
- Data: `data/verified/ftc_rss.xml` (FTC consumer-protection press releases) and
  `data/verified/FTC-Nonmerger-Enforcement-Actions.csv` (FTC enforcement actions) — public FTC data
- Secret: `GEMINI_API_KEY` (set in Streamlit Community Cloud → App settings → Secrets; never commit it)

INFO7375 Assignment 6A · Kesana Lok Sainadh
