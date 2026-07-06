"""
The Coking-Coal Bullwhip Game — a Streamlit simulation for teaching the
Bullwhip Effect in a steel value chain.

Students run a 4-tier chain over many rounds. A capacity-constrained coking-coal
source is hit by random cyclones, port congestion, demand swings, rake shortages
and quality rejections. Each round players decide how much to order upstream —
and watch a small, steady customer demand turn into wild order swings as it
travels up the chain.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import numpy as np
import plotly.graph_objects as go
import streamlit as st

import engine as E

st.set_page_config(page_title="Coking-Coal Bullwhip Game", page_icon="🌀",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
      .block-container {padding-top:1.4rem;}
      .tier-card {border:1px solid #334155; border-radius:10px; padding:12px 14px;
                  background:#0f172a;}
      .tier-name {font-weight:700; font-size:0.95rem;}
      .muted {color:#94a3b8; font-size:0.8rem;}
      .pill {display:inline-block; padding:1px 8px; border-radius:10px;
             font-size:0.7rem; background:#334155; color:#e2e8f0; margin-left:6px;}
      .ev {background:#111827; border-left:4px solid #f59e0b; padding:8px 12px;
           border-radius:6px; margin-bottom:6px; font-size:0.85rem;}
      .crit {border-left-color:#ef4444;}
    </style>
    """,
    unsafe_allow_html=True,
)

TIER_ICONS = ["🏬", "🏗️", "🏭", "⛴️"]

# --------------------------------------------------------------------------- #
# Sidebar — setup
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("🌀 Bullwhip Game")
    st.caption("A steel coking-coal supply chain under stress.")

    difficulty = st.select_slider(
        "Difficulty (random-event intensity)",
        options=list(E.DIFFICULTY.keys()), value="Rough")
    max_rounds = st.slider("Rounds", 10, 40, 20)
    seed = st.number_input("Random seed", 0, 9999, 42, step=1,
                           help="Same seed = same run of events, so teams can be "
                                "compared and a game can be replayed in debrief.")

    st.markdown("**Who plays each tier?**  *(unchecked = you decide its order)*")
    auto = []
    for i, name in enumerate(E.TIER_NAMES):
        auto.append(st.checkbox(f"{TIER_ICONS[i]} {name} — auto (naive)",
                                value=(i != 2), key=f"auto_{i}"))
    st.caption("Default: you play the **Steel Plant**; the rest auto-play a naive "
               "(pipeline-ignoring) policy. Check/uncheck to play more tiers.")

    share_demand = st.checkbox("Share true customer demand with every tier",
                               value=False,
                               help="Information sharing — a classic bullwhip "
                                    "mitigation. Turn on and replay the same seed "
                                    "to see the swings shrink.")

    if st.button("▶️  Start / Restart game", type="primary", use_container_width=True):
        st.session_state["G"] = E.new_game(
            difficulty=difficulty, seed=int(seed), max_rounds=int(max_rounds),
            share_demand=share_demand, auto=tuple(auto))
        st.session_state["reveal"] = False

    st.divider()
    with st.expander("📖 How it works / the concept", expanded=False):
        st.markdown(
            "**Chain (steel flows down, orders flow up):**\n\n"
            "🏬 Service Center → 🏗️ Regional Stockyard → 🏭 Steel Plant → "
            "⛴️ Coking-Coal Import Desk → *(source: mine/port)*\n\n"
            "The **source** has limited capacity, and cyclones / port congestion "
            "cut it further. Shortages ripple **down**; panic orders ripple **up**, "
            "amplifying at every tier — the **Bullwhip Effect**.\n\n"
            "**Each round you:** see the order from your customer + your stock, "
            "then decide how much to order from your supplier. Lead times mean your "
            "order arrives 2–4 rounds later — so over-reacting today floods you "
            "tomorrow.\n\n"
            "**Costs:** ₹1 per unit of stock held, ₹3 per unit of backorder, "
            "per round. Lower total cost = better.\n\n"
            "**The trap:** react to your *on-hand* stock and ignore what's already "
            "*on the way*, and you will over-order. The **disciplined suggestion** "
            "on each card counts the pipeline for you."
        )

# --------------------------------------------------------------------------- #
# Initialise a game on first load
# --------------------------------------------------------------------------- #
if "G" not in st.session_state:
    st.session_state["G"] = E.new_game(difficulty="Rough", seed=42, max_rounds=20,
                                       auto=(True, True, False, True))
    st.session_state["reveal"] = False

G = st.session_state["G"]

# --------------------------------------------------------------------------- #
# Header + scoreboard
# --------------------------------------------------------------------------- #
st.title("The Coking-Coal Bullwhip Game")

