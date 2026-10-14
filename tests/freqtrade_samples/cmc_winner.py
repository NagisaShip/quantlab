# CMCWinner：CCI、MFI、CMO 三个超卖同时满足
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class CMCWinner(IStrategy):
    minimal_roi = {"40": 0.0, "30": 0.02, "20": 0.03, "0": 0.05}
    stoploss = -0.05
    timeframe = "15m"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["cci"] = ta.CCI(dataframe)
        dataframe["mfi"] = ta.MFI(dataframe)
        dataframe["cmo"] = ta.CMO(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["cci"].shift(1) < -100) & (dataframe["mfi"].shift(1) < 20) & (dataframe["cmo"].shift(1) < -50)),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["cci"].shift(1) > 100) & (dataframe["mfi"].shift(1) > 80) & (dataframe["cmo"].shift(1) > 50)),
            "exit_long",
        ] = 1
        return dataframe
