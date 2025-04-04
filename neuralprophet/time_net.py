import logging
from collections import OrderedDict
from functools import reduce
from typing import Dict, List, Optional, Union

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torchmetrics

from neuralprophet import configure, configure_components, np_types
from neuralprophet.components.router import get_future_regressors, get_seasonality, get_trend
from neuralprophet.utils import (
    check_for_regularization,
    config_events_to_model_dims,
    reg_func_events,
    reg_func_regressors,
    reg_func_season,
    reg_func_seasonality_glocal,
    reg_func_trend,
    reg_func_trend_glocal,
)
from neuralprophet.utils_torch import init_parameter, interprete_model

log = logging.getLogger("NP.time_net")


class TimeNet(pl.LightningModule):
    """Linear time regression fun and some not so linear fun.
    A modular model that models classic time-series components
        * trend
        * seasonality
        * auto-regression (as AR-Net)
        * covariates (as AR-Net)
        * apriori regressors
        * events and holidays
    by using Neural Network components.
    The Auto-regression and covariate components can be configured as a deeper network (AR-Net).
    """

    def __init__(
        self,
        config_model: configure.Model,
        config_seasonality: configure_components.Seasonalities,
        config_train: Optional[configure.Train] = None,
        config_trend: Optional[configure_components.Trend] = None,
        config_ar: Optional[configure_components.AutoregRession] = None,
        config_normalization: Optional[configure.Normalization] = None,
        config_lagged_regressors: Optional[configure_components.LaggedRegressors] = None,
        config_regressors: Optional[configure_components.FutureRegressors] = None,
        config_events: Optional[configure_components.Events] = None,
        config_holidays: Optional[configure_components.Holidays] = None,
        n_forecasts: int = 1,
        n_lags: int = 0,
        ar_layers: Optional[List[int]] = [],
        metrics: Optional[np_types.CollectMetricsMode] = {},
        id_list: List[str] = ["__df__"],
        num_trends_modelled: int = 1,
        num_seasonalities_modelled: int = 1,
        num_seasonalities_modelled_dict: dict = None,
        meta_used_in_model: bool = False,
    ):
        """
        Parameters
        ----------
            quantiles : list
                the set of quantiles estimated
            config_train : configure.Train
            config_trend : configure_components.Trend
            config_seasonality : configure_components.Seasonalities
            config_ar : configure_components.AutoregRession
            config_lagged_regressors : configure_components.LaggedRegressors
                Configurations for lagged regressors
            config_regressors : configure_components.FutureRegressors
                Configs of regressors with mode and index.
            config_events : configure_components.Events
            config_holidays : OrderedDict
            config_normalization: OrderedDict
            n_forecasts : int
                number of steps to forecast. Aka number of model outputs
            n_lags : int
                number of previous steps of time series used as input (aka AR-order)
                Note
                ----
                The default value is ``0``, which initializes no auto-regression.

            ar_layers : list
                List of hidden layers (for AR-Net).

                Note
                ----
                The default value is ``[]``, which initializes no hidden layers.

            metrics : dict
                Dictionary of torchmetrics to be used during training and for evaluation.
            id_list : list
                List of different time series IDs, used for global-local modelling (if enabled)
                Note
                ----
                This parameter is set to  ``['__df__']`` if only one time series is input.
            num_trends_modelled : int
                Number of different trends modelled.
                Note
                ----
                If only 1 time series is modelled, it will be always 1.
                Note
                ----
                For multiple time series. If trend is modelled globally the value is set
                to 1, otherwise it is set to the number of time series modelled.
            num_seasonalities_modelled : int
                Number of different seasonalities modelled.
                Note
                ----
                If only 1 time series is modelled, it will be always 1.
                Note
                ----
                For multiple time series. If seasonality is modelled globally the value is set
                to 1, otherwise it is set to the number of time series modelled.
            meta_used_in_model : boolean
                Whether we need to know the time series ID when we interact with the Model.
                Note
                ----
                Will be set to ``True`` if more than one component is modelled locally.
        """
        super().__init__()

        # Store hyerparameters in model checkpoint
        # TODO: causes a RuntimeError under certain conditions, investigate and handle better
        try:
            self.save_hyperparameters()
        except RuntimeError:
            pass

        # General
        self.config_model = config_model
        self.config_model.n_forecasts = n_forecasts

        # Components stackers to unpack the input tensor
        self.components_stacker = {
            "train": None,
            "val": None,
            "test": None,
            "predict": None,
        }
        # Lightning Config
        self.config_train = config_train
        self.config_normalization = config_normalization
        self.include_components = False  # flag to indicate if we are in include_components mode, set in prodiction mode by set_compute_components
        self.config_model = config_model

        # Manual optimization: we are responsible for calling .backward(), .step(), .zero_grad().
        self.automatic_optimization = False

        # Hyperparameters (can be tuned using trainer.tune())
        self.learning_rate = self.config_train.learning_rate
        self.batch_size = self.config_train.batch_size
        self.finding_lr = False  # flag to indicate if we are in lr finder mode

        # Metrics Config
        self.metrics_enabled = bool(metrics)  # yields True if metrics is not an empty dictionary
        if self.metrics_enabled:
            metrics = {metric: torchmetrics.__dict__[metrics[metric][0]](**metrics[metric][1]) for metric in metrics}
            self.log_args = {
                "on_step": False,
                "on_epoch": True,
                "prog_bar": True,
                "batch_size": self.config_train.batch_size,
            }
            self.metrics_train = torchmetrics.MetricCollection(metrics=metrics)
            self.metrics_val = torchmetrics.MetricCollection(metrics=metrics, postfix="_val")

        # For Multiple Time Series Analysis
        self.id_list = id_list
        self.id_dict = dict((key, i) for i, key in enumerate(id_list))
        self.num_trends_modelled = num_trends_modelled
        self.num_seasonalities_modelled = num_seasonalities_modelled
        self.num_seasonalities_modelled_dict = num_seasonalities_modelled_dict
        self.meta_used_in_model = meta_used_in_model

        # Regularization
        self.reg_enabled = check_for_regularization(
            [
                config_seasonality,
                config_regressors,
                config_lagged_regressors,
                config_ar,
                config_events,
                config_trend,
                config_holidays,
            ]
        )

        # Quantiles
        self.quantiles = self.config_model.quantiles

        # Trend
        self.config_trend = config_trend
        self.trend = get_trend(
            config=config_trend,
            id_list=id_list,
            quantiles=self.quantiles,
            num_trends_modelled=num_trends_modelled,
            n_forecasts=n_forecasts,
            device=self.device,
        )

        # Seasonalities
        self.config_seasonality = config_seasonality
        # Error handling
        if self.config_seasonality is not None:
            if self.config_seasonality.mode == "multiplicative" and self.config_trend is None:
                raise ValueError("Multiplicative seasonality requires trend.")
            if self.config_seasonality.mode not in ["additive", "multiplicative"]:
                raise ValueError(f"Seasonality Mode {self.config_seasonality.mode} not implemented.")
            # Initialize seasonality
            self.seasonality = get_seasonality(
                config=config_seasonality,
                id_list=id_list,
                quantiles=self.quantiles,
                num_seasonalities_modelled=num_seasonalities_modelled,
                num_seasonalities_modelled_dict=num_seasonalities_modelled_dict,
                n_forecasts=n_forecasts,
                device=self.device,
            )

        # Events
        self.config_events = config_events
        self.config_holidays = config_holidays
        self.events_dims = config_events_to_model_dims(self.config_events, self.config_holidays)
        if self.events_dims is not None:
            n_additive_event_params = 0
            n_multiplicative_event_params = 0
            for event, configs in self.events_dims.items():
                if configs["mode"] not in ["additive", "multiplicative"]:
                    log.error("Event Mode {} not implemented. Defaulting to 'additive'.".format(configs["mode"]))
                    self.events_dims[event]["mode"] = "additive"
                if configs["mode"] == "additive":
                    n_additive_event_params += len(configs["event_indices"])
                elif configs["mode"] == "multiplicative":
                    if self.config_trend is None:
                        log.error("Multiplicative events require trend.")
                        raise ValueError
                    n_multiplicative_event_params += len(configs["event_indices"])
            self.event_params = nn.ParameterDict(
                {
                    # dimensions - [no. of quantiles, no. of additive events]
                    "additive": init_parameter(dims=[len(self.quantiles), n_additive_event_params]),
                    # dimensions - [no. of quantiles, no. of multiplicative events]
                    "multiplicative": init_parameter(dims=[len(self.quantiles), n_multiplicative_event_params]),
                }
            )
        else:
            self.config_events = None
            self.config_holidays = None

        # Autoregression
        self.config_ar = config_ar
        self.n_lags = n_lags
        self.ar_layers = ar_layers
        if self.n_lags > 0:
            ar_net_layers = []
            d_inputs = self.n_lags
            for d_hidden_i in self.ar_layers:
                ar_net_layers.append(nn.Linear(d_inputs, d_hidden_i, bias=True))
                ar_net_layers.append(nn.ReLU())
                d_inputs = d_hidden_i
            # final layer has input size d_inputs and output size equal to no. of forecasts * no. of quantiles
            ar_net_layers.append(nn.Linear(d_inputs, self.config_model.n_forecasts * len(self.quantiles), bias=False))
            self.ar_net = nn.Sequential(*ar_net_layers)
            for lay in self.ar_net:
                if isinstance(lay, nn.Linear):
                    nn.init.kaiming_normal_(lay.weight, mode="fan_in")

        # Lagged regressors
        self.config_lagged_regressors = config_lagged_regressors
        if self.config_lagged_regressors is not None and self.config_lagged_regressors.regressors is not None:
            covar_net_layers = []
            d_inputs = sum([covar.n_lags for _, covar in self.config_lagged_regressors.regressors.items()])
            for d_hidden_i in self.config_lagged_regressors.layers:
                covar_net_layers.append(nn.Linear(d_inputs, d_hidden_i, bias=True))
                covar_net_layers.append(nn.ReLU())
                d_inputs = d_hidden_i
            covar_net_layers.append(
                nn.Linear(d_inputs, self.config_model.n_forecasts * len(self.quantiles), bias=False)
            )
            self.covar_net = nn.Sequential(*covar_net_layers)
            for lay in self.covar_net:
                if isinstance(lay, nn.Linear):
                    nn.init.kaiming_normal_(lay.weight, mode="fan_in")

        # Regressors
        self.config_regressors = config_regressors
        if self.config_regressors.regressors is not None:
            # Initialize future_regressors
            self.future_regressors = get_future_regressors(
                config=config_regressors,
                id_list=id_list,
                quantiles=self.quantiles,
                n_forecasts=n_forecasts,
                device=self.device,
                config_trend_none_bool=self.config_trend is None,
            )
        else:
            self.config_regressors.regressors = None

    @property
    def ar_weights(self) -> torch.Tensor:
        """sets property auto-regression weights for regularization. Update if AR is modelled differently"""
        # TODO: this is wrong for deep networks, use utils_torch.interprete_model
        for layer in self.ar_net:
            if isinstance(layer, nn.Linear):
                return layer.weight

    def set_components_stacker(self, stacker, mode):
        """Set the components stacker for the given mode.
        Parameters
        ----------
        components_stacker : ComponentStacker
            The components stacker to be set.
        mode : str
            The mode for which the components stacker is to be set
            options: ["train", "val", "test", "predict"]
        """
        modes = ["train", "val", "test", "predict"]
        assert mode in modes, f"mode must be one of {modes}"
        self.components_stacker[mode] = stacker

    def get_covar_weights(self, covar_input=None) -> torch.Tensor:
        """
        Get attributions of covariates network w.r.t. the model input.
        """
        if self.config_lagged_regressors is not None and self.config_lagged_regressors.regressors is not None:
            # Accumulate the lags of the covariates
            covar_splits = np.add.accumulate(
                [covar.n_lags for _, covar in self.config_lagged_regressors.regressors.items()][:-1]
            ).tolist()
            # If actual covariates are provided, use them to compute the attributions
            if covar_input is not None:
                covar_input = torch.cat([covar for _, covar in covar_input.items()], axis=1)
            # Calculate the attributions w.r.t. the inputs
            if self.config_lagged_regressors.layers == []:
                attributions = self.covar_net[0].weight
            else:
                attributions = interprete_model(self, "covar_net", "forward_covar_net", covar_input)
            # Split the attributions into the different covariates
            attributions_split = torch.tensor_split(
                attributions,
                covar_splits,
                axis=1,
            )
            # Combine attributions and covariate name
            covar_attributions = dict(zip(self.config_lagged_regressors.regressors.keys(), attributions_split))
        else:
            covar_attributions = None
        return covar_attributions

    def set_covar_weights(self, covar_weights: torch.Tensor):
        """
        Function to set the covariate weights for later interpretation in compute_components.
        This function is needed since the gradient information is not available during the predict_step
        method and attributions cannot be calculated in compute_components.

        :param covar_weights: _description_
        :type covar_weights: torch.Tensor
        """
        self.covar_weights = covar_weights

    def get_event_weights(self, name: str) -> Dict[str, torch.Tensor]:
        """
        Retrieve the weights of event features given the name
        Parameters
        ----------
            name : str
                Event name
        Returns
        -------
            OrderedDict
                Dict of the weights of all offsets corresponding to a particular event
        """

        event_dims = self.events_dims[name]
        mode = event_dims["mode"]

        if mode == "multiplicative":
            event_params = self.event_params["multiplicative"]
        else:
            assert mode == "additive"
            event_params = self.event_params["additive"]

        event_param_dict = OrderedDict({})
        for event_delim, indices in zip(event_dims["event_delim"], event_dims["event_indices"]):
            event_param_dict[event_delim] = event_params[:, indices : (indices + 1)]
        return event_param_dict

    def _compute_quantile_forecasts_from_diffs(self, diffs: torch.Tensor, predict_mode: bool = False) -> torch.Tensor:
        """
        Computes the actual quantile forecasts from quantile differences estimated from the model
        Args:
            diffs : torch.Tensor
                tensor of dims (batch, n_forecasts, no_quantiles) which
                contains the median quantile forecasts as well as the diffs of other quantiles
                from the median quantile
            predict_mode : bool
                boolean variable indicating whether the model is in prediction mode
        Returns:
            dim (batch, n_forecasts, no_quantiles)
                final forecasts
        """

        if len(self.quantiles) <= 1:
            return diffs
        # generate the actual quantile forecasts from predicted differences
        if any(quantile > 0.5 for quantile in self.quantiles):
            quantiles_divider_index = next(i for i, quantile in enumerate(self.quantiles) if quantile > 0.5)
        else:
            quantiles_divider_index = len(self.quantiles)

        n_upper_quantiles = diffs.shape[-1] - quantiles_divider_index
        n_lower_quantiles = quantiles_divider_index - 1

        out = torch.zeros_like(diffs)
        out[:, :, 0] = diffs[:, :, 0]  # set the median where 0 is the median quantile index

        if n_upper_quantiles > 0:  # check if upper quantiles exist
            upper_quantile_diffs = diffs[:, :, quantiles_divider_index:]
            if predict_mode:  # check for quantile crossing and correct them in predict mode
                upper_quantile_diffs[:, :, 0] = torch.max(
                    torch.tensor(0, device=self.device), upper_quantile_diffs[:, :, 0]
                )
                for i in range(n_upper_quantiles - 1):
                    next_diff = upper_quantile_diffs[:, :, i + 1]
                    diff = upper_quantile_diffs[:, :, i]
                    upper_quantile_diffs[:, :, i + 1] = torch.max(next_diff, diff)
            out[:, :, quantiles_divider_index:] = (
                upper_quantile_diffs + diffs[:, :, 0].unsqueeze(dim=2).repeat(1, 1, n_upper_quantiles).detach()
            )  # set the upper quantiles

        if n_lower_quantiles > 0:  # check if lower quantiles exist
            lower_quantile_diffs = diffs[:, :, 1:quantiles_divider_index]
            if predict_mode:  # check for quantile crossing and correct them in predict mode
                lower_quantile_diffs[:, :, -1] = torch.max(
                    torch.tensor(0, device=self.device), lower_quantile_diffs[:, :, -1]
                )
                for i in range(n_lower_quantiles - 1, 0, -1):
                    next_diff = lower_quantile_diffs[:, :, i - 1]
                    diff = lower_quantile_diffs[:, :, i]
                    lower_quantile_diffs[:, :, i - 1] = torch.max(next_diff, diff)
            lower_quantile_diffs = -lower_quantile_diffs
            out[:, :, 1:quantiles_divider_index] = (
                lower_quantile_diffs + diffs[:, :, 0].unsqueeze(dim=2).repeat(1, 1, n_lower_quantiles).detach()
            )  # set the lower quantiles

        return out

    def scalar_features_effects(self, features: torch.Tensor, params: nn.Parameter, indices=None) -> torch.Tensor:
        """
        Computes events component of the model
        Parameters
        ----------
            features : torch.Tensor, float
                Features (either additive or multiplicative) related to event component dims (batch, n_forecasts,
                n_features)
            params : nn.Parameter
                Params (either additive or multiplicative) related to events dims (n_quantiles, n_features)
            indices : list of int
                Indices in the feature tensors related to a particular event
        Returns
        -------
            torch.Tensor
                Forecast component of dims (batch, n_forecasts, n_quantiles)
        """
        if indices is not None:
            features = features[:, :, indices]
            params = params[:, indices]
        # features dims: (batch, n_forecasts, n_features)  -> (batch, n_forecasts, 1, n_features)
        # params dims: (n_quantiles, n_features) -> (batch, 1, n_quantiles, n_features)
        out = torch.sum(features.unsqueeze(dim=2) * params.unsqueeze(dim=0).unsqueeze(dim=0), dim=-1)
        return out  # dims (batch, n_forecasts, n_quantiles)

    def auto_regression(self, lags: Union[torch.Tensor, float]) -> torch.Tensor:
        """Computes auto-regessive model component AR-Net.
        Parameters
        ----------
            lags  : torch.Tensor, float
                Previous times series values, dims: (batch, n_lags)
        Returns
        -------
            torch.Tensor
                Forecast component of dims: (batch, n_forecasts)
        """
        x = self.ar_net(lags)
        # segment the last dimension to match the quantiles
        x = x.view(x.shape[0], self.config_model.n_forecasts, len(self.quantiles))
        return x

    def forward_covar_net(self, covariates):
        """Compute all covariate components.
        Parameters
        ----------
            covariates : dict(torch.Tensor, float)
                dict of named covariates (keys) with their features (values)
                dims of each dict value: (batch, n_lags)
        Returns
        -------
            torch.Tensor
                Forecast component of dims (batch, n_forecasts, quantiles)
        """
        # Concat covariates into one tensor)
        if isinstance(covariates, dict):
            x = torch.cat([covar for _, covar in covariates.items()], axis=1)
        else:
            x = covariates
        x = self.covar_net(x)
        # segment the last dimension to match the quantiles
        x = x.view(x.shape[0], self.config_model.n_forecasts, len(self.quantiles))
        return x

    def forward(
        self,
        input_tensor: torch.Tensor,
        mode: str,
        meta: Dict = None,
    ) -> torch.Tensor:
        """This method defines the model forward pass.
        Parameters
        ----------
            input_tensor : torch.Tensor
                Input tensor of dims (batch, n_lags + n_forecasts, n_features)
            mode : str operation mode ["train", "val", "test", "predict"]
            meta : dict Static features of the time series
        Returns
        -------
            torch.Tensor Forecast tensor of dims (batch, n_forecasts, n_quantiles)
            dict of components of the model if self.include_components is True,
                each of dims (batch, n_forecasts, n_quantiles)

        """

        time_input = self.components_stacker[mode].unstack(component_name="time", batch_tensor=input_tensor)
        # Handle meta argument
        if meta is None and self.meta_used_in_model:
            name_id_dummy = self.id_list[0]
            meta = OrderedDict()
            meta["df_name"] = [name_id_dummy for _ in range(time_input.shape[0])]
            meta = torch.tensor([self.id_dict[i] for i in meta["df_name"]], device=self.device)

        # Initialize components and nonstationary tensors
        components = {}
        additive_components = torch.zeros(
            size=(time_input.shape[0], self.config_model.n_forecasts, len(self.quantiles)),
            device=self.device,
        )
        additive_components_nonstationary = torch.zeros(
            size=(time_input.shape[0], time_input.shape[1], len(self.quantiles)),
            device=self.device,
        )
        multiplicative_components_nonstationary = torch.zeros(
            size=(time_input.shape[0], time_input.shape[1], len(self.quantiles)),
            device=self.device,
        )

        # Unpack time feature and compute trend
        trend = self.trend(t=time_input, meta=meta)
        components["trend"] = trend

        # Unpack and process seasonalities
        seasonalities_input = None
        if self.config_seasonality and self.config_seasonality.periods:
            seasonalities_input = self.components_stacker[mode].unstack(
                component_name="seasonalities", batch_tensor=input_tensor
            )
            s = self.seasonality(s=seasonalities_input, meta=meta)
            if self.config_seasonality.mode == "additive":
                additive_components_nonstationary += s
            elif self.config_seasonality.mode == "multiplicative":
                multiplicative_components_nonstationary += s
            components["seasonalities"] = s

        # Unpack and process events
        additive_events_input = None
        multiplicative_events_input = None
        if self.events_dims is not None:
            if "additive_events" in self.components_stacker[mode].feature_indices:
                additive_events_input = self.components_stacker[mode].unstack(
                    component_name="additive_events", batch_tensor=input_tensor
                )
                additive_events = self.scalar_features_effects(additive_events_input, self.event_params["additive"])
                additive_components_nonstationary += additive_events
                components["additive_events"] = additive_events
            if "multiplicative_events" in self.components_stacker[mode].feature_indices:
                multiplicative_events_input = self.components_stacker[mode].unstack(
                    component_name="multiplicative_events", batch_tensor=input_tensor
                )
                multiplicative_events = self.scalar_features_effects(
                    multiplicative_events_input, self.event_params["multiplicative"]
                )
                multiplicative_components_nonstationary += multiplicative_events
                components["multiplicative_events"] = multiplicative_events

        # Unpack and process regressors
        additive_regressors_input = None
        multiplicative_regressors_input = None
        if "additive_regressors" in self.components_stacker[mode].feature_indices:
            additive_regressors_input = self.components_stacker[mode].unstack(
                component_name="additive_regressors", batch_tensor=input_tensor
            )
            additive_regressors = self.future_regressors(additive_regressors_input, "additive")
            additive_components_nonstationary += additive_regressors
            components["additive_regressors"] = additive_regressors
        if "multiplicative_regressors" in self.components_stacker[mode].feature_indices:
            multiplicative_regressors_input = self.components_stacker[mode].unstack(
                component_name="multiplicative_regressors", batch_tensor=input_tensor
            )
            multiplicative_regressors = self.future_regressors(multiplicative_regressors_input, "multiplicative")
            multiplicative_components_nonstationary += multiplicative_regressors
            components["multiplicative_regressors"] = multiplicative_regressors

        # Unpack and process lags
        lags_input = None
        if "lags" in self.components_stacker[mode].feature_indices:
            lags_input = self.components_stacker[mode].unstack(component_name="lags", batch_tensor=input_tensor)
            nonstationary_components = (
                trend[:, : self.n_lags, 0]
                + additive_components_nonstationary[:, : self.n_lags, 0]
                + trend[:, : self.n_lags, 0].detach() * multiplicative_components_nonstationary[:, : self.n_lags, 0]
            )
            stationarized_lags = lags_input - nonstationary_components
            lags = self.auto_regression(lags=stationarized_lags)
            additive_components += lags
            components["lags"] = lags

        # Unpack and process covariates
        covariates_input = None
        if self.config_lagged_regressors and self.config_lagged_regressors.regressors is not None:
            covariates_input = self.components_stacker[mode].unstack(
                component_name="lagged_regressors", batch_tensor=input_tensor
            )
            covariates = self.forward_covar_net(covariates=covariates_input)
            additive_components += covariates
            components["covariates"] = covariates

        # Combine components and compute predictions
        predictions_nonstationary = (
            trend[:, self.n_lags : time_input.shape[1], :]
            + additive_components_nonstationary[:, self.n_lags : time_input.shape[1], :]
            + trend[:, self.n_lags : time_input.shape[1], :].detach()
            * multiplicative_components_nonstationary[:, self.n_lags : time_input.shape[1], :]
        )
        prediction = predictions_nonstationary + additive_components

        # Correct crossing quantiles
        prediction_with_quantiles = self._compute_quantile_forecasts_from_diffs(
            diffs=prediction, predict_mode=mode != "train"
        )

        # Compute components if required
        if self.include_components:
            components = self.compute_components(
                time_input,
                seasonalities_input,
                lags_input,
                covariates_input,
                additive_events_input,
                multiplicative_events_input,
                additive_regressors_input,
                multiplicative_regressors_input,
                components,
                meta,
            )
        else:
            components = None

        return prediction_with_quantiles, components

    def compute_components(
        self,
        time_input,
        seasonality_input,
        lags_input,
        covariates_input,
        additive_events_input,
        multiplicative_events_input,
        additive_regressors_input,
        multiplicative_regressors_input,
        components_raw: Dict,
        meta: Dict,
    ) -> Dict:
        components = {}

        components["trend"] = components_raw["trend"][:, self.n_lags : time_input.shape[1], :]
        if self.config_trend is not None and seasonality_input is not None:
            for name, features in seasonality_input.items():
                components[f"season_{name}"] = self.seasonality.compute_fourier(
                    features=features[:, self.n_lags : time_input.shape[1], :], name=name, meta=meta
                )
        if self.n_lags > 0 and lags_input is not None:
            components["ar"] = components_raw["lags"]
        if self.config_lagged_regressors is not None and covariates_input is not None:
            # Combined forward pass
            all_covariates = components_raw["covariates"]
            # Calculate the contribution of each covariate on each forecast
            covar_attributions = self.covar_weights
            # Sum the contributions of all covariates
            covar_attribution_sum_per_forecast = reduce(
                torch.add, [torch.sum(covar, axis=1) for _, covar in covar_attributions.items()]
            ).to(all_covariates.device)
            for name in covariates_input.keys():
                # Distribute the contribution of the current covariate to the combined forward pass
                # 1. Calculate the relative share of each covariate on the total attributions
                # 2. Multiply the relative share with the combined forward pass
                components[f"lagged_regressor_{name}"] = torch.multiply(
                    all_covariates,
                    torch.divide(
                        torch.sum(covar_attributions[name], axis=1).to(all_covariates.device),
                        covar_attribution_sum_per_forecast,
                    ).reshape(self.config_model.n_forecasts, len(self.quantiles)),
                )
        if self.config_events is not None or self.config_holidays is not None:
            if additive_events_input is not None:
                components["events_additive"] = components_raw["additive_events"][
                    :, self.n_lags : time_input.shape[1], :
                ]
            if multiplicative_events_input is not None:
                components["events_multiplicative"] = components_raw["multiplicative_events"][
                    :, self.n_lags : time_input.shape[1], :
                ]
            for event, configs in self.events_dims.items():
                mode = configs["mode"]
                indices = configs["event_indices"]
                if mode == "additive":
                    features = additive_events_input[:, self.n_lags : time_input.shape[1], :]
                    params = self.event_params["additive"]
                else:
                    features = multiplicative_events_input[:, self.n_lags : time_input.shape[1], :]
                    params = self.event_params["multiplicative"]
                components[f"event_{event}"] = self.scalar_features_effects(
                    features=features, params=params, indices=indices
                )
        if self.config_regressors.regressors is not None:
            if additive_regressors_input is not None:
                components["future_regressors_additive"] = components_raw["additive_regressors"][
                    :, self.n_lags : time_input.shape[1], :
                ]

            if multiplicative_regressors_input is not None:
                components["future_regressors_multiplicative"] = components_raw["multiplicative_regressors"][
                    :, self.n_lags : time_input.shape[1], :
                ]

            for regressor, configs in self.future_regressors.regressors_dims.items():
                mode = configs["mode"]
                index = []
                index.append(configs["regressor_index"])
                if mode == "additive" and additive_regressors_input is not None:
                    components[f"future_regressor_{regressor}"] = self.future_regressors(
                        additive_regressors_input[:, self.n_lags : time_input.shape[1], :], mode, indeces=index
                    )
                if mode == "multiplicative" and multiplicative_regressors_input is not None:
                    components[f"future_regressor_{regressor}"] = self.future_regressors(
                        multiplicative_regressors_input[:, self.n_lags : time_input.shape[1], :], mode, indeces=index
                    )
        return components

    def set_compute_components(self, include_components):
        self.prev_include_components = self.include_components
        self.include_components = include_components

    def reset_compute_components(self):
        self.include_components = self.prev_include_components

    def loss_func(self, time, predicted, targets):
        loss = None
        # Compute loss. no reduction.
        loss = self.config_train.loss_func(predicted, targets)
        if self.config_train.newer_samples_weight > 1.0:
            # Weigh newer samples more.
            loss = loss * self._get_time_based_sample_weight(t=time[:, self.n_lags :])
        loss = loss.sum(dim=2).mean()
        # Regularize.
        if self.reg_enabled and not self.finding_lr:
            loss, reg_loss = self._add_batch_regularizations(loss, self.train_progress)
        else:
            reg_loss = torch.tensor(0.0, device=self.device)
        return loss, reg_loss

    def training_step(self, batch, batch_idx):
        inputs_tensor, meta = batch

        epoch_float = self.trainer.current_epoch + batch_idx / float(self.train_steps_per_epoch)
        self.train_progress = epoch_float / float(self.config_train.epochs)
        targets = self.components_stacker["train"].unstack("targets", batch_tensor=inputs_tensor)
        time = self.components_stacker["train"].unstack("time", batch_tensor=inputs_tensor)
        # Global-local
        if self.meta_used_in_model:
            meta_name_tensor = torch.tensor([self.id_dict[i] for i in meta["df_name"]], device=self.device)
        else:
            meta_name_tensor = None
        # Run forward calculation
        predicted, _ = self.forward(inputs_tensor, mode="train", meta=meta_name_tensor)
        # Store predictions in self for later network visualization
        self.train_epoch_prediction = predicted
        # Calculate loss
        loss, reg_loss = self.loss_func(time, predicted, targets)

        # Optimization
        optimizer = self.optimizers()
        optimizer.zero_grad()
        self.manual_backward(loss)
        optimizer.step()

        scheduler = self.lr_schedulers()
        if self.finding_lr:
            scheduler.step()
        else:
            scheduler.step(epoch=epoch_float)

        if self.finding_lr:
            # Manually track the loss for the lr finder
            self.log("train_loss", loss, on_step=False, on_epoch=True, prog_bar=True, logger=True)
            # self.log("reg_loss", reg_loss, on_step=False, on_epoch=True, prog_bar=True, logger=True)

        # Metrics
        if self.metrics_enabled and not self.finding_lr:
            predicted_denorm = self.denormalize(predicted[:, :, 0])
            target_denorm = self.denormalize(targets.squeeze(dim=2))
            target_denorm = target_denorm.contiguous()
            self.log_dict(self.metrics_train(predicted_denorm, target_denorm), **self.log_args)
            self.log("Loss", loss, **self.log_args)
            self.log("RegLoss", reg_loss, **self.log_args)
            # self.log("TrainProgress", self.train_progress, **self.log_args)
            self.log("LR", scheduler.get_last_lr()[0], **self.log_args)
        return loss

    def validation_step(self, batch, batch_idx):
        inputs_tensor, meta = batch
        targets = self.components_stacker["val"].unstack("targets", batch_tensor=inputs_tensor)
        time = self.components_stacker["val"].unstack("time", batch_tensor=inputs_tensor)
        # Global-local
        if self.meta_used_in_model:
            meta_name_tensor = torch.tensor([self.id_dict[i] for i in meta["df_name"]], device=self.device)
        else:
            meta_name_tensor = None
        # Run forward calculation
        predicted, _ = self.forward(inputs_tensor, mode="val", meta=meta_name_tensor)
        # Calculate loss
        loss, reg_loss = self.loss_func(time, predicted, targets)
        # Metrics
        if self.metrics_enabled:
            predicted_denorm = self.denormalize(predicted[:, :, 0])
            target_denorm = self.denormalize(targets.squeeze(dim=2))
            target_denorm = target_denorm.contiguous()
            self.log_dict(self.metrics_val(predicted_denorm, target_denorm), **self.log_args)
            self.log("Loss_val", loss, **self.log_args)
            self.log("RegLoss_val", reg_loss, **self.log_args)

    def test_step(self, batch, batch_idx):
        inputs_tensor, meta = batch
        targets = self.components_stacker["test"].unstack("targets", batch_tensor=inputs_tensor)
        time = self.components_stacker["test"].unstack("time", batch_tensor=inputs_tensor)
        # Global-local
        if self.meta_used_in_model:
            meta_name_tensor = torch.tensor([self.id_dict[i] for i in meta["df_name"]], device=self.device)
        else:
            meta_name_tensor = None
        # Run forward calculation
        predicted, _ = self.forward(inputs_tensor, mode="test", meta=meta_name_tensor)
        # Calculate loss
        loss, reg_loss = self.loss_func(time, predicted, targets)
        # Metrics
        if self.metrics_enabled:
            predicted_denorm = self.denormalize(predicted[:, :, 0])
            target_denorm = self.denormalize(targets.squeeze(dim=2))
            # target_denorm = target_denorm.detach().clone()
            target_denorm = target_denorm.contiguous()
            self.log_dict(self.metrics_val(predicted_denorm, target_denorm), **self.log_args)
            self.log("Loss_test", loss, **self.log_args)
            self.log("RegLoss_test", reg_loss, **self.log_args)

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        inputs_tensor, meta = batch
        # Global-local
        if self.meta_used_in_model:
            meta_name_tensor = torch.tensor([self.id_dict[i] for i in meta["df_name"]], device=self.device)
        else:
            meta_name_tensor = None

        # Run forward calculation
        prediction, components = self.forward(
            inputs_tensor,
            mode="predict",
            meta=meta_name_tensor,
        )
        return prediction, components

    def configure_optimizers(self):
        self.train_steps_per_epoch = self.config_train.batches_per_epoch
        # self.trainer.num_training_batches = self.train_steps_per_epoch * self.config_train.epochs

        self.config_train.set_optimizer()
        self.config_train.set_scheduler()

        # Optimizer
        if self.finding_lr and self.learning_rate is None:
            self.learning_rate = 0.1
        optimizer = self.config_train.optimizer(
            self.parameters(),
            lr=self.learning_rate,
            **self.config_train.optimizer_args,
        )

        # Scheduler
        if self.config_train.scheduler == torch.optim.lr_scheduler.OneCycleLR:
            lr_scheduler = self.config_train.scheduler(
                optimizer,
                max_lr=self.learning_rate,
                # total_steps=self.trainer.estimated_stepping_batches, # if using self.lr_schedulers().step()
                total_steps=self.config_train.epochs,  # if using self.lr_schedulers().step(epoch=epoch_float)
                **self.config_train.scheduler_args,
            )
        else:
            lr_scheduler = self.config_train.scheduler(
                optimizer,
                **self.config_train.scheduler_args,
            )

        return {"optimizer": optimizer, "lr_scheduler": lr_scheduler}

    def _get_time_based_sample_weight(self, t):
        end_w = self.config_train.newer_samples_weight
        start_t = self.config_train.newer_samples_start
        time = (t.detach() - start_t) / (1.0 - start_t)
        time = torch.clamp(time, 0.0, 1.0)  # time = 0 to 1
        time = np.pi * (time - 1.0)  # time =  -pi to 0
        time = 0.5 * torch.cos(time) + 0.5  # time =  0 to 1
        # scales end to be end weight times bigger than start weight
        # with end weight being 1.0
        weight = (1.0 + time * (end_w - 1.0)) / end_w
        # add an extra dimension for the quantiles
        weight = weight.unsqueeze(dim=2)
        return weight

    def _add_batch_regularizations(self, loss, progress):
        """Add regularization terms to loss, if applicable
        Parameters
        ----------
            loss : torch.Tensor, scalar
                current batch loss
            progress : float
                progress within training, across all epochs and batches, between 0 and 1
        Returns
        -------
            loss, reg_loss
        """
        delay_weight = self.config_train.get_reg_delay_weight(progress)

        reg_loss = torch.zeros(1, dtype=torch.float, requires_grad=False, device=self.device)
        if delay_weight > 0:
            # Add regularization of AR weights - sparsify
            if self.config_model.max_lags > 0 and self.config_ar.reg_lambda is not None:
                reg_ar = self.config_ar.regularize(self.ar_weights)
                reg_ar = torch.sum(reg_ar).squeeze() / self.config_model.n_forecasts
                reg_loss += self.config_ar.reg_lambda * reg_ar

            # Regularize trend to be smoother/sparse
            l_trend = self.config_trend.trend_reg
            if self.config_trend.n_changepoints > 0 and l_trend is not None and l_trend > 0:
                reg_trend = reg_func_trend(
                    weights=self.trend.get_trend_deltas,
                    threshold=self.config_train.trend_reg_threshold,
                )
                reg_loss += l_trend * reg_trend

            # Regularize seasonality: sparsify fourier term coefficients
            if self.config_seasonality:
                l_season = self.config_seasonality.reg_lambda
                if self.seasonality.season_dims is not None and l_season is not None and l_season > 0:
                    for name in self.seasonality.season_params.keys():
                        reg_season = reg_func_season(self.seasonality.season_params[name])
                        reg_loss += l_season * reg_season

            # Regularize events: sparsify events features coefficients
            if self.config_events is not None or self.config_holidays is not None:
                reg_events_loss = reg_func_events(self.config_events, self.config_holidays, self)
                reg_loss += reg_events_loss

            # Regularize regressors: sparsify regressor features coefficients
            if self.config_regressors.regressors is not None:
                reg_regressor_loss = reg_func_regressors(self.config_regressors.regressors, self)
                reg_loss += reg_regressor_loss

        trend_glocal_loss = torch.zeros(1, dtype=torch.float, requires_grad=False)
        # Glocal Trend
        if self.config_trend is not None:
            if self.config_trend.trend_global_local == "local" and self.config_trend.trend_local_reg:
                trend_glocal_loss = reg_func_trend_glocal(
                    self.trend.trend_k0, self.trend.trend_deltas, self.config_trend.trend_local_reg
                )
                reg_loss += trend_glocal_loss
        # Glocal Seasonality
        if self.config_seasonality is not None:
            if (
                self.config_seasonality.global_local in ["local", "glocal"]
                and self.config_seasonality.seasonality_local_reg
            ):
                seasonality_glocal_loss = reg_func_seasonality_glocal(
                    self.seasonality.season_params, self.config_seasonality.seasonality_local_reg
                )
                reg_loss += seasonality_glocal_loss
        loss = loss + reg_loss
        return loss, reg_loss

    def denormalize(self, ts):
        """
        Denormalize timeseries
        Parameters
        ----------
            target : torch.Tensor
                ts tensor
        Returns
        -------
            denormalized timeseries
        """
        if self.config_normalization.global_normalization:
            shift_y = (
                self.config_normalization.global_data_params["y"].shift
                if self.config_normalization.global_normalization and not self.config_normalization.normalize == "off"
                else 0
            )
            scale_y = (
                self.config_normalization.global_data_params["y"].scale
                if self.config_normalization.global_normalization and not self.config_normalization.normalize == "off"
                else 1
            )
            ts = scale_y * ts + shift_y
        return ts

    # def train_dataloader(self):
    #     return self.train_loader


