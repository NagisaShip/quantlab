# ClucMay72018：EMA100 下方、布林下轨、低成交量
import freqtrade.vendor.qtpylib.indicators as qtpylib
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class ClucMay72018(IStrategy):
    minimal_roi = {"0": 0.01}
    stoploss = -0.05
    timeframe = "5m"
    startup_candle_count = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=5)
        rsiframe = DataFrame(dataframe["rsi"]).rename(columns={"rsi": "close"})
        dataframe["emarsi"] = ta.EMA(rsiframe, timeperiod=5)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["adx"] = ta.ADX(dataframe)
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        dataframe["ema100"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["ema100"])
                & (dataframe["close"] < 0.985 * dataframe["bb_lowerband"])
                & (dataframe["volume"] < (dataframe["volume"].rolling(window=30).mean().shift(1) * 20))
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["bb_middleband"]), "exit_long"] = 1
        return dataframe
