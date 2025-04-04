#!/usr/bin/env python3

import logging
import os
import pathlib

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest
from torch.utils.data import DataLoader

from neuralprophet import NeuralProphet, configure, configure_components, df_utils, time_dataset, utils_time_dataset
from neuralprophet.data.process import _handle_missing_data
from neuralprophet.data.transform import _normalize

log = logging.getLogger("NP.test")
log.setLevel("ERROR")
log.parent.setLevel("ERROR")

DIR = pathlib.Path(__file__).parent.parent.absolute()
DATA_DIR = os.path.join(DIR, "tests", "test-data")
PEYTON_FILE = os.path.join(DATA_DIR, "wp_log_peyton_manning.csv")
AIR_FILE = os.path.join(DATA_DIR, "air_passengers.csv")
YOS_FILE = os.path.join(DATA_DIR, "yosemite_temps.csv")
NROWS = 512
EPOCHS = 1
BATCH_SIZE = 128
LR = 1.0

PLOT = False


def test_impute_missing():
    """Debugging data preprocessing"""
    log.info("testing: Impute Missing")
    allow_missing_dates = False
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    name = "test"
    df[name] = df["y"].values
    if not allow_missing_dates:
        df_na, _ = df_utils.add_missing_dates_nan(df.copy(deep=True), freq="D")
    else:
        df_na = df.copy(deep=True)
    to_fill = pd.isna(df_na["y"])
    # TODO fix debugging printout error
    log.debug(f"sum(to_fill): {sum(to_fill.values)}")
    # df_filled, remaining_na = df_utils.fill_small_linear_large_trend(
    #     df.copy(deep=True),
    #     column=name,
    #     allow_missing_dates=allow_missing_dates
    # )
    df_filled = df.copy(deep=True)
    df_filled.loc[:, name], remaining_na = df_utils.fill_linear_then_rolling_avg(
        df_filled[name], limit_linear=5, rolling=20
    )
    # TODO fix debugging printout error
    log.debug("sum(pd.isna(df_filled[name])): {}".format(sum(pd.isna(df_filled[name]).values)))
    if PLOT:
        if not allow_missing_dates:
            df, _ = df_utils.add_missing_dates_nan(df, freq="D")
        df = df.loc[200:250]
        plt.plot(df["ds"], df[name], "b-")
        plt.plot(df["ds"], df[name], "b.")
        df_filled = df_filled.loc[200:250]
        # fig3 = plt.plot(df_filled['ds'], df_filled[name], 'kx')
        plt.plot(df_filled["ds"][to_fill], df_filled[name][to_fill], "kx")
        plt.show()


def test_timedataset_minimal():
    # manually load any file that stores a time series, for example:
    df_in = pd.read_csv(AIR_FILE, index_col=False, nrows=NROWS)
    log.debug(f"Infile shape: {df_in.shape}")
    valid_p = 0.2
    for n_forecasts, n_lags in [(1, 0), (1, 5), (3, 5)]:
        config_ar = configure_components.AutoregRession(n_lags=n_lags)
        config_model = configure.Model(n_forecasts=n_forecasts)
        config_model.set_max_num_lags(n_lags)
        config_missing = configure.MissingDataHandling()
        # config_train = configure.Train()
        df_in, _, _, _ = df_utils.check_multiple_series_id(df_in)
        df, df_val = df_utils.split_df(df_in, n_lags, n_forecasts, valid_p)
        # create a tabularized dataset from time series
        # df = df.copy(deep=True)
        # df, _, _, _ = df_utils.check_multiple_series_id(df)
        df, _, _ = df_utils.check_dataframe(df)
        df = _handle_missing_data(
            df,
            freq="MS",
            n_lags=n_lags,
            n_forecasts=n_forecasts,
            config_missing=config_missing,
            # config_regressors: Optional[configure_components.FutureRegressors],
            # config_lagged_regressors: Optional[configure_components.LaggedRegressors],
            # config_events: Optional[configure_components.Events],
            # config_seasonality: Optional[configure_components.Seasonalities],
            predicting=False,
        )
        local_data_params, global_data_params = df_utils.init_data_params(df=df, normalize="minmax")
        df = df.drop("ID", axis=1)
        df = df_utils.normalize(df, global_data_params)
        df["ID"] = "__df__"

        components_stacker = utils_time_dataset.ComponentStacker(
            n_lags=n_lags,
            n_forecasts=n_forecasts,
            max_lags=n_lags,
            config_seasonality=None,
            lagged_regressor_config=None,
        )

        dataset = time_dataset.TimeDataset(
            df=df,
            components_stacker=components_stacker,
            predict_mode=False,
            config_model=config_model,
            config_missing=config_missing,
            config_ar=config_ar,
            config_seasonality=None,
            config_events=None,
            config_country_holidays=None,
            config_regressors=None,
            config_lagged_regressors=None,
        )
        input, meta = dataset.__getitem__(0)
        # # inputs50, targets50, meta50 = dataset.__getitem__(50)
        # log.debug(f"(n_forecasts {n_forecasts}, n_lags {n_lags})")
        # log.debug(f"tabularized targets: {targets.shape}")
        # log.debug(
        #     "tabularized inputs: {}".format(
        #         "; ".join(["{}: {}".format(inp, values.shape) for inp, values in inputs.items()])
        #     )
        # )


