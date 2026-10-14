# EMASkipPump：成交量没有暴增时，收盘低于 EMA 且在布林下轨附近买
import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class EMASkipPump(IStrategy):
    EMA_SHORT_TERM = 5
    EMA_MEDIUM_TERM = 12
    EMA_LONG_TERM = 21
    minimal_roi = {"0": 0.1}
    stoploss = -0.05
    timeframe = "5m"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe[f"ema_{self.EMA_SHORT_TERM}"] = ta.EMA(dataframe, timeperiod=self.EMA_SHORT_TERM)
        dataframe[f"ema_{self.EMA_MEDIUM_TERM}"] = ta.EMA(dataframe, timeperiod=self.EMA_MEDIUM_TERM)
        dataframe[f"ema_{self.EMA_LONG_TERM}"] = ta.EMA(dataframe, timeperiod=self.EMA_LONG_TERM)
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        dataframe["min"] = ta.MIN(dataframe, timeperiod=self.EMA_MEDIUM_TERM)
        dataframe["max"] = ta.MAX(dataframe, timeperiod=self.EMA_SHORT_TERM)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] < (dataframe["volume"].rolling(window=30).mean().shift(1) * 20))
            & (dataframe["close"] < dataframe[f"ema_{self.EMA_SHORT_TERM}"])
            & (dataframe["close"] < dataframe[f"ema_{self.EMA_MEDIUM_TERM}"])
            & (dataframe["close"] == dataframe["min"])
            & (dataframe["close"] <= dataframe["bb_lowerband"]),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe[f"ema_{self.EMA_SHORT_TERM}"])
            & (dataframe["close"] > dataframe[f"ema_{self.EMA_MEDIUM_TERM}"])
            & (dataframe["close"] >= dataframe["max"])
            & (dataframe["close"] >= dataframe["bb_upperband"]),
            "exit_long",
        ] = 1
        return dataframe
