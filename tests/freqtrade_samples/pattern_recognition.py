# K 线形态（TA-Lib CDL 系列）：兼容层没有，应当报出来
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class PatternRecognition(IStrategy):
    minimal_roi = {"0": 0.05}
    stoploss = -0.1
    timeframe = "1h"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["CDLHAMMER"] = ta.CDLHAMMER(dataframe)
        dataframe["CDLENGULFING"] = ta.CDLENGULFING(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["CDLHAMMER"] > 0), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["CDLENGULFING"] < 0), "exit_long"] = 1
        return dataframe
