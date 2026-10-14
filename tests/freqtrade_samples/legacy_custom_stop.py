# 旧版接口（buy / sell 列 + buy_tag）+ custom_stoploss + custom_exit + hlhb 式的 reduce 条件
from datetime import datetime
from functools import reduce

import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_open
from pandas import DataFrame


class LegacyCustomStop(IStrategy):
    INTERFACE_VERSION = 2
    minimal_roi = {"0": 0.2}
    stoploss = -0.15
    use_custom_stoploss = True
    timeframe = "1h"
    startup_candle_count = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=10)
        dataframe["ema5"] = ta.EMA(dataframe, timeperiod=5)
        dataframe["ema10"] = ta.EMA(dataframe, timeperiod=10)
        dataframe["adx"] = ta.ADX(dataframe)
        return dataframe

    def populate_buy_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        conditions.append(qtpylib.crossed_above(dataframe["rsi"], 50))
        conditions.append(
            qtpylib.crossed_above(dataframe["ema5"], dataframe["ema10"]) | (dataframe["ema5"] > dataframe["ema10"])
        )
        conditions.append(dataframe["adx"] > 20)
        conditions.append(dataframe["volume"] > 0)
        dataframe.loc[reduce(lambda x, y: x & y, conditions), ["buy", "buy_tag"]] = (1, "rsi_cross")
        return dataframe

    def populate_sell_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            qtpylib.crossed_below(dataframe["rsi"], 50) & qtpylib.crossed_below(dataframe["ema5"], dataframe["ema10"]),
            "sell",
        ] = 1
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: "Trade", current_time: datetime, current_rate: float, current_profit: float, **kwargs
    ) -> float:
        if current_profit > 0.06:
            return stoploss_from_open(0.03, current_profit)
        return 1

    def custom_exit(
        self, pair: str, trade: "Trade", current_time: datetime, current_rate: float, current_profit: float, **kwargs
    ):
        if current_profit < -0.05 and (current_time - trade.open_date_utc).days >= 1:
            return "unclog"
        return None
