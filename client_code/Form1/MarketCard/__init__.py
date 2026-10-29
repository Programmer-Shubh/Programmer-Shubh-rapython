from ._anvil_designer import MarketCardTemplate


class MarketCard(MarketCardTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
