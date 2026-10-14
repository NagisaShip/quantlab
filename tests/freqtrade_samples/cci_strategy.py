# CCIStrategy：CCI 双周期 + MFI + CMF（资金流用自定义函数算）
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class CCIStrategy(IStrategy):
    minimal_roi = {"0": 0.1}
    stoploss = -0.02
    timeframe = "1m"
    startup_candle_count = 170

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.resample(dataframe, self.timeframe, 5)
        dataframe["cci_one"] = ta.CCI(dataframe, timeperiod=170)
        dataframe["cci_two"] = ta.CCI(dataframe, timeperiod=34)
        dataframe["rsi"] = ta.RSI(dataframe)
        dataframe["mfi"] = ta.MFI(dataframe)
        dataframe["cmf"] = self.chaikin_mf(dataframe)
        return dataframe

    def chaikin_mf(self, df, periods=20):
        close = df["close"]
        low = df["low"]
        high = df["high"]
        volume = df["volume"]
        mfv = ((close - low) - (high - close)) / (high - low)
        mfv = mfv.fillna(0.0)
        mfv *= volume
        cmf = mfv.rolling(periods).sum() / volume.rolling(periods).sum()
        return cmf

    def resample(self, dataframe, interval, factor):
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["cci_one"] < -100)
                & (dataframe["cci_two"] < -100)
                & (dataframe["cmf"] < -0.1)
                & (dataframe["mfi"] < 25)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["cci_one"] > 100)
                & (dataframe["cci_two"] > 100)
                & (dataframe["cmf"] > 0.3)
                & (dataframe["mfi"] > 80)
            ),
            "exit_long",
        ] = 1
        return dataframe
