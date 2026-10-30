# --------------------------------------------------------------
# streamlit-paper-l2.py
# --------------------------------------------------------------
#  Paper‑trading L2 dashboard (Streamlit)
#  --------------------------------------------------------------
#  Requirements (install once):
#      pip install streamlit rich futu-api
#  --------------------------------------------------------------

import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import streamlit as st
from futu import OpenQuoteContext, SubType, RET_OK, OrderBookHandlerBase

# ========================= CONFIG =============================
HOST, PORT   = "127.0.0.1", 11111
SYMBOL       = "NVDA.US"          # <-- change or pass via CLI (see bottom)
MAX_LEVELS   = 20
IMB_THRESH   = 0.65               # buy‑vol / (buy+sell)
SPREAD_MAX   = 0.02               # $0.02
POS_QTY      = 100                # shares per signal
START_CASH   = 100_000.0
FILL_MODE    = "mid"              # "mid" | "bidask" | "best"
REFRESH_MS   = 500                # UI refresh interval
# ==============================================================

# -------------------------- DOMAIN ----------------------------
@dataclass
class LocalBook:
    max_levels: int = MAX_LEVELS
    bids: Dict[float, int] = field(default_factory=dict)
    asks: Dict[float, int] = field(default_factory=dict)

    def apply_snapshot(self, bids, asks):
        self.bids = {p: s for p, s in bids[:self.max_levels]}
        self.asks = {p: s for p, s in asks[:self.max_levels]}

    def apply_delta(self, side, price, size):
        book = self.bids if side == "Bid" else self.asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size
        if len(book) > self.max_levels:
            sorted_prices = sorted(book, reverse=(side == "Bid"))
            for p in sorted_prices[self.max_levels:]:
                book.pop(p, None)

    # ---- metric helpers ----
    def best_bid(self) -> float: return max(self.bids) if self.bids else 0.0
    def best_ask(self) -> float: return min(self.asks) if self.asks else 0.0
    def spread(self)   -> float: return self.best_ask() - self.best_bid()
    def mid_price(self)-> float: return (self.best_bid() + self.best_ask()) / 2

    def cum_vol(self, side: str, n: int) -> int:
        book = self.bids if side == "Bid" else self.asks
        prices = sorted(book, reverse=(side == "Bid"))[:n]
        return sum(book[p] for p in prices)

    def imbalance(self, n: int = 5) -> float:
        bv, av = self.cum_vol("Bid", n), self.cum_vol("Ask", n)
        return bv / (bv + av) if (bv + av) else 0.5

    def micro_price(self, n: int = 5) -> float:
        bv, av = self.cum_vol("Bid", n), self.cum_vol("Ask", n)
        if bv + av == 0: return self.mid_price()
        return (self.best_bid() * av + self.best_ask() * bv) / (bv + av)


class ImbalanceStrat:
    """Rule‑based: long when buy‑pressure high & spread tight."""
    def __init__(self, book: LocalBook):
        self.book = book
        self.position = 0

    def on_tick(self) -> int:
        imb = self.book.imbalance()
        spr = self.book.spread()
        if self.position == 0:
            if imb > IMB_THRESH and spr < SPREAD_MAX:
                self.position = POS_QTY
        else:
            if imb < 0.45 or spr > SPREAD_MAX * 2:
                self.position = 0
        return self.position


@dataclass
class PaperAccount:
    cash: float = START_CASH
    position: int = 0
    avg_px: float = 0.0
    realized: float = 0.0
    trades: List[dict] = field(default_factory=list)

    def _fill_price(self, book: LocalBook) -> float:
        if FILL_MODE == "bidask":
            return book.best_ask() if self.position == 0 else book.best_bid()
        if FILL_MODE == "best":
            return book.best_ask() if self.position == 0 else book.best_bid()
        return book.mid_price()

    def adjust(self, target_pos: int, book: LocalBook):
        if target_pos == self.position:
            return
        fill_px = self._fill_price(book)
        delta = target_pos - self.position

        if self.position == 0:                     # open long
            self.avg_px = fill_px
            self.cash -= delta * fill_px
        else:                                       # close long
            pnl = (fill_px - self.avg_px) * self.position
            self.realized += pnl
            self.cash += self.position * fill_px
            self.avg_px = 0.0

        self.trades.append({
            "side": "BUY" if delta > 0 else "SELL",
            "qty": abs(delta),
            "px": round(fill_px, 4),
            "cash": round(self.cash, 2),
            "realized": round(self.realized, 2),
        })
        self.position = target_pos

    def equity(self, book: LocalBook) -> float:
        mtm = (self.position * (book.mid_price() - self.avg_px)) if self.position else 0.0
        return self.cash + self.realized + mtm


