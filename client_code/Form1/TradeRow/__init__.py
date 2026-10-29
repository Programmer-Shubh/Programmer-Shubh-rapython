from ._anvil_designer import TradeRowTemplate


class TradeRow(TradeRowTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
