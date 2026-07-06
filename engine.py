"""
Bullwhip simulation engine — steel coking-coal value chain.
Pure Python (no Streamlit) so the mechanics can be tested headless.

Chain (downstream -> upstream), orders flow up, steel flows down:

    [0] Service Center  <-  [1] Regional Stockyard  <-  [2] Steel Plant  <-  [3] Coking-Coal Import Desk  <- (SOURCE: mine/port, capacity-constrained)

The SOURCE is where cyclones / port congestion bite: it caps how much
coking coal the import desk (tier 3) can actually receive. That shortage
ripples DOWN the chain as stock-outs, and the players' reactions ripple
amplified order swings back UP the chain — the Bullwhip Effect.

A game is a plain dict `G` (so it drops straight into st.session_state).
Lifecycle:  new_game() -> prepare_round() already called (awaiting orders)
            -> commit_round(orders) -> auto prepare_round() ... until finished.
"""

from __future__ import annotations
import numpy as np

TIER_NAMES = [
    "Service Center",
    "Regional Stockyard",
    "Steel Plant",
    "Coking-Coal Import Desk",
]
N = 4

# Per-link lead times (rounds) for supply arriving INTO each tier.
# Domestic links are short; the imported-coal link into tier 3 is long.
SHIP_DELAY = [1, 1, 2, 3]
ORDER_DELAY = 1  # information lag on every link

BASE_DEMAND = 40
HOLDING_COST = 1.0     # per unit of on-hand inventory per round
BACKLOG_COST = 3.0     # per unit of backorder per round (stock-outs hurt more)
SAFETY_BUFFER = 40     # extra target stock used by order policies
SOURCE_CAPACITY = 60   # normal coking-coal throughput at the source

DIFFICULTY = {
    "Calm":   dict(p=0.15, allow=["cyclone", "demand_surge"], max_new=1),
    "Normal": dict(p=0.30, allow=["cyclone", "port", "demand_surge", "demand_drop"], max_new=1),
    "Rough":  dict(p=0.45, allow=["cyclone", "port", "demand_surge", "demand_drop",
                                  "rake", "quality"], max_new=1),
    "Brutal": dict(p=0.62, allow=["cyclone", "port", "demand_surge", "demand_drop",
                                  "rake", "quality"], max_new=2),
}


# --------------------------------------------------------------------------- #
# Game construction
# --------------------------------------------------------------------------- #
def new_game(difficulty="Normal", seed=42, max_rounds=20,
             share_demand=False, auto=(False, False, False, False)):
    rng = np.random.default_rng(seed)
    inv0 = [2 * BASE_DEMAND for _ in range(N)]
    # supply_line = everything ordered but not yet received (in-transit + on-order)
    supply_line0 = [BASE_DEMAND * SHIP_DELAY[i] + BASE_DEMAND * ORDER_DELAY
                    for i in range(N)]
    G = {
        "rng": rng,
        "seed": seed,
        "difficulty": difficulty,
        "max_rounds": max_rounds,
        "share_demand": share_demand,
        "auto": list(auto),
        "round": 0,
        "finished": False,
        "awaiting_orders": False,
        # physical state
        "inv": [float(x) for x in inv0],
        "back": [0.0] * N,
        "supply_line": [float(x) for x in supply_line0],  # on-order, not yet received
        "oq": [[float(BASE_DEMAND)] * ORDER_DELAY for _ in range(N)],   # orders in transit up
        "sq": [[float(BASE_DEMAND)] * SHIP_DELAY[i] for i in range(N)],  # shipments in transit down
        "carryover": [0.0] * N,     # deferred arrivals (rake shortage)
        "source_back": 0.0,
        # per-round scratch (set by prepare)
        "demand_seen": [0.0] * N,
        "ip": [0.0] * N,
        "cur_events": [],
        "cap_mult": 1.0,
        "active_events": [],        # list of dicts {type,left,mag}
        # history
        "hist": {
            "round": [], "cust_demand": [], "capacity": [], "events": [],
            "orders": [[] for _ in range(N)],
            "demand_seen": [[] for _ in range(N)],
            "inv": [[] for _ in range(N)],
            "back": [[] for _ in range(N)],
            "shipped": [[] for _ in range(N)],
            "cost": [[] for _ in range(N)],
        },
        "total_cost": 0.0,
        "log": [],
    }
    prepare_round(G)
    return G


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def _spawn_event(G, etype):
    rng = G["rng"]
    if etype == "cyclone":
        ev = dict(type="cyclone", left=int(rng.integers(2, 5)),
                  mag=float(rng.uniform(0.2, 0.5)))
        msg = (f"🌀 Cyclone off the Queensland coast — coking-coal loading "
               f"cut to {int(ev['mag']*100)}% for {ev['left']} rounds.")
    elif etype == "port":
        ev = dict(type="port", left=int(rng.integers(2, 4)),
                  mag=float(rng.uniform(0.5, 0.75)))
        msg = (f"⚓ Port congestion at the discharge terminal — throughput "
               f"{int(ev['mag']*100)}% for {ev['left']} rounds.")
    elif etype == "demand_surge":
        ev = dict(type="demand_surge", left=int(rng.integers(2, 5)),
                  mag=float(rng.uniform(1.5, 2.2)))
        msg = (f"📈 Infrastructure order book jumps — end demand ×{ev['mag']:.1f} "
               f"for {ev['left']} rounds.")
    elif etype == "demand_drop":
        ev = dict(type="demand_drop", left=int(rng.integers(2, 4)),
                  mag=float(rng.uniform(0.4, 0.7)))
        msg = (f"📉 Auto & construction slump — end demand ×{ev['mag']:.1f} "
               f"for {ev['left']} rounds.")
    elif etype == "rake":
        ev = dict(type="rake", left=1, mag=0.5, tier=int(rng.integers(0, 3)))
        msg = (f"🚆 Rake shortage — half of this round's inbound to "
               f"{TIER_NAMES[ev['tier']]} slips to next round.")
    elif etype == "quality":
        t = int(rng.integers(0, N))
        ev = dict(type="quality", left=1, mag=float(rng.uniform(0.05, 0.15)), tier=t)
        msg = (f"🔬 Quality rejection — {int(ev['mag']*100)}% of "
               f"{TIER_NAMES[t]} stock written off this round.")
    else:
        return None, None
    return ev, msg