def test_normalize():
    length = 100
    days = pd.date_range(start="2017-01-01", periods=length)
    y = np.arange(length)
    df = pd.DataFrame({"ds": days, "y": y})
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        normalize="soft",
    )
    df = df.copy(deep=True)
    df, _, _, _ = df_utils.check_multiple_series_id(df)
    # with config

    m.config_normalization.init_data_params(df, m.config_lagged_regressors, m.config_regressors, m.config_events)
    _normalize(df=df, config_normalization=m.config_normalization)

    m.config_normalization.unknown_data_normalization = True
    _normalize(df=df, config_normalization=m.config_normalization)
    m.config_normalization.unknown_data_normalization = False

    # using config for utils
    df = df.drop("ID", axis=1)
    _ = df_utils.normalize(df.copy(deep=True), m.config_normalization.global_data_params)
    _ = df_utils.normalize(df.copy(deep=True), m.config_normalization.local_data_params["__df__"])


def test_normalize_utils():
    length = 100
    days = pd.date_range(start="2017-01-01", periods=length)
    y = np.arange(length)
    df = pd.DataFrame({"ds": days, "y": y})
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        normalize="soft",
    )
    df, _, _, _ = df_utils.check_multiple_series_id(df)

    # m.config_normalization.unknown_data_normalization = True

    # with utils
    local_data_params, global_data_params = df_utils.init_data_params(
        df=df,
        normalize=m.config_normalization.normalize,
        config_lagged_regressors=m.config_lagged_regressors,
        config_regressors=m.config_regressors,
        config_events=m.config_events,
        global_normalization=m.config_normalization.global_normalization,
        global_time_normalization=m.config_normalization.global_time_normalization,
    )
    log.error(local_data_params)
    log.error(global_data_params)
    df_utils.normalize(df.copy(deep=True), global_data_params)
    df_utils.normalize(df.copy(deep=True), local_data_params["__df__"])


def test_add_lagged_regressors():
    NROWS = 512
    EPOCHS = 3
    BATCH_SIZE = 32
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    df["A"] = df["y"].rolling(7, min_periods=1).mean()
    df["B"] = df["y"].rolling(15, min_periods=1).mean()
    df["C"] = df["y"].rolling(30, min_periods=1).mean()
    col_dict = {
        "1": "A",
        "2": ["B"],
        "3": ["A", "B", "C"],
    }
    for key, value in col_dict.items():
        log.debug(value)
        if isinstance(value, list):
            feats = np.array(["ds", "y"] + value)
        else:
            feats = np.array(["ds", "y", value])
        df1 = pd.DataFrame(df, columns=feats)
        cols = [col for col in df1.columns if col not in ["ds", "y"]]
        m = NeuralProphet(
            n_forecasts=1,
            n_lags=3,
            weekly_seasonality=False,
            daily_seasonality=False,
            epochs=EPOCHS,
            batch_size=BATCH_SIZE,
            learning_rate=LR,
        )
        m = m.add_lagged_regressor(names=cols)
        m.fit(df1, freq="D", validation_df=df1[-100:])
        future = m.make_future_dataframe(df1, n_historic_predictions=365)
        # Check if the future dataframe contains all the lagged regressors
        check = any(item in future.columns for item in cols)
        m.predict(future)
        log.debug(check)


def test_auto_batch_epoch():
    # for epochs = int(2 ** (2.3 * np.log10(100 + n_data)) / (n_data / 1000.0))
    # for epochs = int(2 ** (2.5 * np.log10(100 + n_data)) / (n_data / 1000.0))
    check = {
        "1": (1, 500),
        "10": (8, 500),
        "100": (16, 250),
        "1000": (32, 110),
        "10000": (128, 60),
        "100000": (256, 30),
        "1000000": (1024, 20),
        "10000000": (2048, 20),
    }

    for n_data, (batch_size, epochs) in check.items():
        n_data = int(n_data)
        c = configure.Train(
            learning_rate=None,
            epochs=None,
            batch_size=None,
            loss_func="mse",
            optimizer="SGD",
        )
        c.set_auto_batch_epoch(n_data=n_data)
        assert c.batch_size == batch_size
        assert c.epochs == epochs


