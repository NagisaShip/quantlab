# MACDStrategy：MACD 上穿信号线且 CCI 超卖（带 hyperopt 参数）
import talib.abstract as ta
from freqtrade.strategy import IntParameter, IStrategy
from pandas import DataFrame


class MACDStrategy(IStrategy):
    INTERFACE_VERSION = 3
    minimal_roi = {"60": 0.01, "30": 0.03, "20": 0.04, "0": 0.05}
    stoploss = -0.3
    timeframe = "5m"
    buy_cci = IntParameter(low=-700, high=0, default=-50, space="buy", optimize=True)
    sell_cci = IntParameter(low=0, high=700, default=100, space="sell", optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["cci"] = ta.CCI(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["macd"] > dataframe["macdsignal"])
                & (dataframe["cci"] <= self.buy_cci.value)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["macd"] < dataframe["macdsignal"])
                & (dataframe["cci"] >= self.sell_cci.value)
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
