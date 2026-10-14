# Simple：MACD、RSI、布林上轨突破
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class Simple(IStrategy):
    minimal_roi = {"0": 0.01}
    stoploss = -0.25
    timeframe = "5m"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=7)
        bollinger = ta.BBANDS(dataframe, timeperiod=12, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_upper"] = bollinger["upperband"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["macd"] > 0)
                & (dataframe["macd"] > dataframe["macdsignal"])
                & (dataframe["bb_upper"] > dataframe["bb_upper"].shift(1))
                & (dataframe["rsi"] > 70)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["rsi"] > 80), "exit_long"] = 1
        return dataframe