def test_split_impute():
    def check_split(df_in, df_len_expected, n_lags, n_forecasts, freq, p=0.1):
        m = NeuralProphet(
            epochs=EPOCHS,
            batch_size=BATCH_SIZE,
            learning_rate=LR,
            n_lags=n_lags,
            n_forecasts=n_forecasts,
        )
        df_in, _, _, _ = df_utils.check_multiple_series_id(df_in)
        df_in, _, _ = df_utils.check_dataframe(df_in, check_y=False)
        df_in = _handle_missing_data(
            df=df_in,
            freq=freq,
            n_lags=n_lags,
            n_forecasts=n_forecasts,
            config_missing=m.config_missing,
            config_regressors=m.config_regressors,
            config_lagged_regressors=m.config_lagged_regressors,
            config_events=m.config_events,
            config_seasonality=m.config_seasonality,
            predicting=False,
        )
        assert df_len_expected == len(df_in)
        total_samples = len(df_in) - n_lags - 2 * n_forecasts + 2
        df_train, df_test = m.split_df(df_in, freq=freq, valid_p=0.1)
        n_train = len(df_train) - n_lags - n_forecasts + 1
        n_test = len(df_test) - n_lags - n_forecasts + 1
        assert total_samples == n_train + n_test
        n_test_expected = max(1, int(total_samples * p))
        n_train_expected = total_samples - n_test_expected
        assert n_train == n_train_expected
        assert n_test == n_test_expected

    log.info("testing: SPLIT: daily data")
    df = pd.read_csv(PEYTON_FILE)
    check_split(df_in=df, df_len_expected=len(df) + 59, freq="D", n_lags=10, n_forecasts=3)
    log.info("testing: SPLIT: monthly data")
    df = pd.read_csv(AIR_FILE, nrows=NROWS)
    check_split(df_in=df, df_len_expected=len(df), freq="MS", n_lags=10, n_forecasts=3)
    log.info("testing: SPLIT:  5min data")
    df = pd.read_csv(YOS_FILE, nrows=NROWS)
    check_split(df_in=df, df_len_expected=len(df), freq="5min", n_lags=10, n_forecasts=3)
    # redo with no lags
    log.info("testing: SPLIT: daily data")
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    check_split(df_in=df, df_len_expected=len(df), freq="D", n_lags=0, n_forecasts=1)
    log.info("testing: SPLIT: monthly data")
    df = pd.read_csv(AIR_FILE, nrows=NROWS)
    check_split(df_in=df, df_len_expected=len(df), freq="MS", n_lags=0, n_forecasts=1)
    log.info("testing: SPLIT:  5min data")
    df = pd.read_csv(YOS_FILE)
    check_split(df_in=df, df_len_expected=len(df) - 12, freq="5min", n_lags=0, n_forecasts=1)


