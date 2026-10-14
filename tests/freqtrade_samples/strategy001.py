# Strategy001（freqtrade-strategies）：EMA 20/50 金叉 + Heikin-Ashi 收阳
import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Strategy001(IStrategy):
    INTERFACE_VERSION = 3
    minimal_roi = {"60": 0.01, "30": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.10
    timeframe = "5m"
    trailing_stop = False
    process_only_new_candles = False
    use_exit_signal = True
    exit_profit_only = True
    ignore_roi_if_entry_signal = False
    startup_candle_count = 50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=100)
        heikinashi = qtpylib.heikinashi(dataframe)
        dataframe["ha_open"] = heikinashi["open"]
        dataframe["ha_close"] = heikinashi["close"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["ema20"], dataframe["ema50"])
                & (dataframe["ha_close"] > dataframe["ema20"])
                & (dataframe["ha_open"] < dataframe["ha_close"])
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                qtpylib.crossed_above(dataframe["ema50"], dataframe["ema100"])
                & (dataframe["ha_close"] < dataframe["ema20"])
                & (dataframe["ha_open"] > dataframe["ha_close"])
            ),
            "exit_long",
        ] = 1
        return dataframe
