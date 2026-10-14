# Strategy004：ADX + 慢速随机指标 + CCI
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Strategy004(IStrategy):
    minimal_roi = {"60": 0.01, "30": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.10
    timeframe = "5m"
    startup_candle_count = 50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe)
        dataframe["slowadx"] = ta.ADX(dataframe, 35)
        dataframe["cci"] = ta.CCI(dataframe)
        stoch = ta.STOCHF(dataframe, 5)
        dataframe["fastd"] = stoch["fastd"]
        dataframe["fastk"] = stoch["fastk"]
        dataframe["fastk-previous"] = dataframe.fastk.shift(1)
        dataframe["fastd-previous"] = dataframe.fastd.shift(1)
        slowstoch = ta.STOCHF(dataframe, 50)
        dataframe["slowfastd"] = slowstoch["fastd"]
        dataframe["slowfastk"] = slowstoch["fastk"]
        dataframe["slowfastk-previous"] = dataframe.slowfastk.shift(1)
        dataframe["slowfastd-previous"] = dataframe.slowfastd.shift(1)
        dataframe["mean-volume"] = dataframe["volume"].mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                ((dataframe["adx"] > 50) | (dataframe["slowadx"] > 26))
                & (dataframe["cci"] < -100)
                & ((dataframe["fastk-previous"] < 20) & (dataframe["fastd-previous"] < 20))
                & ((dataframe["slowfastk-previous"] < 30) & (dataframe["slowfastd-previous"] < 30))
                & (dataframe["fastk-previous"] < dataframe["fastd-previous"])
                & (dataframe["fastk"] > dataframe["fastd"])
                & (dataframe["close"] > 0.00000100)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["slowadx"] < 25)
                & ((dataframe["fastk"] > 70) | (dataframe["fastd"] > 70))
                & (dataframe["fastk-previous"] < dataframe["fastd-previous"])
                & (dataframe["close"] > dataframe["ema5"] if "ema5" in dataframe else dataframe["fastk"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