def test_cv():
    def check_folds(df, n_lags, n_forecasts, valid_fold_num, valid_fold_pct, fold_overlap_pct):
        df, _, _, _ = df_utils.check_multiple_series_id(df)
        folds = df_utils.crossvalidation_split_df(
            df, n_lags, n_forecasts, valid_fold_num, valid_fold_pct, fold_overlap_pct
        )
        train_folds_len = []
        val_folds_len = []
        for f_train, f_val in folds:
            train_folds_len.append(len(f_train))
            val_folds_len.append(len(f_val))
        train_folds_samples = [x - n_lags - n_forecasts + 1 for x in train_folds_len]
        val_folds_samples = [x - n_lags - n_forecasts + 1 for x in val_folds_len]
        total_samples = len(df) - n_lags - (2 * n_forecasts) + 2
        val_fold_each = max(1, int(total_samples * valid_fold_pct))
        overlap_each = int(fold_overlap_pct * val_fold_each)
        assert all([x == val_fold_each for x in val_folds_samples])
        train_folds_should = [
            total_samples - val_fold_each - (valid_fold_num - i - 1) * (val_fold_each - overlap_each)
            for i in range(valid_fold_num)
        ]
        assert all([x == y for (x, y) in zip(train_folds_samples, train_folds_should)])

    len_df = 100
    df = pd.DataFrame({"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df)})
    check_folds(
        df=df,
        n_lags=0,
        n_forecasts=1,
        valid_fold_num=3,
        valid_fold_pct=0.1,
        fold_overlap_pct=0.0,
    )
    len_df = 1000
    df = pd.DataFrame({"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df)})
    check_folds(
        df=df,
        n_lags=50,
        n_forecasts=10,
        valid_fold_num=10,
        valid_fold_pct=0.1,
        fold_overlap_pct=0.5,
    )


def test_cv_for_global_model():
    def check_folds_dict(
        df, n_lags, n_forecasts, valid_fold_num, valid_fold_pct, fold_overlap_pct, global_model_cv_type="local"
    ):
        "Does not work with global_model_cv_type == global-time or global_model_cv_type is None"
        df, _, _, _ = df_utils.check_multiple_series_id(df)
        folds = df_utils.crossvalidation_split_df(
            df,
            n_lags,
            n_forecasts,
            valid_fold_num,
            valid_fold_pct,
            fold_overlap_pct,
            global_model_cv_type=global_model_cv_type,
        )
        for df_name, df_i in df.groupby("ID"):
            train_folds_len = []
            val_folds_len = []
            for f_train, f_val in folds:
                train_folds_len.append(len(f_train[f_train["ID"] == df_name]))
                val_folds_len.append(len(f_val[f_val["ID"] == df_name]))
            if global_model_cv_type == "local":
                total_samples = len(df_i) - n_lags - (2 * n_forecasts) + 2
            elif global_model_cv_type == "intersect":
                start_date, end_date = df_utils.find_valid_time_interval_for_cv(df)
                total_samples = len(pd.date_range(start=start_date, end=end_date)) - n_lags - (2 * n_forecasts) + 2
            else:
                raise ValueError(
                    "Insert valid value for global_model_cv_type (None or global-type does not work for this function"
                )
            train_folds_samples = [x - n_lags - n_forecasts + 1 for x in train_folds_len]
            val_folds_samples = [x - n_lags - n_forecasts + 1 for x in val_folds_len]
            val_fold_each = max(1, int(total_samples * valid_fold_pct))
            overlap_each = int(fold_overlap_pct * val_fold_each)
            assert all([x == val_fold_each for x in val_folds_samples])
            train_folds_should = [
                total_samples - val_fold_each - (valid_fold_num - i - 1) * (val_fold_each - overlap_each)
                for i in range(valid_fold_num)
            ]
            assert all([x == y for (x, y) in zip(train_folds_samples, train_folds_should)])
        return folds

    # Test cv for dict with time series with similar time range
    len_df = 1000
    df1 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df) * 3, "ID": "df1"}
    )
    df2 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df) * 5, "ID": "df2"}
    )
    df3 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df) * 2, "ID": "df3"}
    )
    df_global = pd.concat((df1, df2, df3))
    n_lags = 3
    n_forecasts = 2
    k = 4
    valid_fold_pct = 0.1
    fold_overlap_pct = 0.5
    # test three different types of crossvalidation for df_dict
    global_model_cv_options = ["global-time", "local", "intersect"]
    fold_type = {}
    single_fold = df_utils.crossvalidation_split_df(df1, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct)
    for cv_type in global_model_cv_options:
        if cv_type == "global-time":
            fold_type[cv_type] = df_utils.crossvalidation_split_df(
                df_global, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type=cv_type
            )
            # manually asserting global-time case:
            for i in range(k):
                for j in range(2):
                    aux = fold_type[cv_type][i][j].copy(deep=True)
                    assert len(aux[aux["ID"] == "df1"]) == len(single_fold[i][j])
        else:
            fold_type[cv_type] = check_folds_dict(
                df_global, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type=cv_type
            )
    # since the time range is the same in all cases all of the folds should be exactly the same no matter the
    # global_model_cv_option
    for x in global_model_cv_options:
        for y in global_model_cv_options:
            if x != y:
                assert fold_type[x][0][0].equals(fold_type[y][0][0])
    assert fold_type["global-time"][-1][0][fold_type["global-time"][-1][0]["ID"] == "df1"].equals(single_fold[-1][0])

    # Test cv for dict with time series with different time range
    list_for_global_time_assertion = [580, 639, 608, 215, 215, 215, 790, 849, 818, 215, 156, 187]
    df1 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-03-01", periods=len_df), "y": np.arange(len_df) * 3, "ID": "df1"}
    )
    df2 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df) * 5, "ID": "df2"}
    )
    df3 = pd.DataFrame(
        {"ds": pd.date_range(start="2017-02-01", periods=len_df), "y": np.arange(len_df) * 2, "ID": "df3"}
    )
    df_global = pd.concat((df1, df2, df3))
    n_lags = 5
    n_forecasts = 1
    k = 2
    valid_fold_pct = 0.2
    fold_overlap_pct = 0.0
    fold_type = {}
    for cv_type in global_model_cv_options:
        if cv_type == "global-time":
            fold_type[cv_type] = df_utils.crossvalidation_split_df(
                df_global, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type=cv_type
            )
            # manually asserting global-time case:
            cont = 0
            for i in range(k):
                for j in range(2):
                    for key in fold_type[cv_type][i][j]["ID"].unique():
                        aux = fold_type[cv_type][i][j].copy(deep=True)
                        assert len(aux[aux["ID"] == key]) == list_for_global_time_assertion[cont]
                        cont = cont + 1
        else:
            fold_type[cv_type] = check_folds_dict(
                df_global, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type=cv_type
            )
    for x in global_model_cv_options:
        for y in global_model_cv_options:
            if x != y:
                with pytest.raises(AssertionError):
                    assert fold_type[x][0][0].equals(fold_type[y][0][0])
    df_list = list()
    df_list.append(df1)
    # Raise value error for df type different than pd.DataFrame or dict
    with pytest.raises(ValueError):
        df_utils.crossvalidation_split_df(
            df_list, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type=cv_type
        )
    # Raise value error for invalid type of global_model_cv_type
    with pytest.raises(ValueError):
        df_utils.crossvalidation_split_df(
            df_global, n_lags, n_forecasts, k, valid_fold_pct, fold_overlap_pct, global_model_cv_type="invalid"
        )


