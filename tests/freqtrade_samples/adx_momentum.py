# ADXMomentum：ADX > 25 且 +DI 在 -DI 上方、动量为正
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class ADXMomentum(IStrategy):
    minimal_roi = {"0": 0.01}
    stoploss = -0.25
    timeframe = "1h"
    startup_candle_count: int = 20

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["plus_di"] = ta.PLUS_DI(dataframe, timeperiod=25)
        dataframe["minus_di"] = ta.MINUS_DI(dataframe, timeperiod=25)
        dataframe["sar"] = ta.SAR(dataframe)
        dataframe["mom"] = ta.MOM(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["adx"] > 25)
                & (dataframe["mom"] > 0)
                & (dataframe["plus_di"] > 25)
                & (dataframe["plus_di"] > dataframe["minus_di"])
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["adx"] > 25)
                & (dataframe["mom"] < 0)
                & (dataframe["minus_di"] > 25)
                & (dataframe["plus_di"] < dataframe["minus_di"])
            ),
            "exit_long",
        ] = 1
        return dataframe
