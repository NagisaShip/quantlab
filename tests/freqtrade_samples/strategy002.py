# Strategy002：RSI + 随机指标 + 布林下轨 + 锤子线（这里锤子线换成实体判断）
import freqtrade.vendor.qtpylib.indicators as qtpylib
import numpy
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Strategy002(IStrategy):
    minimal_roi = {"60": 0.01, "30": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.10
    timeframe = "5m"
    startup_candle_count = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        stoch = ta.STOCH(dataframe)
        dataframe["slowk"] = stoch["slowk"]
        dataframe["rsi"] = ta.RSI(dataframe)
        rsi = 0.1 * (dataframe["rsi"] - 50)
        dataframe["fisher_rsi"] = (numpy.exp(2 * rsi) - 1) / (numpy.exp(2 * rsi) + 1)
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["sar"] = ta.SAR(dataframe)
        dataframe["body_up"] = (dataframe["close"] > dataframe["open"]).astype("int")
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 30)
                & (dataframe["slowk"] < 20)
                & (dataframe["bb_lowerband"] > dataframe["close"])
                & (dataframe["body_up"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[((dataframe["sar"] > dataframe["close"]) & (dataframe["fisher_rsi"] > 0.3)), "exit_long"] = 1
        return dataframe