def _roll_events(G):
    rng = G["rng"]
    cfg = DIFFICULTY[G["difficulty"]]
    new_msgs = []
    n_new = 0
    while n_new < cfg["max_new"] and rng.random() < cfg["p"]:
        etype = str(rng.choice(cfg["allow"]))
        ev, msg = _spawn_event(G, etype)
        if ev is not None:
            G["active_events"].append(ev)
            new_msgs.append(msg)
            new_msgs_persist(G, msg)
        n_new += 1
    return new_msgs


def new_msgs_persist(G, msg):
    G["log"].append((G["round"], msg))


# --------------------------------------------------------------------------- #
# Round lifecycle
# --------------------------------------------------------------------------- #
def prepare_round(G):
    """Advance to the next round: fire events, receive supply, reveal demand.
    Leaves the game awaiting player/auto orders."""
    if G["finished"]:
        return
    G["round"] += 1
    rng = G["rng"]

    # 1) events -----------------------------------------------------------
    G["cur_events"] = _roll_events(G)

    # resolve active-event modifiers for THIS round
    cap_mult = 1.0
    demand_mult = 1.0
    rake_tier = None
    quality = []  # (tier, mag)
    still_active = []
    for ev in G["active_events"]:
        if ev["type"] in ("cyclone", "port"):
            cap_mult *= ev["mag"]
        elif ev["type"] == "demand_surge":
            demand_mult *= ev["mag"]
        elif ev["type"] == "demand_drop":
            demand_mult *= ev["mag"]
        elif ev["type"] == "rake":
            rake_tier = ev["tier"]
        elif ev["type"] == "quality":
            quality.append((ev["tier"], ev["mag"]))
        ev["left"] -= 1
        if ev["left"] > 0:
            still_active.append(ev)
    G["active_events"] = still_active
    cap_mult = max(0.1, cap_mult)
    G["cap_mult"] = cap_mult

    # 2) receive shipments (advance downstream pipelines) -----------------
    for i in range(N):
        raw = G["sq"][i].pop(0)              # delivered by supplier this round
        G["supply_line"][i] -= raw           # these units have left the pipeline
        total_in = raw + G["carryover"][i]
        G["carryover"][i] = 0.0
        if rake_tier is not None and i == rake_tier:
            held = round(total_in * 0.5)     # rake shortage defers half to next round
            G["carryover"][i] += held
            total_in -= held
        G["inv"][i] += total_in
    for (t, mag) in quality:
        G["inv"][t] *= (1.0 - mag)           # scrapped stock is a pure loss

    # 3) SOURCE production feeds tier 3 (capacity-constrained) ------------
    req = G["oq"][3].pop(0)                     # order tier 3 placed, now arrives at source
    capacity = SOURCE_CAPACITY * cap_mult
    produce = min(req + G["source_back"], capacity)
    G["source_back"] = max(0.0, req + G["source_back"] - produce)
    G["sq"][3].append(produce)                 # ships to tier 3 after its lead time
    G["hist"]["capacity"].append(capacity)

    # 4) reveal demand each tier faces this round -------------------------
    base = BASE_DEMAND * demand_mult
    noise = rng.normal(0, max(1.0, 0.05 * base))
    cust = max(0.0, round(base + noise))
    G["hist"]["cust_demand"].append(cust)
    G["demand_seen"][0] = cust
    for j in range(1, N):
        G["demand_seen"][j] = G["oq"][j - 1].pop(0)   # order placed by tier j-1 arrives

    # 5) inventory position for order policies / display ------------------
    for i in range(N):
        G["ip"][i] = G["inv"][i] - G["back"][i] + G["supply_line"][i]

    G["hist"]["round"].append(G["round"])
    G["hist"]["events"].append(" ".join(G["cur_events"]))
    G["awaiting_orders"] = True