def test_reg_delay():
    df = pd.read_csv(PEYTON_FILE, nrows=102)[:100]
    m = NeuralProphet(
        epochs=10,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
    )
    m.fit(df, freq="D")
    c = m.config_train
    # weight, epoch, epoch_iteration_progress
    for w, e, i in [
        (0, 0, 1),
        (0, 3, 0),
        (0, 5, 0),
        # (0.002739052315863355, 5, 0.1),
        (0.5, 6, 0.5),
        # (0.9972609476841366, 7, 0.9),
        (1, 7, 1),
        (1, 8, 0),
    ]:
        progress = float(e + i) / 10.0
        weight = c.get_reg_delay_weight(progress=progress, reg_start_pct=0.5, reg_full_pct=0.8)
        assert weight == w


def test_double_crossvalidation():
    len_df = 100
    df = pd.DataFrame({"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df), "ID": "__df__"})
    folds_val, folds_test = df_utils.double_crossvalidation_split_df(
        df=df,
        n_lags=0,
        n_forecasts=1,
        k=3,
        valid_pct=0.3,
        test_pct=0.15,
    )
    train_folds_len1 = []
    val_folds_len1 = []
    for f_train, f_val in folds_val:
        train_folds_len1.append(len(f_train))
        val_folds_len1.append(len(f_val))
    train_folds_len2 = []
    val_folds_len2 = []
    for f_train, f_val in folds_test:
        train_folds_len2.append(len(f_train))
        val_folds_len2.append(len(f_val))
    assert train_folds_len1[-1] == 75
    assert train_folds_len2[0] == 85
    assert val_folds_len1[0] == 10
    assert val_folds_len2[0] == 5

    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=2,
    )
    len_df = 100
    df = pd.DataFrame({"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df), "ID": "__df__"})
    folds_val, folds_test = m.double_crossvalidation_split_df(
        df=df,
        k=3,
        valid_pct=0.3,
        test_pct=0.15,
    )
    train_folds_len1 = []
    val_folds_len1 = []
    for f_train, f_val in folds_val:
        train_folds_len1.append(len(f_train))
        val_folds_len1.append(len(f_val))
    train_folds_len2 = []
    val_folds_len2 = []
    for f_train, f_val in folds_test:
        train_folds_len2.append(len(f_train))
        val_folds_len2.append(len(f_val))
    assert train_folds_len1[-1] == 78
    assert train_folds_len2[0] == 88
    assert val_folds_len1[0] == 12
    assert val_folds_len2[0] == 6

    # Raise not implemented error as double_crossvalidation is not compatible with many time series
    with pytest.raises(NotImplementedError):
        len_df = 100
        df = pd.DataFrame(
            {"ds": pd.date_range(start="2017-01-01", periods=len_df), "y": np.arange(len_df), "ID": "__df__"}
        )
        df1 = df.copy(deep=True)
        df1["ID"] = "df1"
        df2 = df.copy(deep=True)
        df2["ID"] = "df2"
        folds_val, folds_test = m.double_crossvalidation_split_df(
            pd.concat((df1, df2)),
            k=3,
            valid_pct=0.3,
            test_pct=0.15,
        )


def test_check_duplicate_ds():
    # Check whether a ValueError is thrown in case there
    # are duplicate dates in the ds column of dataframe
    df = pd.read_csv(PEYTON_FILE, nrows=102)[:50]
    # introduce duplicates in dataframe
    df = pd.concat([df, df[8:9]]).reset_index()
    # Check if error thrown on duplicates
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=24,
        ar_reg=0.5,
    )
    with pytest.raises(ValueError):
        m.fit(df, freq="D")


def test_infer_frequency():
    df = pd.read_csv(PEYTON_FILE, nrows=102)[:50]
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
    )
    # Check if freq is set automatically
    df_train, df_test = m.split_df(df)
    log.debug("freq automatically set")
    # Check if freq is set automatically
    df_train, df_test = m.split_df(df, freq=None)
    log.debug("freq automatically set even if set to None")
    # Check if freq is set when equal to the original
    df_train, df_test = m.split_df(df, freq="D")
    log.debug("freq is equal to ideal")
    # Check if freq is set in different freq
    df_train, df_test = m.split_df(df, freq="5D")
    log.debug("freq is set even though is different than the ideal")
    # Assert for data unevenly spaced
    index = np.unique(np.geomspace(1, 40, 20, dtype=int))
    df_uneven = df.iloc[index, :]
    with pytest.raises(ValueError):
        m.split_df(df_uneven)
    # Check if freq is set even in a df with multiple freqs
    df_train, df_test = m.split_df(df_uneven, freq="H")
    log.debug("freq is set even with not definable freq")
    # Check if freq is set for list
    df1 = df.copy(deep=True)
    df1["ID"] = "df1"
    df2 = df.copy(deep=True)
    df2["ID"] = "df2"
    df_global = pd.concat((df1, df2))
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
    )
    m.fit(df_global)
    log.debug("freq is set for list of dataframes")
    # Check if freq is set for list with different freq for n_lags=0
    time_range = pd.date_range(start="1994-12-01", periods=df.shape[0], freq="M")
    df1["ds"] = time_range
    df_global = pd.concat((df1, df2))
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=0,
    )
    m.fit(df_global)
    log.debug("freq is set for list of dataframes(n_lags=0)")
    # Assert for automatic frequency in list with different freq
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=2,
    )
    with pytest.raises(ValueError):
        m.fit(df_global)
    # Exceptions
    frequencies = ["M", "MS", "Y", "YS", "Q", "QS", "B", "BH"]
    df = df.iloc[:200, :]
    for freq in frequencies:
        df1 = df.copy(deep=True)
        time_range = pd.date_range(start="1994-12-01", periods=df.shape[0], freq=freq)
        df1["ds"] = time_range
        df_train, df_test = m.split_df(df1)
    log.debug("freq is set for all the exceptions")


