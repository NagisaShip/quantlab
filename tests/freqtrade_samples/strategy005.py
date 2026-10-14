# Strategy005：带 hyperopt 参数的 RSI / 随机指标 / Fisher
import freqtrade.vendor.qtpylib.indicators as qtpylib
import numpy
import talib.abstract as ta
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy
from pandas import DataFrame


class Strategy005(IStrategy):
    INTERFACE_VERSION = 3
    minimal_roi = {"1440": 0.01, "80": 0.02, "40": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.10
    timeframe = "5m"
    startup_candle_count = 50

    buy_volumeAVG = IntParameter(low=50, high=300, default=70, space="buy", optimize=True)
    buy_rsi = IntParameter(low=1, high=100, default=30, space="buy", optimize=True)
    buy_fastd = IntParameter(low=1, high=100, default=65, space="buy", optimize=True)
    buy_fishRsiNorma = IntParameter(low=1, high=100, default=80, space="buy", optimize=True)
    sell_rsi = IntParameter(low=1, high=100, default=70, space="sell", optimize=True)
    sell_minusDI = IntParameter(low=1, high=100, default=50, space="sell", optimize=True)
    sell_fishRsiNorma = IntParameter(low=1, high=100, default=30, space="sell", optimize=True)
    sell_trigger = DecimalParameter(0.0, 1.0, default=0.5, space="sell")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        stoch_fast = ta.STOCHF(dataframe, 5, 3, 0)
        dataframe["fastd"] = stoch_fast["fastd"]
        dataframe["fastk"] = stoch_fast["fastk"]
        dataframe["minus_di"] = ta.MINUS_DI(dataframe)
        dataframe["rsi"] = ta.RSI(dataframe)
        rsi = 0.1 * (dataframe["rsi"] - 50)
        dataframe["fisher_rsi"] = (numpy.exp(2 * rsi) - 1) / (numpy.exp(2 * rsi) + 1)
        dataframe["fisher_rsi_norma"] = 50 * (dataframe["fisher_rsi"] + 1)
        dataframe["sar"] = ta.SAR(dataframe)
        dataframe["vol_mean"] = dataframe["volume"].rolling(30).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] > 0.00000200)
                & (dataframe["volume"] > dataframe["vol_mean"] * self.buy_volumeAVG.value / 100)
                & (dataframe["rsi"] < self.buy_rsi.value)
                & (dataframe["fastd"] > dataframe["fastk"])
                & (dataframe["fastd"] > self.buy_fastd.value)
                & (dataframe["fisher_rsi_norma"] < self.buy_fishRsiNorma.value)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = []
        conditions.append(
            (
                (qtpylib.crossed_above(dataframe["rsi"], self.sell_rsi.value))
                | (qtpylib.crossed_above(dataframe["fastd"], 70))
            )
            & (dataframe["minus_di"] > self.sell_minusDI.value)
            & (dataframe["fisher_rsi"] > self.sell_fishRsiNorma.value / 100.0 * self.sell_trigger.value)
        )
        dataframe.loc[conditions[0], "exit_long"] = 1
        return dataframe
