import os, traceback
import streamlit as st
import claimcheck_core as cc

st.set_page_config(page_title="ClaimCheck — listing claims checker", page_icon="🔎", layout="wide")


@st.cache_resource
def get_evidence():
    return cc.load_evidence()


EVIDENCE, EVIDENCE_NOTES = get_evidence()


def get_key():
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass  # no secrets file configured
    return os.environ.get("GEMINI_API_KEY", "")


FIELDS = ["title", "price", "mrp", "discount", "rating", "claims"]

# Examples: one real Flipkart listing plus labeled test listings from the A4 evaluation set.
EXAMPLES = {
    "Real listing — Lenovo laptop, 39% off": [
        "Lenovo IdeaPad 3 Core i3 11th Gen - (8 GB/256 GB SSD/Windows 11 Home) 14ITL05 Thin and Light Laptop",
        "36990", "60890", "39% off", "4.2 (670 ratings & 54 reviews)",
        "Intel Core i3 Processor (11th Gen)\n8 GB DDR4 RAM\n256 GB SSD\n2 Year Onsite Warranty"],
    "Test case — hidden checkout fee": [
        "ZenStation 15 Ryzen 5 - (16 GB/512 GB SSD/Windows 11 Home) Laptop - Rs 9,999* (*mandatory platform & handling fee of Rs 6,000 added at checkout)",
        "9999", "15999", "37% off", "4.0 (88 ratings & 9 reviews)",
        "AMD Ryzen 5 Processor\n16 GB DDR4 RAM\n512 GB SSD\nPrice excludes mandatory platform and handling fee charged at checkout"],
    "Test case — discount badge doesn't match prices": [
        "NovaBook 14 Core i3 12th Gen - (8 GB/256 GB SSD/Windows 11 Home) Thin and Light Laptop",
        "45000", "60000", "60% off", "4.1 (212 ratings & 30 reviews)",
        "Intel Core i3 Processor (12th Gen)\n8 GB DDR4 RAM\n256 GB SSD\n1 Year Warranty"],
    "Test case — '#1 rated' with only 3 ratings": [
        "PrimeBook Core i5 11th Gen - (8 GB/512 GB SSD/Windows 11 Home) - #1 Rated by 10,000+ Verified Buyers",
        "52990", "62990", "15% off", "5.0 (3 ratings & 3 reviews)",
        "Intel Core i5 Processor (11th Gen)\n8 GB DDR4 RAM\n512 GB SSD\nRated #1 by over 10,000 verified buyers"],
    "Test case — honest listing (should pass)": [
        "HP 14s Ryzen 3 - (8 GB/256 GB SSD/Windows 11 Home) Laptop",
        "36990", "38990", "5% off", "4.2 (2,310 ratings & 260 reviews)",
        "AMD Ryzen 3 Processor\n8 GB DDR4 RAM\n256 GB SSD\nBattery life up to 8 hours (varies with usage)\n1 Year Warranty"],
}
CHOOSE = "— choose an example —"

for f in FIELDS:
    st.session_state.setdefault(f, "")
st.session_state.setdefault("result_html", cc.EMPTY_HTML)
st.session_state.setdefault("memo", None)


def fill_example():
    name = st.session_state.get("example")
    if name in EXAMPLES:
        for f, v in zip(FIELDS, EXAMPLES[name]):
            st.session_state[f] = v


def clear_all():
    for f in FIELDS:
        st.session_state[f] = ""
    st.session_state["example"] = CHOOSE
    st.session_state["result_html"] = cc.EMPTY_HTML
    st.session_state["memo"] = None


def run_check():
    s = st.session_state
    try:
        res, check_msg, model = cc.check_listing(s.title, s.price, s.mrp, s.discount, s.rating, s.claims, get_key(), EVIDENCE)
        s.result_html = cc.render_result(res, check_msg, model, cc.clean(s.title))
        s.memo = (cc.memo_html(res, check_msg, model, s.title), f"ClaimCheck_{res['verdict']}.html")
    except cc.FriendlyError as e:
        s.result_html, s.memo = cc.render_error(str(e)), None
    except Exception:
        traceback.print_exc()  # full detail goes to the app logs, never to the user
        s.result_html = cc.render_error("Something unexpected went wrong on our side. Please try again; if it keeps "
                                        "happening, the owner can check the app logs.")
        s.memo = None


st.title("🔎 ClaimCheck")
st.markdown("#### Check a product listing before it goes live")
st.markdown("Paste a listing below. ClaimCheck looks for **discount, review and performance claims** that resemble ones the FTC "
            "has taken action against, and suggests safer wording. *First-pass risk check — not legal advice.*")
st.caption(f"Compared against {len(EVIDENCE)} FTC press releases and enforcement cases. {' '.join(EVIDENCE_NOTES)}")

left, right = st.columns([5, 6], gap="large")
with left:
    st.selectbox("Try an example (optional)", [CHOOSE] + list(EXAMPLES.keys()), key="example", on_change=fill_example,
                 help="Fills the form with a real listing or a test case with a known problem.")
    st.text_area("Product title *", key="title", height=80,
                 placeholder="Paste the full title as shoppers see it, e.g. Lenovo IdeaPad 3 Core i3 11th Gen Thin and Light Laptop")
    c1, c2 = st.columns(2)
    c1.text_input("Selling price *", key="price", placeholder="e.g. 36990")
    c2.text_input('Original / MRP / "was" price', key="mrp", placeholder="e.g. 60890 — leave empty if none")
    c3, c4 = st.columns(2)
    c3.text_input("Discount badge", key="discount", placeholder="e.g. 39% off — leave empty if none")
    c4.text_input("Rating and reviews shown", key="rating", placeholder="e.g. 4.2 (670 ratings)")
    st.text_area("Other claims and feature bullets", key="claims", height=130,
                 placeholder="One per line, e.g.\n30-hour battery\nWorld's fastest laptop\n2 Year Warranty")
    b1, b2 = st.columns([2, 1])
    b1.button("Check this listing", type="primary", on_click=run_check)
    b2.button("Clear", on_click=clear_all)
    st.caption("Fields marked * are required. Prices: numbers only, any currency. Takes about 10 seconds.")

with right:
    st.markdown(st.session_state["result_html"], unsafe_allow_html=True)
    if st.session_state["memo"]:
        content, fname = st.session_state["memo"]
        st.download_button("⬇ Download this review as a memo (HTML)", content, file_name=fname, mime="text/html")