def test_globaltimedataset():
    df = pd.read_csv(PEYTON_FILE, nrows=100)
    df1 = df[:50]
    df1 = df1.assign(ID="df1")
    df2 = df[50:]
    df2 = df2.assign(ID="df2")
    m1 = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        yearly_seasonality=True,
        weekly_seasonality=True,
        daily_seasonality=True,
    )
    m2 = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=3,
        n_forecasts=2,
    )
    m3 = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
    )
    # TODO m3.add_country_holidays("US")
    config_normalization = configure.Normalization("auto", False, True, False)
    for m in [m1, m2, m3]:
        df_global = pd.concat((df1, df2))
        df_global["ds"] = pd.to_datetime(df_global.loc[:, "ds"])
        config_normalization.init_data_params(
            df_global, m.config_lagged_regressors, m.config_regressors, m.config_events
        )
        m.config_normalization = config_normalization
        df_global = _normalize(df=df_global, config_normalization=m.config_normalization)
        components_stacker = utils_time_dataset.ComponentStacker(
            n_lags=m.config_ar.n_lags,
            n_forecasts=m.config_model.n_forecasts,
            max_lags=m.config_model.max_lags,
            config_seasonality=m.config_seasonality,
            lagged_regressor_config=m.config_lagged_regressors,
        )
        m._create_dataset(df_global, predict_mode=False, components_stacker=components_stacker)
        m._create_dataset(df_global, predict_mode=True, components_stacker=components_stacker)

    # lagged_regressors, future_regressors
    df4 = df.copy()
    df4["A"] = np.arange(len(df4))
    df4["B"] = np.arange(len(df4)) * 0.1
    df4["ID"] = "df4"
    df4["ds"] = pd.to_datetime(df4.loc[:, "ds"])
    m4 = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=2,
    )
    m4.add_future_regressor("A")
    m4.add_lagged_regressor("B")
    config_normalization = configure.Normalization("auto", False, True, False)
    for m in [m4]:
        df4
        config_normalization.init_data_params(df4, m.config_lagged_regressors, m.config_regressors, m.config_events)
        m.config_normalization = config_normalization
        df4 = _normalize(df=df4, config_normalization=m.config_normalization)
        components_stacker = utils_time_dataset.ComponentStacker(
            n_lags=m.config_ar.n_lags,
            n_forecasts=m.config_model.n_forecasts,
            max_lags=m.config_model.max_lags,
            config_seasonality=m.config_seasonality,
            lagged_regressor_config=m.config_lagged_regressors,
        )
        m._create_dataset(df4, predict_mode=False, components_stacker=components_stacker)
        m._create_dataset(df4, predict_mode=True, components_stacker=components_stacker)


