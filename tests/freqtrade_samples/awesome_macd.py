# AwesomeMacd：MACD 为正且 AO 上穿 0
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


def awesome_oscillator(dataframe, fast=5, slow=34):
    df = dataframe.copy()
    df["hl"] = (df["high"] + df["low"]) / 2
    return df["hl"].rolling(fast).mean() - df["hl"].rolling(slow).mean()


class AwesomeMacd(IStrategy):
    minimal_roi = {"0": 0.1}
    stoploss = -0.25
    timeframe = "1h"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["ao"] = awesome_oscillator(dataframe)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["macd"] > 0) & (dataframe["ao"] > 0) & (dataframe["ao"].shift() < 0)), "enter_long"
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["macd"] < 0) & (dataframe["ao"] < 0) & (dataframe["ao"].shift() > 0)), "exit_long"
        ] = 1
        return dataframe
