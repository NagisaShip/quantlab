# 大周期过滤：@informative('4h') 的 RSI 和 informative_pairs + merge_informative_pair 两种写法
import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.strategy import IStrategy, informative, merge_informative_pair
from pandas import DataFrame


class MultiTfRsi(IStrategy):
    minimal_roi = {"0": 0.08}
    stoploss = -0.08
    timeframe = "1h"
    startup_candle_count = 60

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(pair, "1d") for pair in pairs]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        daily = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe="1d")
        daily["sma"] = ta.SMA(daily, timeperiod=5)
        dataframe = merge_informative_pair(dataframe, daily, self.timeframe, "1d", ffill=True)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[((dataframe["rsi_4h"] > 50) & qtpylib.crossed_above(dataframe["rsi"], 40)), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] > 75) | (dataframe["rsi_4h"] < 40), "exit_long"] = 1
        return dataframe
