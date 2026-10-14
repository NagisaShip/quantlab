# 用 pandas_ta 的策略：兼容层没有 pandas_ta，应当报出来
import pandas_ta as pta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class PandasTaStrategy(IStrategy):
    minimal_roi = {"0": 0.05}
    stoploss = -0.1
    timeframe = "1h"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = pta.rsi(dataframe["close"], length=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] < 30), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] > 70), "exit_long"] = 1
        return dataframe