# -------------------------- OpenD HANDLER --------------------
class L2Handler(OrderBookHandlerBase):
    def __init__(self, book: LocalBook, strat: ImbalanceStrat, acct: PaperAccount):
        super().__init__()
        self.book, self.strat, self.acct = book, strat, acct

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK or data.get("code") != SYMBOL:
            return RET_OK, data

        # snapshot vs. delta
        if "bids" in data and "asks" in data and data["bids"] and data["asks"]:
            if len(data["bids"]) > 1 and len(data["asks"]) > 1:
                self.book.apply_snapshot(data["bids"], data["asks"])
            else:
                for px, sz in data["bids"]:
                    self.book.apply_delta("Bid", px, sz)
                for px, sz in data["asks"]:
                    self.book.apply_delta("Ask", px, sz)

        target = self.strat.on_tick()
        self.acct.adjust(target, self.book)
        return RET_OK, data


# -------------------------- BACKGROUND THREAD ---------------
def start_openD_loop(book, strat, acct):
    """Runs in a daemon thread; keeps the subscription alive."""
    ctx = OpenQuoteContext(host=HOST, port=PORT)
    handler = L2Handler(book, strat, acct)
    ctx.set_handler(handler)
    ret, _ = ctx.subscribe([SYMBOL], [SubType.ORDER_BOOK])
    if ret != RET_OK:
        print("⚠️  OpenD subscribe failed – check L2 entitlement", file=sys.stderr)
    # The callback thread does all work; just sleep here.
    while True:
        time.sleep(1)


# -------------------------- STREAMLIT UI --------------------
st.set_page_config(page_title="Paper L2 – NVDA", layout="wide")
st.title(f"📈 Paper‑trading L2 Dashboard  •  {SYMBOL}")

# Initialise once (session_state survives reruns)
if "book" not in st.session_state:
    st.session_state.book   = LocalBook()
    st.session_state.strat  = ImbalanceStrat(st.session_state.book)
    st.session_state.acct   = PaperAccount()
    # start background OpenD loop
    t = threading.Thread(
        target=start_openD_loop,
        args=(st.session_state.book, st.session_state.strat, st.session_state.acct),
        daemon=True,
    )
    t.start()

book  = st.session_state.book
strat = st.session_state.strat
acct  = st.session_state.acct

# ---- top‑row metrics ----
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Best Bid", f"{book.best_bid():.4f}")
c2.metric("Best Ask", f"{book.best_ask():.4f}")
c3.metric("Spread",   f"{book.spread():.4f}")
c4.metric("Imb(5)",   f"{book.imbalance():.3f}")
c5.metric("Micro‑Px", f"{book.micro_price():.4f}")

c6, c7, c8 = st.columns(3)
c6.metric("Position", acct.position)
c7.metric("Cash",     f"${acct.cash:,.2f}")
c8.metric("Realized P&L", f"${acct.realized:,.2f}")

st.metric("💰 Equity (MTM)", f"${acct.equity(book):,.2f}")

# ---- recent trades table ----
if acct.trades:
    st.subheader("Recent simulated trades")
    st.dataframe(acct.trades[-30:], use_container_width=True, hide_index=True)

# ---- equity curve (simple line chart) ----
# we store a tiny history in session_state for plotting
if "equity_hist" not in st.session_state:
    st.session_state.equity_hist = []
st.session_state.equity_hist.append(acct.equity(book))
if len(st.session_state.equity_hist) > 500:
    st.session_state.equity_hist = st.session_state.equity_hist[-500:]

st.line_chart(st.session_state.equity_hist, height=250, use_container_width=True)

# ---- auto‑refresh ----
time.sleep(REFRESH_MS / 1000)
st.experimental_rerun()
