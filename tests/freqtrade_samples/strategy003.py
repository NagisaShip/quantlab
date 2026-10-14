# Strategy003：MFI、RSI、Fisher RSI、EMA、SAR 组合
import numpy
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Strategy003(IStrategy):
    minimal_roi = {"60": 0.01, "30": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.10
    timeframe = "5m"
    startup_candle_count = 50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["mfi"] = ta.MFI(dataframe)
        stoch_fast = ta.STOCHF(dataframe)
        dataframe["fastd"] = stoch_fast["fastd"]
        dataframe["fastk"] = stoch_fast["fastk"]
        dataframe["rsi"] = ta.RSI(dataframe)
        rsi = 0.1 * (dataframe["rsi"] - 50)
        dataframe["fisher_rsi"] = (numpy.exp(2 * rsi) - 1) / (numpy.exp(2 * rsi) + 1)
        dataframe["ema5"] = ta.EMA(dataframe, timeperiod=5)
        dataframe["ema10"] = ta.EMA(dataframe, timeperiod=10)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["sar"] = ta.SAR(dataframe)
        dataframe["sma"] = ta.SMA(dataframe, timeperiod=40)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 40)
                & (dataframe["close"] > 0.00000200)
                & (dataframe["volume"] > 0)
                & (dataframe["close"] < dataframe["sma"])
                & (dataframe["fisher_rsi"] < -0.5)
                & (dataframe["mfi"] < 40)
                & (dataframe["ema50"] > dataframe["ema100"])
                | ((dataframe["fastd"] > dataframe["fastk"]) & (dataframe["fastd"] > 0)) & (dataframe["rsi"] < 30)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[((dataframe["sar"] > dataframe["close"]) & (dataframe["fisher_rsi"] > 0.3)), "exit_long"] = 1
        return dataframe