class FlatNet(nn.Module):
    """
    Linear regression fun
    """

    def __init__(self, d_inputs, d_outputs):
        # Perform initialization of the pytorch superclass
        super(FlatNet, self).__init__()
        self.layers = nn.Sequential(
            nn.Linear(d_inputs, d_outputs),
        )
        nn.init.kaiming_normal_(self.layers[0].weight, mode="fan_in")

    def forward(self, x):
        return self.layers(x)

    @property
    def ar_weights(self):
        return self.model.layers[0].weight


class DeepNet(nn.Module):
    """
    A simple, general purpose, fully connected network
    """

    def __init__(self, d_inputs, d_outputs, layers=[]):
        # Perform initialization of the pytorch superclass
        super(DeepNet, self).__init__()
        layers = []
        for d_hidden_i in layers:
            layers.append(nn.Linear(d_inputs, d_hidden_i, bias=True))
            layers.append(nn.ReLU())
            d_inputs = d_hidden_i
        layers.append(nn.Linear(d_inputs, d_outputs, bias=True))
        self.layers = nn.Sequential(*layers)
        for lay in self.layers:
            nn.init.kaiming_normal_(lay.weight, mode="fan_in")

    def forward(self, x):
        """
        This method defines the network layering and activation functions
        """
        return self.layers(x)

    @property
    def ar_weights(self):
        return self.layers[0].weight