def test_dataloader():
    df = pd.read_csv(PEYTON_FILE, nrows=100)
    df["A"] = np.arange(len(df))
    df["B"] = np.arange(len(df)) * 0.1
    df1 = df[:50]
    df1 = df1.assign(ID="df1")
    df2 = df[50:]
    df2 = df2.assign(ID="df2")
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        yearly_seasonality=True,
        weekly_seasonality=True,
        daily_seasonality=True,
        n_lags=3,
        n_forecasts=2,
    )
    m.add_future_regressor("A")
    m.add_lagged_regressor("B")
    config_normalization = configure.Normalization("auto", False, True, False)
    df_global = pd.concat((df1, df2))
    df_global["ds"] = pd.to_datetime(df_global.loc[:, "ds"])
    config_normalization.init_data_params(df_global, m.config_lagged_regressors, m.config_regressors, m.config_events)
    m.config_normalization = config_normalization
    df_global = _normalize(df=df_global, config_normalization=m.config_normalization)
    components_stacker = utils_time_dataset.ComponentStacker(
        n_lags=3,
        n_forecasts=2,
        max_lags=3,
        config_seasonality=None,
        lagged_regressor_config=None,
    )
    dataset = m._create_dataset(df_global, predict_mode=False, components_stacker=components_stacker)
    loader = DataLoader(dataset, batch_size=min(1024, len(df)), shuffle=True, drop_last=False)
    for _, meta in loader:
        assert set(meta["df_name"]) == set(df_global["ID"].unique())
        break


def test_newer_sample_weight():
    dates = pd.date_range(start="2020-01-01", periods=100, freq="D")
    a = [0, 1] * 50
    y = -1 * np.array(a[:50])
    y = np.concatenate([y, np.array(a[50:])])
    # first half: y = -a
    # second half: y = a
    df = pd.DataFrame({"ds": dates, "y": y, "a": a})

    newer_bias = 5
    m = NeuralProphet(
        epochs=10,
        batch_size=10,
        learning_rate=LR,
        newer_samples_weight=newer_bias,
        newer_samples_start=0.0,
        future_regressors_model="linear",
        # growth='off',
        n_changepoints=0,
        daily_seasonality=False,
        weekly_seasonality=False,
        yearly_seasonality=False,
    )
    m.add_future_regressor("a")
    m.fit(df)

    # test that second half dominates
    # -> positive relationship of a and y
    dates = pd.date_range(start="2020-01-01", periods=100, freq="D")
    a = [1] * 100
    y = [0] * 100
    df = pd.DataFrame({"ds": dates, "y": y, "a": a})
    forecast1 = m.predict(df[:10])
    forecast2 = m.predict(df[-10:])
    avg_a1 = np.mean(forecast1["future_regressor_a"])
    avg_a2 = np.mean(forecast2["future_regressor_a"])
    log.info(f"avg regressor a contribution first samples: {avg_a1}")
    log.info(f"avg regressor a contribution last samples: {avg_a2}")
    # must hold
    assert avg_a1 > 0.1
    assert avg_a2 > 0.1

    # this is less strict, as it also depends on trend, but should still hold
    avg_y1 = np.mean(forecast1["yhat1"])
    avg_y2 = np.mean(forecast2["yhat1"])
    log.info(f"avg yhat first samples: {avg_y1}")
    log.info(f"avg yhat last samples: {avg_y2}")
    assert avg_y1 > -0.9
    assert avg_y2 > 0.1


def test_make_future():
    df = pd.read_csv(PEYTON_FILE, nrows=100)
    df["A"] = df["y"].rolling(7, min_periods=1).mean()
    df_future_regressor = pd.DataFrame({"A": np.arange(10)})

    # without lags
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_forecasts=10,
    )
    m = m.add_future_regressor(name="A")
    future = m.make_future_dataframe(
        df,
        periods=10,
        regressors_df=df_future_regressor,
    )
    assert len(future) == 10

    df = pd.read_csv(PEYTON_FILE, nrows=100)
    df["A"] = df["y"].rolling(7, min_periods=1).mean()
    df["B"] = df["y"].rolling(30, min_periods=1).min()
    df_future_regressor = pd.DataFrame({"A": np.arange(10)})
    # with lags
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=5,
        n_forecasts=3,
    )
    m = m.add_future_regressor(name="A")
    m = m.add_lagged_regressor(names="B")
    future = m.make_future_dataframe(
        df,
        n_historic_predictions=10,
        regressors_df=df_future_regressor,
    )
    assert len(future) == 10 + 5 + 3


