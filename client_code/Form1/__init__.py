from ._anvil_designer import Form1Template
from anvil import *
import anvil.server


class Form1(Form1Template):
    def __init__(self, **properties):
        super().__init__(**properties)
        symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK", "TCS", "INFY"]
        self.chain_symbol.items = symbols[:3]
        self.trade_symbol.items = symbols[:3]
        self.trade_symbol.selected_value = "NIFTY"
        self.trade_option.items = ["CE", "PE"]
        self.trade_side.items = ["BUY", "SELL"]
        self.strategy_symbol.items = symbols[:3]
        self.strategy_symbol.selected_value = "NIFTY"
        self.show_page("dashboard")
        self.load_dashboard()
        source = anvil.server.call("get_source_status")
        self.status_label.text = "{} · {}".format(source["source_mode"], source["execution_mode"])

    def show_page(self, page):
        pages = {
            "dashboard": self.dashboard_panel,
            "trading": self.trading_panel,
            "chain": self.chain_panel,
            "strategy": self.strategy_panel,
        }
        for name, panel in pages.items():
            panel.visible = name == page
        self.page_title.text = {
            "dashboard": "Market dashboard",
            "trading": "Paper trading center",
            "chain": "Option chain",
            "strategy": "Strategy lab",
        }[page]

    def load_dashboard(self):
        data = anvil.server.call("get_dashboard")
        self.market_cards.items = data["cards"]
        self.mover_rows.items = data["movers"]
        self.position_rows.items = data["positions"]
        stats = data["stats"]
        self.dashboard_stats.text = (
            "Open {}  ·  Closed {}  ·  Total P&L ₹{:,.2f}  ·  Win rate {:.1f}%"
        ).format(
            stats["open_count"], stats["closed_count"], stats["total_pnl"], stats["win_rate"]
        )
        self.feed_status.text = data["feed_status"]

    def load_chain(self):
        data = anvil.server.call("get_option_chain", self.chain_symbol.selected_value)
        self.chain_meta.text = "{} spot ₹{:,.2f} · ATM {} · expiry {} · {}".format(
            data["symbol"], data["spot"], data["atm"], data["expiry"], data["source"]
        )
        self.chain_rows.items = [
            {
                "ce_ltp": "₹{:,.2f}".format(row["ce_ltp"]),
                "ce_oi": "{:,.0f}".format(row["ce_oi"]),
                "strike": "{:,.0f}".format(row["strike"]),
                "pe_oi": "{:,.0f}".format(row["pe_oi"]),
                "pe_ltp": "₹{:,.2f}".format(row["pe_ltp"]),
            }
            for row in data["rows"]
        ]

    def load_trades(self):
        data = anvil.server.call("get_trade_book")
        self.trade_mode_label.text = data["mode"]
        self.trade_rows.items = data["positions"]

    def _set_backtest_metrics(self, metrics):
        self.backtest_metrics.text = (
            "Net P&L ₹{:,.2f}  ·  Trades {}  ·  Win rate {:.1f}%  ·  Max drawdown ₹{:,.2f}"
        ).format(metrics["net_pnl"], metrics["total_trades"], metrics["win_rate"], metrics["max_drawdown"])

    @handle("dashboard_button", "click")
    def dashboard_button_click(self, **event_args):
        self.show_page("dashboard")
        self.load_dashboard()

    @handle("trading_button", "click")
    def trading_button_click(self, **event_args):
        self.show_page("trading")
        self.load_trades()

    @handle("chain_button", "click")
    def chain_button_click(self, **event_args):
        self.show_page("chain")
        self.load_chain()

    @handle("strategy_button", "click")
    def strategy_button_click(self, **event_args):
        self.show_page("strategy")

    @handle("refresh_button", "click")
    def refresh_button_click(self, **event_args):
        self.load_dashboard()

    @handle("chain_refresh_button", "click")
    def chain_refresh_button_click(self, **event_args):
        self.load_chain()

    @handle("run_backtest_button", "click")
    def run_backtest_button_click(self, **event_args):
        result = anvil.server.call("run_backtest", {
            "symbol": self.strategy_symbol.selected_value,
            "start_date": self.start_date.text,
            "end_date": self.end_date.text,
            "lots": self.lots.text,
            "stop_loss": self.stop_loss.text,
            "take_profit": self.take_profit.text,
        })
        if not result["ok"]:
            self.backtest_metrics.text = result["message"]
            self.backtest_trades.items = []
            return
        self._set_backtest_metrics(result["metrics"])
        self.backtest_trades.items = [
            {
                "symbol": result["symbol"],
                "option_type": "Trade",
                "strike": trade["date"],
                "transaction_type": "P&L",
                "pnl": "₹{:,.2f}".format(trade["pnl"]),
            }
            for trade in result["trades"]
        ]

    @handle("place_trade_button", "click")
    def place_trade_button_click(self, **event_args):
        result = anvil.server.call("place_paper_trade", {
            "symbol": self.trade_symbol.selected_value,
            "option_type": self.trade_option.selected_value,
            "transaction_type": self.trade_side.selected_value,
            "strike": self.trade_strike.text,
            "entry_price": self.trade_price.text,
            "quantity": self.trade_quantity.text,
        })
        self.trade_result.text = result["message"]
        if result["ok"]:
            self.load_trades()
