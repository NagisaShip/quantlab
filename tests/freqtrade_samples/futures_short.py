# 合约做空 + 杠杆：平台导入只跑现货多头，杠杆写法应当报出来
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class FuturesShort(IStrategy):
    can_short = True
    minimal_roi = {"0": 0.05}
    stoploss = -0.1
    timeframe = "1h"

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return 3.0

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] < 30), "enter_long"] = 1
        dataframe.loc[(dataframe["rsi"] > 70), "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] > 60), "exit_long"] = 1
        return dataframe
