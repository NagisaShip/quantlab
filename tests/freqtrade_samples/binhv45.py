# BinHV45：布林下轨附近的急跌反弹（numpy 计算 + hyperopt 参数）
import numpy as np
from freqtrade.strategy import DecimalParameter, IStrategy
from pandas import DataFrame


def bollinger_bands(stock_price, window_size, num_of_std):
    rolling_mean = stock_price.rolling(window=window_size).mean()
    rolling_std = stock_price.rolling(window=window_size).std()
    lower_band = rolling_mean - (rolling_std * num_of_std)
    return rolling_mean, lower_band


class BinHV45(IStrategy):
    minimal_roi = {"0": 0.0125}
    stoploss = -0.05
    timeframe = "1m"
    buy_bbdelta = DecimalParameter(0.001, 0.016, default=0.008, space="buy")
    buy_closedelta = DecimalParameter(0.001, 0.03, default=0.0175, space="buy")
    buy_tail = DecimalParameter(0.1, 0.5, default=0.25, space="buy")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mid, lower = bollinger_bands(dataframe["close"], window_size=40, num_of_std=2)
        dataframe["mid"] = np.nan_to_num(mid)
        dataframe["lower"] = np.nan_to_num(lower)
        dataframe["bbdelta"] = (dataframe["mid"] - dataframe["lower"]).abs()
        dataframe["pricedelta"] = (dataframe["open"] - dataframe["close"]).abs()
        dataframe["closedelta"] = (dataframe["close"] - dataframe["close"].shift()).abs()
        dataframe["tail"] = (dataframe["close"] - dataframe["low"]).abs()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                dataframe["lower"].shift().gt(0)
                & dataframe["bbdelta"].gt(dataframe["close"] * self.buy_bbdelta.value)
                & dataframe["closedelta"].gt(dataframe["close"] * self.buy_closedelta.value / 10)
                & dataframe["tail"].lt(dataframe["bbdelta"] * self.buy_tail.value)
                & dataframe["close"].lt(dataframe["lower"].shift())
                & dataframe["close"].le(dataframe["close"].shift())
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        no_exit = dataframe["close"] < 0
        dataframe.loc[no_exit, "exit_long"] = 1
        return dataframe
