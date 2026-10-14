# AverageStrategy：EMA 8 / 21 交叉
import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class AverageStrategy(IStrategy):
    minimal_roi = {"0": 0.5}
    stoploss = -0.2
    timeframe = "4h"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["maShort"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["maMedium"] = ta.EMA(dataframe, timeperiod=21)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(qtpylib.crossed_above(dataframe["maShort"], dataframe["maMedium"])), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(qtpylib.crossed_above(dataframe["maMedium"], dataframe["maShort"])), "exit_long"] = 1
        return dataframe