ratios, var_c = E.bullwhip_ratios(G)
sl = E.service_level(G)
top_bw = ratios[-1] if not np.isnan(ratios[-1]) else float("nan")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Round", f"{G['round']} / {G['max_rounds']}")
c2.metric("Total chain cost", f"₹{G['total_cost']:,.0f}")
c3.metric("Customer service level",
          "—" if np.isnan(sl) else f"{sl*100:.0f}%")
c4.metric("Bullwhip at the source",
          "—" if np.isnan(top_bw) else f"{top_bw:.0f}×",
          help="Order-variance at the coking-coal tier ÷ true customer-demand "
               "variance. 1× = no amplification.")

# --------------------------------------------------------------------------- #
# Event ticker
# --------------------------------------------------------------------------- #
if not G["finished"]:
    st.subheader(f"Round {G['round']} — incoming events")
    if G["cur_events"]:
        for msg in G["cur_events"]:
            crit = ("Cyclone" in msg or "congestion" in msg)
            st.markdown(f'<div class="ev {"crit" if crit else ""}">{msg}</div>',
                        unsafe_allow_html=True)
    else:
        st.caption("A quiet round — no new disruptions. (Earlier events may still "
                   "be playing out.)")
    if G["cap_mult"] < 0.999:
        st.warning(f"Coking-coal source running at **{G['cap_mult']*100:.0f}%** "
                   f"of normal capacity this round.")

# --------------------------------------------------------------------------- #
# Tier cards + order entry
# --------------------------------------------------------------------------- #
def suggestion(i):
    dem = (G["hist"]["cust_demand"][-1] if G["share_demand"]
           else G["demand_seen"][i])
    ip = G["inv"][i] - G["back"][i] + G["supply_line"][i]
    return max(0, int(round(dem + (E.target_stock(i) - ip))))


if not G["finished"]:
    st.subheader("Your supply chain — place this round's orders")
    cols = st.columns(N := E.N)
    manual_orders = {}
    for i in range(E.N):
        with cols[i]:
            st.markdown(
                f'<div class="tier-card"><span class="tier-name">'
                f'{TIER_ICONS[i]} {E.TIER_NAMES[i]}</span>'
                f'{"<span class=pill>AUTO</span>" if G["auto"][i] else "<span class=pill>YOU</span>"}'
                f'</div>', unsafe_allow_html=True)
            st.metric("Order from your customer", f"{G['demand_seen'][i]:.0f}")
            a, b = st.columns(2)
            a.metric("On hand", f"{G['inv'][i]:.0f}")
            b.metric("Backorders", f"{G['back'][i]:.0f}",
                     delta=None, delta_color="inverse")
            st.caption(f"On the way (pipeline): **{G['supply_line'][i]:.0f}**  ·  "
                       f"position: **{G['inv'][i]-G['back'][i]+G['supply_line'][i]:.0f}**")
            if G["share_demand"] and G["hist"]["cust_demand"]:
                st.caption(f"📣 True customer demand: "
                           f"**{G['hist']['cust_demand'][-1]:.0f}**")
            if G["auto"][i]:
                st.caption(f"Auto order → **{E.auto_order(G, i):.0f}**")
            else:
                default = int(G["demand_seen"][i])
                manual_orders[i] = st.number_input(
                    "Order to place upstream ▲", min_value=0, max_value=2000,
                    value=default, step=5, key=f"ord_{i}_{G['round']}")
                st.caption(f"💡 Disciplined suggestion: **{suggestion(i)}** "
                           f"(counts the pipeline)")

    b1, b2 = st.columns([1, 1])
    if b1.button("✅  Advance round", type="primary", use_container_width=True):
        orders = [E.auto_order(G, i) if G["auto"][i] else manual_orders.get(i, 0)
                  for i in range(E.N)]
        E.commit_round(G, orders)
        st.rerun()
    if b2.button("⏩  Fast-forward to end (auto-play remaining)",
                 use_container_width=True):
        while not G["finished"]:
            orders = [E.auto_order(G, i) if G["auto"][i]
                      else suggestion(i) for i in range(E.N)]
            E.commit_round(G, orders)
        st.rerun()

# --------------------------------------------------------------------------- #
# End-of-game banner
# --------------------------------------------------------------------------- #
if G["finished"]:
    st.success(f"Game over after {G['round']} rounds. "
               f"Total chain cost **₹{G['total_cost']:,.0f}**, "
               f"customer service **{sl*100:.0f}%**.")
    st.session_state["reveal"] = True

# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
n = len(G["hist"]["orders"][0])
reveal = st.session_state.get("reveal", False) or G["share_demand"]