def commit_round(G, orders):
    """Fulfil demand, ship downstream, place `orders` upstream, accrue cost,
    then prepare the next round (or finish)."""
    if not G["awaiting_orders"] or G["finished"]:
        return
    orders = [max(0.0, float(o)) for o in orders]

    for i in range(N):
        need = G["demand_seen"][i] + G["back"][i]
        ship = min(G["inv"][i], need)
        G["inv"][i] -= ship
        G["back"][i] = need - ship
        if i > 0:
            G["sq"][i - 1].append(ship)         # steel flows downstream
        # tier 0 ships to the external customer (leaves the system)

        cost = HOLDING_COST * G["inv"][i] + BACKLOG_COST * G["back"][i]
        G["total_cost"] += cost

        G["hist"]["orders"][i].append(orders[i])
        G["hist"]["demand_seen"][i].append(G["demand_seen"][i])
        G["hist"]["inv"][i].append(G["inv"][i])
        G["hist"]["back"][i].append(G["back"][i])
        G["hist"]["shipped"][i].append(ship)
        G["hist"]["cost"][i].append(cost)

    for i in range(N):
        G["oq"][i].append(orders[i])            # orders flow upstream
        G["supply_line"][i] += orders[i]        # now outstanding until received

    G["awaiting_orders"] = False
    if G["round"] >= G["max_rounds"]:
        G["finished"] = True
    else:
        prepare_round(G)


# --------------------------------------------------------------------------- #
# Order policies & metrics
# --------------------------------------------------------------------------- #
def target_stock(i):
    return BASE_DEMAND * (SHIP_DELAY[i] + ORDER_DELAY + 1) + SAFETY_BUFFER


def disciplined_order(G, i):
    """Pipeline-aware order-up-to (base-stock) — the 'good' benchmark.
    Restores inventory position to target S, fully crediting the supply line
    (in-transit + on-order). Because it counts what's already coming, it does
    NOT over-react to a shortage — order variance stays close to demand."""
    ip = G["inv"][i] - G["back"][i] + G["supply_line"][i]
    return max(0.0, round(G["demand_seen"][i] + (target_stock(i) - ip)))


def auto_order(G, i):
    """Naive policy for auto-played tiers: chases ON-HAND net stock and
    IGNORES the supply line. Under-weighting the pipeline (Sterman, 1989) is
    exactly what manufactures the bullwhip — during a shortage it keeps
    ordering for goods that are already on the way."""
    net = G["inv"][i] - G["back"][i]                    # pipeline ignored
    return max(0.0, round(G["demand_seen"][i] + (target_stock(i) - net)))


def bullwhip_ratios(G):
    """Var(orders_i) / Var(customer_demand), computed over committed rounds."""
    n = len(G["hist"]["orders"][0])
    if n < 2:
        return [np.nan] * N, np.nan
    cust = np.array(G["hist"]["cust_demand"][:n], dtype=float)
    var_c = np.var(cust)
    ratios = []
    for i in range(N):
        o = np.array(G["hist"]["orders"][i], dtype=float)
        ratios.append(float(np.var(o) / var_c) if var_c > 1e-9 else np.nan)
    return ratios, float(var_c)


def service_level(G):
    """System fill rate = shipped-to-customer / customer demand (tier 0)."""
    n = len(G["hist"]["shipped"][0])
    if n == 0:
        return np.nan
    shipped = np.array(G["hist"]["shipped"][0][:n], dtype=float)
    dem = np.array(G["hist"]["cust_demand"][:n], dtype=float)
    tot = dem.sum()
    return float(shipped.sum() / tot) if tot > 1e-9 else np.nan
