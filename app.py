"""ClaimCheck dashboard.

    streamlit run app.py

Calls the same service layer as the REST API, so roles, audit logging and re-qualification behave identically.
On first launch with an empty database it builds the offline demo (synthetic claimants, no LLM needed).
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import pymupdf
import streamlit as st

from claimcheck import config, evaluate, llm, plain, rules, search, service
from claimcheck.db import init_db
from claimcheck.ingest import doc_path

st.set_page_config(page_title="ClaimCheck", page_icon="⚖️", layout="wide")

# Categorical slots 1-3 of the validated reference palette (all-pairs CVD-safe), status colors with icon + label.
SERIES = {"exposure": "#2a78d6", "diagnosis": "#eb6834", "visit": "#1baf7a"}
METHOD_COLOR = {"pattern": "#2a78d6", "hybrid": "#eb6834", "llm": "#1baf7a"}
STATUS = {"qualified": ("✅", "Qualified"), "not_qualified": ("❌", "Not qualified"),
          "needs_review": ("⚠️", "Needs review"), "pending": ("⏳", "Pending")}
RULE_ICON = {"pass": "✅", "fail": "❌", "unknown": "⚠️"}


@st.cache_resource(show_spinner="First launch: loading the demo dataset (synthetic claimants)…")
def ensure_data():
    from claimcheck import bundle
    default_db = config.DATA_DIR / "claimcheck.db"
    if config.DATABASE_URL == f"sqlite:///{default_db}" and not default_db.exists():
        bundle.restore()          # precomputed hybrid-LLM run, so the hosted demo needs no model
    init_db()
    if not service.claimant_summary():
        from claimcheck.pipeline import build_demo
        build_demo(method="pattern", agent_mode="offline", regenerate=not (config.DOCS_DIR / "gold.json").exists(),
                   log=lambda *_: None)
    return True


ensure_data()


def status_label(s: str) -> str:
    icon, label = STATUS.get(s, ("", s))
    return f"{icon} {label}"


@st.cache_data(show_spinner=False, max_entries=64)
def render_page(path: str, page_no: int, quote: str) -> tuple[bytes, bool]:
    """PNG of one PDF page with the quoted evidence highlighted (when the page has a text layer)."""
    with pymupdf.open(doc_path(path)) as doc:
        page = doc[page_no - 1]
        found = False
        for part in quote.split(" … "):
            for line in [x.strip() for x in part.replace(". ", ".\n").split("\n") if len(x.strip()) > 6]:
                words = line.split()
                for i in range(0, len(words), 6):          # short word runs survive PDF line breaks
                    for rect in page.search_for(" ".join(words[i:i + 6])):
                        page.add_highlight_annot(rect)
                        found = True
        return page.get_pixmap(dpi=110).tobytes("png"), found


def show_evidence(fact: dict, key: str) -> None:
    png, found = render_page(fact["path"], fact["page_no"], fact["quote"] or "")
    st.caption(f"📄 {fact['filename']} · page {fact['page_no']}" +
               ("" if found else " · scanned page (no text layer): quote checked against OCR text"))
    st.image(png, use_container_width=True)


# ------------------------------------------------------------------------------------------------ sidebar
SIMPLE = ["Overview", "Claimants"]
TECHNICAL = ["Claimant details (technical)", "Accuracy & cost", "Behind the scenes"]
PEOPLE = {"a.rivera": "Ana Rivera · Attorney", "j.chen": "Jamie Chen · Paralegal"}


def open_claimant(ext: str, group: str) -> None:
    """Button callback: jump to a claimant in the Claimants workspace."""
    st.session_state["nav"], st.session_state["claimant_ext"], st.session_state["grp"] = "Claimants", ext, group


qp = st.query_params            # deep links, e.g. ?view=Claimants&claimant=CC-1003
if "nav" not in st.session_state:
    st.session_state["nav"] = qp.get("view") if qp.get("view") in SIMPLE + TECHNICAL else "Overview"
    st.session_state["tech"] = st.session_state["nav"] in TECHNICAL
    if "claimant" in qp:
        st.session_state["claimant_ext"] = qp["claimant"]

with st.sidebar:
    st.markdown("## ⚖️ ClaimCheck")
    st.caption("Checks claimant files against the case rules, with proof")
    user = st.selectbox("Viewing as", list(PEOPLE), format_func=PEOPLE.get)
    role = service.DEMO_USERS[user]
    tech = st.toggle("Show technical pages", key="tech")
    views = SIMPLE + (TECHNICAL if tech else [])
    if st.session_state["nav"] not in views:
        st.session_state["nav"] = "Overview"
    page = st.radio("Page", views, key="nav", label_visibility="collapsed")
    st.divider()
    case = rules.load_case(config.DEFAULT_CASE)
    st.caption(f"**Case:** {case['name']}")
    st.caption("All people and records are made up. No real patient data.")
    if tech:
        st.caption(f"**Live LLM:** {config.CHAT_MODEL if llm.llm_enabled() else 'off (offline mode)'} · "
                   f"**Dataset extracted with:** {service.extraction_provenance()} · "
                   f"**Database:** {config.DATABASE_URL.split(':')[0]}")

summary = service.claimant_summary()
by_ext = {c["external_id"]: c for c in summary}


def pick_claimant() -> dict:
    default = st.session_state.get("claimant_ext", summary[0]["external_id"])
    ext = st.selectbox("Claimant", list(by_ext), index=list(by_ext).index(default) if default in by_ext else 0,
                       format_func=lambda e: f"{e} · {by_ext[e]['name']} · {status_label(by_ext[e]['status'])}")
    st.session_state["claimant_ext"] = ext
    return by_ext[ext]


# ------------------------------------------------------------------------------------------------ overview
if page == "Overview":
    st.title("⚖️ ClaimCheck")
    st.markdown("#### Checks every claimant's records against the case rules in seconds, shows the proof, "
                "and leaves the decision to a person.")
    st.caption(f"Demo: {case['name']}. All people and records are made up.")

    counts = pd.Series([c["status"] for c in summary]).value_counts()
    n_q, n_r, n_n = (int(counts.get(k, 0)) for k in ("qualified", "needs_review", "not_qualified"))
    m = st.columns(4)
    m[0].metric("📂 Claimant files reviewed", len(summary))
    m[1].metric("✅ Ready for attorney sign-off", n_q)
    m[2].metric("⚠️ Need a person to check", n_r)
    m[3].metric("❌ Don't qualify", n_n)
    res = {r["method"]: r for r in evaluate.load_results()}
    best = res.get("hybrid") or next(iter(res.values()), None)
    if best:
        st.success(f"🛡️ **{len(best['false_qualifications'])} claimants wrongly approved** in testing "
                   f"({best['status_accuracy']:.0%} of files sorted correctly). When it isn't sure, it hands the file "
                   "to a person instead of guessing.")

    st.subheader("⏱️ Time it saves")
    minutes = st.slider("Your estimate: minutes a paralegal spends on a first-pass review of one file", 10, 120, 45, 5)
    secs = (best or {}).get("llm_usage", {}).get("seconds_per_claimant") or 10
    t = st.columns(3)
    t[0].metric(f"By hand, these {len(summary)} files", f"{len(summary) * minutes / 60:.0f} hours")
    t[1].metric("With ClaimCheck", f"~{max(1, round(len(summary) * secs / 60))} min",
                help="Computer time, plus a short human check of the flagged files")
    t[2].metric("At 5,000 claimants", f"{5000 * minutes / 60:,.0f} hours",
                help="Paralegal hours a manual first pass would take; people then focus only on the flagged files")

    st.subheader("How it works")
    steps = st.columns(4)
    for col, (title, text) in zip(steps, [
            ("1. 📥 Collect", "Pulls each client's records from the firm's case system automatically."),
            ("2. 🔎 Read", "Reads every page, even scans, and finds the dates, places and diagnoses, quoting the record "
                          "for each one."),
            ("3. ⚖️ Check", "Applies the case rules the attorneys set. The AI finds facts; it never makes the decision."),
            ("4. 👩‍⚖️ Approve", "Paralegals check anything uncertain, attorneys sign off, and every step is logged.")]):
        with col.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(text)

    st.subheader("See one claimant file")
    exts = list(by_ext)
    pick = st.selectbox("Claimant", exts, index=exts.index("CC-1003") if "CC-1003" in exts else 0,
                        format_func=lambda e: f"{by_ext[e]['name']} ({e})", label_visibility="collapsed")
    d = service.claimant_detail(by_ext[pick]["id"])
    ex = plain.explain(d, case)
    with st.container(border=True):
        st.markdown(f"### {ex['icon']} {d['name']}: {ex['headline']}")
        for i, row in enumerate(ex["rows"]):
            st.markdown(f"{row['icon']} **{row['rule']}**  \n{row['sentence']}")
            if row["proof_fact"]:
                with st.expander(f"📄 See the proof: {row['proof_label']}"):
                    show_evidence(row["proof_fact"], f"ov{i}")
        st.info(f"**Next step:** {ex['next_step']}")

    nr = [c for c in summary if c["status"] == "needs_review"]
    if nr:
        st.subheader(f"⚠️ What needs a person right now ({len(nr)})")
        for c in nr:
            dd = service.claimant_detail(c["id"])
            ee = plain.explain(dd, case)
            issue = next((r["sentence"] for r in ee["rows"] if r["icon"] != "✅"), dd["reason"])
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"**{dd['name']}**: {issue}  \n➡️ {ee['next_step']}")
            c2.button("Open file →", key=f"open{c['id']}", on_click=open_claimant, args=(c["external_id"], "needs_review"))

# ------------------------------------------------------------------------------------------------ claimants
elif page == "Claimants":
    st.title("Claimants")
    GROUPS = {"needs_review": "⚠️ Needs a person", "qualified": "✅ Ready to sign off",
              "not_qualified": "❌ Doesn't qualify", "signed": "🖊️ Signed off"}

    def in_group(c: dict, g: str) -> bool:
        return bool(c["decision"]) if g == "signed" else (c["status"] == g and not c["decision"])

    if st.session_state.get("grp") not in GROUPS:
        st.session_state["grp"] = "needs_review"
    grp = st.radio("Show", list(GROUPS), key="grp", horizontal=True, label_visibility="collapsed",
                   format_func=lambda g: f"{GROUPS[g]} ({sum(in_group(c, g) for c in summary)})")
    members = [c for c in summary if in_group(c, grp)]
    if not members:
        st.info("Nothing here right now.")
        st.stop()

    left, right = st.columns([1, 2.8], gap="large")
    with left:
        exts = [c["external_id"] for c in members]
        cur = st.session_state.get("claimant_ext")
        ext = st.radio("Claimant", exts, index=exts.index(cur) if cur in exts else 0,
                       format_func=lambda e: by_ext[e]["name"], label_visibility="collapsed")
        st.session_state["claimant_ext"] = ext

    with right:
        d = service.claimant_detail(by_ext[ext]["id"])
        ex = plain.explain(d, case)
        st.markdown(f"## {ex['icon']} {d['name']}")
        st.caption(f"{ex['headline']} · Matter {d['external_id']}")
        with st.container(border=True):
            for i, row in enumerate(ex["rows"]):
                st.markdown(f"{row['icon']} **{row['rule']}**  \n{row['sentence']}")
                if row["proof_fact"]:
                    with st.expander(f"📄 See the proof: {row['proof_label']}"):
                        show_evidence(row["proof_fact"], f"cl{i}")

        st.markdown("#### What to do")
        todo = False
        # 1) facts the system read but couldn't confirm: a person decides
        for f in [f for f in d["facts"] if not f["verified"] and f["review_status"] == "pending"
                  and f["fact_type"] in ("exposure", "diagnosis")]:
            todo = True
            with st.container(border=True):
                st.markdown(f"🔍 **Please check:** the system read *“{plain.describe_fact(f)}”* "
                            "but couldn't confirm it on the page.")
                show_evidence(f, f"u{f['id']}")
                b1, b2 = st.columns(2)
                if b1.button("✔ Looks right", key=f"ok{f['id']}", type="primary", use_container_width=True):
                    service.review_fact(f["id"], "approve", user, role)
                    st.rerun()
                if b2.button("✖ It's wrong", key=f"no{f['id']}", use_container_width=True):
                    service.review_fact(f["id"], "reject", user, role)
                    st.rerun()
        # 2) letters the system drafted for missing records
        for r in d["records_requests"]:
            if r["status"] != "draft":
                st.caption(f"📨 Letter to {r['provider']}: {r['status']}" +
                           (f" by {r['approved_by']}" if r["approved_by"] else ""))
                continue
            todo = True
            with st.container(border=True):
                st.markdown(f"📨 **Letter ready to send:** asks **{r['provider']}** for {r['record_type']}.")
                with st.expander("Read the letter"):
                    st.text(r["body"])
                if role != "attorney":
                    st.caption("🔒 Waiting for an attorney to approve it.")
                else:
                    b1, b2 = st.columns(2)
                    if b1.button("✔ Approve letter", key=f"ra{r['id']}", type="primary", use_container_width=True):
                        service.review_request(r["id"], True, user, role)
                        st.rerun()
                    if b2.button("✖ Don't send", key=f"rr{r['id']}", use_container_width=True):
                        service.review_request(r["id"], False, user, role)
                        st.rerun()
        # 3) attorney sign-off
        if d["decision"]:
            st.success(f"🖊️ Signed off by {d['decided_by']}: "
                       f"{'qualifies' if d['decision'] == 'qualified' else 'does not qualify'}.")
        elif d["status"] in ("qualified", "not_qualified"):
            todo = True
            rec = "qualifies" if d["status"] == "qualified" else "doesn't qualify"
            other = "not_qualified" if d["status"] == "qualified" else "qualified"
            with st.container(border=True):
                st.markdown(f"🖊️ **Sign-off.** The system recommends: this claimant **{rec}**.")
                if role != "attorney":
                    st.caption("🔒 An attorney signs off on this file.")
                else:
                    b1, b2 = st.columns(2)
                    if b1.button(f"✔ Agree: {rec}", type="primary", use_container_width=True):
                        service.decide_claimant(d["id"], d["status"], user, role)
                        st.rerun()
                    with b2.popover("Disagree", use_container_width=True):
                        note = st.text_input("Why? (required, kept in the audit log)")
                        if st.button("Save my decision"):
                            try:
                                service.decide_claimant(d["id"], other, user, role, note)
                                st.rerun()
                            except ValueError as e:
                                st.error(str(e))
        if not todo and not d["decision"]:
            st.info(ex["next_step"])

# ------------------------------------------------------------------------------------------------ claimant file
elif page == "Claimant details (technical)":
    c = pick_claimant()
    d = service.claimant_detail(c["id"])
    facts = {f["id"]: f for f in d["facts"]}
    head = st.columns([3, 2])
    head[0].markdown(f"### {d['name']}  \n{d['external_id']} · DOB {d['dob'] or '—'}")
    head[1].markdown(f"### {status_label(d['status'])}")
    head[1].caption(d["reason"] or "")
    if d["decision"]:
        st.success(f"Attorney decision: **{status_label(d['decision'])}** by {d['decided_by']}")

    tabs = st.tabs(["Criteria & evidence", "Chronology", "Facts", "Agent", "Ask the file", "Documents"])

    with tabs[0]:
        for r in d["rules"]:
            with st.expander(f"{RULE_ICON[r['status']]} {r['label']}", expanded=r["status"] != "pass"):
                st.write(r["explanation"])
                ev = [facts[i] for i in r["fact_ids"] if i in facts]
                for f in ev[:3]:
                    st.markdown(f"> “{f['quote']}”  \n"
                                f"{'✅ quote verified' if f['verified'] else '⚠️ quote NOT verified'} "
                                f"(match {f['match_score']:.0f}) · {f['method']} · review: {f['review_status']}")
                    if st.toggle("Show source page", key=f"ev{r['rule_id']}{f['id']}"):
                        show_evidence(f, f"r{f['id']}")
        st.divider()
        st.markdown("**Attorney sign-off**")
        if role != "attorney":
            st.caption("🔒 Only attorneys can record a final decision.")
        else:
            dc = st.columns([2, 3, 1])
            choice = dc[0].selectbox("Decision", ["qualified", "not_qualified"], format_func=status_label,
                                     index=0 if d["status"] == "qualified" else 1)
            note = dc[1].text_input("Note (required when overriding the system)")
            if dc[2].button("Record", type="primary"):
                try:
                    service.decide_claimant(d["id"], choice, user, role, note)
                    st.rerun()
                except (ValueError, PermissionError) as e:
                    st.error(str(e))

    with tabs[1]:
        chrono = search.chronology(d["id"])
        gaps = search.treatment_gaps(d["id"])
        m = st.columns(3)
        m[0].metric("Medical specials (billed)", f"${search.medical_specials(d['id']):,.2f}")
        m[1].metric("Dated events", len(chrono))
        m[2].metric(f"Treatment gaps > {config.TREATMENT_GAP_DAYS} days", len(gaps))
        if chrono:
            fig = go.Figure()
            ex = case["exposure"]
            fig.add_vrect(x0=ex["window_start"], x1=ex["window_end"], fillcolor="#898781", opacity=0.10, line_width=0,
                          annotation_text="eligibility window", annotation_position="top left")
            for g in gaps:
                fig.add_vrect(x0=g["from"], x1=g["to"], fillcolor="#fab219", opacity=0.18, line_width=0,
                              annotation_text=f"⚠ gap {g['days']}d", annotation_position="bottom left")
            lanes = {"exposure": 3, "diagnosis": 2, "visit": 1}
            for r in [r for r in chrono if r["fact_type"] == "exposure"]:
                fig.add_trace(go.Scatter(x=[r["date"], r["end"]], y=[3, 3], mode="lines", line=dict(width=10,
                              color=SERIES["exposure"]), name="Exposure", legendgroup="exposure", showlegend=False,
                              hovertemplate=f"{r['event']}<br>{r['document']} p.{r['page']}<extra></extra>"))
            for ft in ("exposure", "diagnosis", "visit"):
                pts = [r for r in chrono if r["fact_type"] == ft]
                if pts:
                    fig.add_trace(go.Scatter(
                        x=[r["date"] for r in pts], y=[lanes[ft]] * len(pts), mode="markers", name=ft.title(),
                        legendgroup=ft, marker=dict(size=11, color=SERIES[ft], line=dict(width=2, color="white")),
                        text=[f"{r['event']}<br>{r['document']} p.{r['page']}" for r in pts],
                        hovertemplate="%{x|%b %d, %Y}<br>%{text}<extra></extra>"))
            fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10), hovermode="closest",
                              yaxis=dict(tickvals=[1, 2, 3], ticktext=["Visits", "Diagnosis", "Exposure"],
                                         range=[0.4, 3.6], showgrid=False),
                              xaxis=dict(showgrid=True, gridcolor="rgba(137,135,129,0.2)"),
                              legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(fig, use_container_width=True, theme="streamlit")
            st.dataframe(pd.DataFrame([{"Date": r["date"], "Event": r["event"], "Source": f"{r['document']} p.{r['page']}"}
                                       for r in chrono]), hide_index=True, use_container_width=True)
        else:
            st.info("No verified dated events yet.")
        if gaps:
            st.warning("Gaps in treatment: " + "; ".join(f"{g['from']} → {g['to']} ({g['days']} days)" for g in gaps) +
                       ". Expect defense counsel to raise these; ask the client what care they received.")

    with tabs[2]:
        st.caption("Unverified facts never count as proof until a reviewer approves them.")
        for f in d["facts"]:
            icon = {"approved": "👍", "rejected": "👎", "edited": "✏️"}.get(f["review_status"],
                                                                         "✅" if f["verified"] else "⚠️")
            with st.expander(f"{icon} {f['fact_type']}: {json.dumps(f['value'])[:110]}"):
                st.markdown(f"> “{f['quote']}”")
                st.caption(f"{f['filename']} p.{f['page_no']} · {f['method']} · match {f['match_score']:.0f} · "
                           f"review: {f['review_status']}" + (f" by {f['reviewed_by']}" if f["reviewed_by"] else ""))
                b = st.columns(3)
                if b[0].button("Approve", key=f"a{f['id']}"):
                    service.review_fact(f["id"], "approve", user, role)
                    st.rerun()
                if b[1].button("Reject", key=f"x{f['id']}"):
                    service.review_fact(f["id"], "reject", user, role)
                    st.rerun()
                if b[2].toggle("Source page", key=f"s{f['id']}"):
                    show_evidence(f, f"f{f['id']}")

    with tabs[3]:
        st.caption("The agent looks things up and drafts next steps. It cannot change a status or send anything.")
        if st.button("▶ Run Case Review Agent", type="primary"):
            with st.spinner("Agent working…"):
                service.run_agent(d["id"], user, role)
            st.rerun()
        run = d["agent_run"]
        if run:
            st.markdown(f"**Last run** ({run['mode']}, {run['started_at']:%Y-%m-%d %H:%M})")
            st.markdown(run["summary"])
            with st.expander(f"Tool calls ({len(run['steps'])})"):
                for i, s in enumerate(run["steps"], 1):
                    st.markdown(f"**{i}. `{s['tool']}`** `{json.dumps(s['args'])}`")
                    st.code(json.dumps(s["result"], default=str, indent=1)[:1200], language="json")
        for r in d["records_requests"]:
            with st.expander(f"📨 Records request to {r['provider']}: {r['status'].upper()}"):
                st.text(r["body"])
                if r["status"] == "draft":
                    if role != "attorney":
                        st.caption("🔒 Needs attorney approval.")
                    else:
                        b = st.columns(2)
                        if b[0].button("Approve request", key=f"ra{r['id']}"):
                            service.review_request(r["id"], True, user, role)
                            st.rerun()
                        if b[1].button("Reject", key=f"rr{r['id']}"):
                            service.review_request(r["id"], False, user, role)
                            st.rerun()

    with tabs[4]:
        q = st.text_input("Ask a question about this claimant's records",
                          placeholder="Where did the claimant live in Cedar Creek, and when?")
        if q:
            with st.spinner("Searching…"):
                a = search.answer(d["id"], q)
            st.markdown(a["answer"])
            for i, h in enumerate(a["sources"], 1):
                st.markdown(f"**[S{i}]** {h['filename']} p.{h['page_no']} · score {h['score']:.2f}")
                st.caption(" ".join(h["text"].split())[:400])

    with tabs[5]:
        st.dataframe(pd.DataFrame([{"File": x["filename"], "Type": x["doc_type"], "Pages": x["pages"],
                                    "OCR": "yes" if x["ocr"] else ""} for x in d["documents"]]),
                     hide_index=True, use_container_width=True)
        doc = st.selectbox("Preview", d["documents"], format_func=lambda x: x["filename"])
        if doc:
            st.image(render_page(doc["path"], 1, "")[0], use_container_width=True)

# ------------------------------------------------------------------------------------------------ accuracy & cost
elif page == "Accuracy & cost":
    st.title("Accuracy & cost")
    st.caption("Measured against hand-built ground truth for the synthetic claimants. "
               "Run `scripts/run_llm_evals.sh` to add the local-LLM and hybrid rows.")
    res = {r["method"]: r for r in evaluate.load_results()}
    if not res:
        st.info("No evaluation results yet: run `python -m claimcheck.pipeline eval`.")
    else:
        order = [m for m in ("pattern", "hybrid", "llm") if m in res]
        names = {"pattern": "Pattern (free)", "hybrid": "Hybrid (pattern → LLM)", "llm": "LLM only"}
        cols = st.columns(len(order))
        for col, m in zip(cols, order):
            r = res[m]
            col.markdown(f"**{names[m]}**" + (f"  \n<small>{r['model']}</small>" if r.get("model") else ""),
                         unsafe_allow_html=True)
            col.metric("Correct status", f"{r['status_accuracy']:.0%}",
                       help=f"{round(r['status_accuracy'] * r['claimants'])} of {r['claimants']} claimants")
            col.metric("False qualifications", len(r["false_qualifications"]),
                       help="Claimants marked qualified who should not be. The costly error.")
            col.metric("Verified-fact recall", f"{r['facts']['verified']['overall']['recall']:.1%}")
            col.metric("Verified-fact precision", f"{r['facts']['verified']['overall']['precision']:.1%}")

        st.subheader("Recall by fact type (verified facts)")
        types = ["exposure", "diagnosis", "visit", "charge"]
        fig = go.Figure()
        for m in order:
            fig.add_trace(go.Bar(name=names[m], x=types, y=[res[m]["facts"]["verified"][t]["recall"] for t in types],
                                 marker=dict(color=METHOD_COLOR[m], cornerradius=4),
                                 hovertemplate="%{x}: %{y:.0%}<extra>" + names[m] + "</extra>"))
        fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08, height=320, yaxis=dict(tickformat=".0%",
                          range=[0, 1.05], gridcolor="rgba(137,135,129,0.2)"), margin=dict(l=10, r=10, t=10, b=10),
                          legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig, use_container_width=True, theme="streamlit")
        st.dataframe(pd.DataFrame([{"Method": names[m], **{t.title(): f"{res[m]['facts']['verified'][t]['recall']:.0%} / "
                                    f"{res[m]['facts']['verified'][t]['precision']:.0%}" for t in types}} for m in order]),
                     hide_index=True, use_container_width=True)
        st.caption("Cells: recall / precision.")

        st.subheader("Cost & speed")
        st.dataframe(pd.DataFrame([{
            "Method": names[m], "LLM calls": res[m]["llm_usage"]["calls"],
            "Tokens (in/out)": f"{res[m]['llm_usage']['input_tokens']:,} / {res[m]['llm_usage']['output_tokens']:,}",
            "LLM seconds per claimant": res[m]["llm_usage"]["seconds_per_claimant"],
            "Actual API cost": f"${res[m]['llm_usage']['actual_cost_usd']:.2f}",
            "Same tokens on a paid API, per claimant": f"${res[m]['llm_usage']['reference_cost_per_claimant_usd']:.4f}"}
            for m in order]), hide_index=True, use_container_width=True)
        any_r = res[order[-1]]
        st.caption(f"Reference price: {any_r['llm_usage']['reference_price']} (set in .env). "
                   "Local models run on the firm's own hardware: no per-token fee and no PHI leaves the machine.")
        with st.expander("Where each method went wrong"):
            for m in order:
                st.markdown(f"**{names[m]}**: " + (", ".join(f"{x['claimant']} (expected {x['expected']}, got {x['got']})"
                                                           for x in res[m]["missed"]) or "no misses"))

# ------------------------------------------------------------------------------------------------ integrations
elif page == "Behind the scenes":
    st.title("Behind the scenes: integrations & audit log")
    from claimcheck.integrations import cms, mock_cms
    st.markdown("**Firm case management system (mock)**: ClaimCheck pulls matters and documents, and writes "
                "status and attorney decisions back to each matter.")
    cols = st.columns(2)
    if cols[0].button("🔄 Sync with CMS now", disabled=role != "attorney",
                      help="Attorney only" if role != "attorney" else None):
        client = cms.in_process_client()
        st.success(f"Pull: {client.pull(actor=user)} · Push: {client.push(actor=user)}")
    matters = mock_cms.list_matters()
    st.dataframe(pd.DataFrame([{"Matter": m["matter_id"], "Client": m["client_name"],
                                "ClaimCheck status (in CMS)": status_label(m["claimcheck_status"] or "pending"),
                                "Attorney decision (in CMS)": m["attorney_decision"] or "—"} for m in matters]),
                 hide_index=True, use_container_width=True, height=240)
    st.subheader("Audit log")
    if role != "attorney":
        st.caption("🔒 The full audit log is visible to attorneys only.")
    else:
        a = service.audit_trail(300)
        acts = sorted({x["action"] for x in a})
        pick = st.multiselect("Actions", acts)
        st.dataframe(pd.DataFrame([{"Time": x["ts"], "Actor": x["actor"], "Role": x["role"], "Action": x["action"],
                                    "Entity": f"{x['entity']} {x['entity_id'] or ''}", "Detail": json.dumps(x["detail"])[:160]}
                                   for x in a if not pick or x["action"] in pick]),
                     hide_index=True, use_container_width=True)