if n >= 1:
    st.divider()
    st.subheader("What happened")

    rounds = G["hist"]["round"][:n]
    palette = ["#38bdf8", "#a78bfa", "#f472b6", "#f59e0b"]

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📈 Orders (the bullwhip)", "📦 Inventory & backorders",
         "💸 Cost", "🔎 Bullwhip ratio"])

    with tab1:
        fig = go.Figure()
        for i in range(E.N):
            fig.add_trace(go.Scatter(
                x=rounds, y=G["hist"]["orders"][i], name=E.TIER_NAMES[i],
                mode="lines+markers", line=dict(color=palette[i], width=2)))
        if reveal:
            fig.add_trace(go.Scatter(
                x=rounds, y=G["hist"]["cust_demand"][:n],
                name="TRUE customer demand", mode="lines",
                line=dict(color="#e2e8f0", width=3, dash="dot")))
        else:
            st.caption("True customer demand is hidden until the game ends "
                       "(or you enable demand sharing) — that's the point.")
        fig.update_layout(height=430, legend_title_text="",
                          xaxis_title="Round", yaxis_title="Units ordered",
                          margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        if reveal and not np.isnan(top_bw):
            st.info(f"A near-flat customer demand became **{top_bw:.0f}×** more "
                    f"variable by the time it reached the coking-coal desk. "
                    f"Each tier reacted to the *distorted* order from below, not "
                    f"the real signal.")

    with tab2:
        fig = go.Figure()
        for i in range(E.N):
            fig.add_trace(go.Scatter(
                x=rounds, y=G["hist"]["inv"][i], name=f"{E.TIER_NAMES[i]} stock",
                mode="lines", line=dict(color=palette[i], width=2)))
            fig.add_trace(go.Scatter(
                x=rounds, y=[-b for b in G["hist"]["back"][i]],
                name=f"{E.TIER_NAMES[i]} backorder", mode="lines",
                line=dict(color=palette[i], width=1, dash="dot"),
                showlegend=False))
        fig.add_hline(y=0, line_color="#475569")
        fig.update_layout(height=430, xaxis_title="Round",
                          yaxis_title="Stock  (below 0 = backorders)",
                          legend_title_text="", margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Solid = on-hand stock, dotted below zero = backorders. "
                   "Watch stock swing from stock-out to glut — the cost of the swing.")

    with tab3:
        cum = np.cumsum([sum(G["hist"]["cost"][i][t] for i in range(E.N))
                         for t in range(n)])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=rounds, y=cum, mode="lines",
                                 line=dict(color="#ef4444", width=3),
                                 name="Cumulative chain cost"))
        fig.update_layout(height=430, xaxis_title="Round",
                          yaxis_title="Cumulative cost (₹)", margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

    with tab4:
        if n >= 2 and not any(np.isnan(ratios)):
            fig = go.Figure(go.Bar(
                x=E.TIER_NAMES, y=ratios,
                marker_color=palette, text=[f"{r:.1f}×" for r in ratios],
                textposition="outside"))
            fig.add_hline(y=1, line_dash="dash", line_color="#e2e8f0",
                          annotation_text="1× = no amplification")
            fig.update_layout(height=430, yaxis_title="Order variance ÷ demand variance",
                              yaxis_type="log", margin=dict(t=10))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Bars climbing left→right are the bullwhip: variance "
                       "amplifies as you move upstream. Log scale.")
        else:
            st.caption("Play at least two rounds to see the amplification.")

# --------------------------------------------------------------------------- #
# Debrief
# --------------------------------------------------------------------------- #
if G["finished"] or reveal:
    with st.expander("🎓 Debrief — why it happened & how to tame it",
                     expanded=G["finished"]):
        st.markdown(
            "**What you felt**\n\n"
            "- A supply shock at one end (coking coal) forced stock-outs, and "
            "everyone rebuilt buffers at once.\n"
            "- Lead times meant today's panic order landed after the shortage had "
            "passed — arriving into a glut.\n"
            "- Each tier saw only the *order below it*, already distorted, and "
            "amplified it again.\n\n"
            "**The four classic causes — in steel terms**\n\n"
            "1. *Demand-signal processing* — reacting to each order swing instead "
            "of the underlying demand.\n"
            "2. *Order batching* — full-rake / full-vessel economics forcing lumpy "
            "orders.\n"
            "3. *Price fluctuation* — forward-buying ahead of coal/steel price "
            "moves.\n"
            "4. *Shortage gaming* — over-ordering when material is allocated.\n\n"
            "**Mitigations you can test here**\n\n"
            "- **Share true demand** (sidebar toggle): replay the same seed and "
            "watch the upstream swings shrink.\n"
            "- **Count the pipeline**: follow the *disciplined suggestion* — it "
            "credits what's already on the way, so you stop double-ordering.\n"
            "- Shorter, more frequent orders; stable pricing; allocation by "
            "historical offtake during shortages; S&OP cadence.\n\n"
            "*Try this:* play once on **Rough** trusting your gut, then replay the "
            "**same seed** following the disciplined suggestion (or turn on demand "
            "sharing). Compare total cost and the bullwhip bars."
        )

st.caption("Synthetic training simulation. Costs are illustrative units, not "
           "actual figures.")