def test_too_many_NaN():
    n_lags = 12
    n_forecasts = 1
    config_ar = configure_components.AutoregRession(n_lags=n_lags)
    config_model = configure.Model(n_forecasts=n_forecasts)
    config_model.set_max_num_lags(n_lags)
    config_missing = configure.MissingDataHandling(
        impute_missing=True,
        impute_linear=5,
        impute_rolling=5,
        drop_missing=False,
    )
    length = 100
    days = pd.date_range(start="2017-01-01", periods=length)
    y = np.ones(length)
    # introduce large NaN value window
    y[25:50] = np.nan
    df = pd.DataFrame({"ds": days, "y": y})
    # linear imputation and rolling avg to fill some of the missing data (but not all are filled!)
    df.loc[:, "y"], remaining_na = df_utils.fill_linear_then_rolling_avg(
        df["y"],
        limit_linear=config_missing.impute_linear,
        rolling=config_missing.impute_rolling,
    )
    df, _, _, id_list = df_utils.check_multiple_series_id(df)
    df, _, _ = df_utils.check_dataframe(df)
    local_data_params, global_data_params = df_utils.init_data_params(df=df, normalize="minmax")
    df = df.drop("ID", axis=1)
    df = df_utils.normalize(df, global_data_params)
    df["ID"] = "__df__"
    # Check if ValueError is thrown, if NaN values remain after auto-imputing
    with pytest.raises(ValueError):
        components_stacker = utils_time_dataset.ComponentStacker(
            n_lags=n_lags,
            n_forecasts=n_forecasts,
            max_lags=config_model.max_lags,
            config_seasonality=None,
            lagged_regressor_config=None,
        )
        time_dataset.TimeDataset(
            df=df,
            components_stacker=components_stacker,
            predict_mode=False,
            config_model=config_model,
            config_missing=config_missing,
            config_ar=config_ar,
            config_seasonality=None,
            config_events=None,
            config_country_holidays=None,
            config_regressors=None,
            config_lagged_regressors=None,
        )


def test_future_df_with_nan():
    # Check whether an Error is thrown if df contains NaN at the end, before it is expanded to the future
    # if there are more consecutive NaN values at the end of df than n_lags: ValueError.
    m = NeuralProphet(epochs=EPOCHS, batch_size=BATCH_SIZE, learning_rate=LR, n_lags=12, n_forecasts=10)
    length = 100
    y = np.random.randint(0, 100, size=length)
    days = pd.date_range(start="2017-01-01", periods=length)
    df = pd.DataFrame({"ds": days, "y": y})
    # introduce 15 NaN values at the end of df. Now #NaN at end > n_lags
    df.iloc[-15:, 1] = np.nan
    m.fit(df, freq="D")
    with pytest.raises(ValueError):
        m.make_future_dataframe(df, periods=10, n_historic_predictions=5)


def test_join_dfs_after_data_drop():
    log.info("Testing inner join of input df and forecast df")
    df = pd.DataFrame()
    df["ds"] = pd.date_range(start="2010-01-01", end="2010-05-01")
    df["y"] = range(0, len(df["ds"]))

    fcst = pd.DataFrame()
    fcst["time"] = pd.date_range(start="2009-12-01", end="2010-02-01")
    fcst["y"] = range(len(fcst["time"]))

    # dfs are not merged into one df
    fcst, df = df_utils.join_dfs_after_data_drop(fcst, df)

    # merge into one df
    df_utils.join_dfs_after_data_drop(fcst, df, merge=True)


def test_ffill_in_future_df():
    # If df contains NaN at the end (before it is expanded to the future): perform forward-filling
    # The user should get a warning, because forward-filling might affect forecast quality
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=12,
        n_forecasts=10,
    )
    length = 100
    y = np.random.randint(0, 100, size=length)
    days = pd.date_range(start="2017-01-01", periods=length)
    df = pd.DataFrame({"ds": days, "y": y})
    # introduce some NaN values at the end of df, before expanding it to the future
    df.iloc[-5:, 1] = np.nan
    m.fit(df, freq="D")
    m.make_future_dataframe(df, periods=10, n_historic_predictions=5)


def test_handle_negative_values_remove():
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    # Insert a negative value
    df.loc[0, "y"] = -1
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=3,
        impute_missing=False,
        drop_missing=False,
    )
    df_ = m.handle_negative_values(df, handle="remove")
    assert len(df_) == len(df) - 1


def test_handle_negative_values_error():
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    # Insert a negative value
    df.loc[0, "y"] = -1
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=3,
        impute_missing=False,
        drop_missing=False,
    )
    with pytest.raises(ValueError):
        m.handle_negative_values(df, handle="error")


def test_handle_negative_values_replace():
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    # Insert a negative value
    df.loc[0, "y"] = -1
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
        n_lags=3,
        impute_missing=False,
        drop_missing=False,
    )
    df_ = m.handle_negative_values(df, handle=0.0)
    assert df_.loc[0, "y"] == 0.0


def test_float32_inputs():
    # test if float32 inputs are forecasted as float32 outputs
    df = pd.read_csv(PEYTON_FILE, nrows=NROWS)
    df["y"] = df["y"].astype(np.float32)
    m = NeuralProphet(
        epochs=EPOCHS,
        batch_size=BATCH_SIZE,
        learning_rate=LR,
    )
    m.fit(df, freq="D")
    forecast = m.predict(df)
    assert forecast["yhat1"].dtype == np.float32
