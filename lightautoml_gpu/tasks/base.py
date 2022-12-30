"""Contain Task object and metric wrappers."""

import inspect
import logging

from functools import partial
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Union

import numpy as np

from .common_metric import _valid_metric_args
from .common_metric import _valid_str_metric_names
from .losses import CBLoss
from .losses import LGBLoss
from .losses import SKLoss
from .losses import TORCHLoss
from .utils import infer_gib
from .utils import infer_gib_multiclass

from .losses.gpu.xgb_gpu import XGBLoss
from .losses.gpu.pb_gpu import PBLoss
    

import torch
if torch.cuda.is_available():
    import cudf
    import cupy as cp
    import dask.array as da
    import dask_cudf
    from .losses.gpu.torch_gpu import TORCHLossGPU
    from .losses.gpu.cuml import CUMLLoss
    from lightautoml_gpu.tasks.gpu.common_metric_gpu import _valid_str_metric_names_gpu
    from lightautoml_gpu.tasks.gpu.utils_gpu import infer_gib_gpu
    from lightautoml_gpu.tasks.gpu.utils_gpu import infer_gib_multiclass_gpu

    if TYPE_CHECKING:
        from ..dataset.gpu.gpu_dataset import CudfDataset
        from ..dataset.gpu.gpu_dataset import CupyDataset
        from ..dataset.gpu.gpu_dataset import DaskCudfDataset

        CumlCompatible = Union[CupyDataset, CudfDataset, DaskCudfDataset]
else:
    print("could not load gpu related libs (tasks/base.py)")

if TYPE_CHECKING:
    from ..dataset.base import LAMLDataset
    from ..dataset.np_pd_dataset import NumpyDataset
    from ..dataset.np_pd_dataset import PandasDataset

    SklearnCompatible = Union[NumpyDataset, PandasDataset]

logger = logging.getLogger(__name__)

_valid_task_names = ["binary", "reg", "multiclass", "multi:reg", "multilabel"]
_one_dim_output_tasks = ["binary", "reg"]

_default_losses = {
    "binary": "logloss",
    "reg": "mse",
    "multiclass": "crossentropy",
    "multi:reg": "mae",
    "multilabel": "logloss",
}

_default_metrics = {
    "binary": "auc",
    "reg": "mse",
    "multiclass": "crossentropy",
    "multi:reg": "mae",
    "multilabel": "logloss",
}

_valid_loss_types = ["lgb", "sklearn", "torch", "cb"]

_valid_str_loss_names = {
    "binary": ["logloss"],
    "reg": ["mse", "mae", "mape", "rmsle", "quantile", "huber", "fair"],
    "multiclass": ["crossentropy", "f1"],
    "multi:reg": ["mae", "mse"],
    "multilabel": ["logloss"],
}


_valid_loss_args = {"quantile": ["q"], "huber": ["a"], "fair": ["c"]}


class LAMLMetric:
    """Abstract class for metric.

    Metric should be called on dataset.

    """

    greater_is_better = True

    def __call__(self, dataset: "LAMLDataset", dropna: bool = False):
        """Call metric on dataset.

        Args:
            dataset: Table with data.
            dropna: To ignore NaN in metric calculation.

        Returns:  # noqa DAR202
            Metric value.

        Raises:
            AttributeError: If metric isn't defined.

        """
        assert hasattr(dataset, "target"), "Dataset should have target to calculate metric"
        raise NotImplementedError


class ArgsWrapper:
    """Wrapper - ignore sample_weight if metric not accepts.

    Args:
        func: Metric function.
        metric_params: Additional metric parameters.

    """

    def __init__(self, func: Callable, metric_params: dict):
        keys = inspect.signature(func).parameters
        self.flg = "sample_weight" in keys
        self.func = partial(func, **metric_params)

    def __call__(self, y_true, y_pred, sample_weight=None):
        """Calculate metric value.

        If the metric does not include weights, then they are ignored.

        Args:
            y_true: Ground truth target values.
            y_pred: Estimated target values.
            sample_weight: Sample weights.

        Returns:
            Metric value.

        """
        if self.flg:
            return self.func(y_true, y_pred, sample_weight=sample_weight)

        return self.func(y_true, y_pred)


class SkMetric(LAMLMetric):
    """Abstract class for scikit-learn compatible metric.

    Implements metric calculation in sklearn format on numpy/pandas datasets.


    Args:
        metric: Specifies metric. Format:
            ``func(y_true, y_false, Optional[sample_weight], **kwargs)`` -> `float`.
        name: Name of metric.
        greater_is_better: Whether or not higher metric value is better.
        one_dim: `True` for single class, False for multiclass.
        weighted: Weights of classes.
        **kwargs: Other parameters for metric.

    """

    @property
    def metric(self) -> Callable:
        """Metric function."""
        assert self._metric is not None, "Metric calculation is not defined"
        return self._metric

    @property
    def name(self) -> str:
        """Name of used metric."""
        if self._name is None:
            return "AutoML Metric"
        else:
            return self._name

    def __init__(
        self,
        metric: Optional[Callable] = None,
        name: Optional[str] = None,
        greater_is_better: bool = True,
        one_dim: bool = True,
        **kwargs: Any,
    ):
        self._metric = metric
        self._name = name

        self.greater_is_better = greater_is_better
        self.one_dim = one_dim
        # self.weighted = weighted

        self.kwargs = kwargs

    def __call__(self, dataset: "SklearnCompatible", dropna: bool = False) -> float:
        """Implement call sklearn metric on dataset.

        Args:
            dataset: Dataset in Numpy or Pandas format.
            dropna: To ignore NaN in metric calculation.

        Returns:
            Metric value.

        Raises:
            AssertionError: if dataset has no target or
                target specified as one-dimensioned, but it is not.

        """
        assert hasattr(dataset, "target"), "Dataset should have target to calculate metric"
        if self.one_dim:
            assert dataset.shape[1] == 1, "Dataset should have single column if metric is one_dim"
        # TODO: maybe refactor this part?
        dataset = dataset.to_numpy()
        y_true = dataset.target
        y_pred = dataset.data
        sample_weight = dataset.weights

        if dropna:
            sl = ~np.isnan(y_pred).any(axis=1)
            y_pred = y_pred[sl]
            y_true = y_true[sl]
            if sample_weight is not None:
                sample_weight = sample_weight[sl]

        if self.one_dim:
            y_pred = y_pred[:, 0]

        value = self.metric(y_true, y_pred, sample_weight=sample_weight)
        sign = 2 * float(self.greater_is_better) - 1
        return value * sign


class CumlMetric(SkMetric):
    """Abstract class for cuml compatible metric.

    Implements metric calculation in cuml format on cupy/cudf datasets.

    """

    def __call__(self, dataset: "CumlCompatible", dropna: bool = False) -> float:
        """Implement call cuml metric on dataset.

        Args:
            dataset: Dataset in Cupy or Cudf format.
            dropna: To ignore NaN in metric calculation.

        Returns:
            Metric value.

        Raises:
            AssertionError: if dataset has no target or
                target specified as one-dimensioned, but it is not.

        """
        assert hasattr(
            dataset, "target"
        ), "Dataset should have target to calculate metric"
        if self.one_dim:
            assert (
                dataset.shape[1] == 1
            ), "Dataset should have single column if metric is one_dim"
        dataset = dataset.to_cupy()
        y_true = dataset.target
        y_pred = dataset.data
        sample_weight = dataset.weights

        if dropna:
            sl = ~cp.isnan(y_pred).any(axis=1)
            y_pred = y_pred[sl]
            y_true = y_true[sl]
            if sample_weight is not None:
                sample_weight = sample_weight[sl]

        if self.one_dim:
            y_pred = y_pred[:, 0]

        value = self.metric(y_true, y_pred, sample_weight=sample_weight)
        sign = 2 * float(self.greater_is_better) - 1
        return value * sign


class DaskmlMetric(SkMetric):
    """Abstract class for dask_ml compatible metric.

    Implements metric calculation in dask_ml format on DaskCudfDataset.

    """

    def __call__(self, dataset: "CumlCompatible", dropna: bool = False) -> float:
        """Implement call dask_ml metric on dataset.

        Args:
            dataset: Dataset in DaskCudf format.
            dropna: To ignore NaN in metric calculation.

        Returns:
            Metric value.

        Raises:
            AssertionError: if dataset has no target or
                target specified as one-dimensioned, but it is not.

        """
        assert hasattr(
            dataset, "target"
        ), "Dataset should have target to calculate metric"
        if self.one_dim:
            assert (
                dataset.shape[1] == 1
            ), "Dataset should have single column if metric is one_dim"

        # in general need to check type of the dataset and act acordingly
        if isinstance(dataset.data, cp.ndarray):
            y_true = dataset.target
            y_pred = dataset.data.astype(cp.float32)
        elif isinstance(dataset.data, (cudf.DataFrame, dask_cudf.DataFrame)):
            y_true = dataset.target.values
            y_pred = dataset.data.astype(cp.float32).values
        else:
            raise NotImplementedError

        sample_weight = None
        if dataset.weights is not None:
            sample_weight = dataset.weights.values

        if dropna:
            sl = ~da.isnan(y_pred).any(axis=1)
            y_pred = y_pred[sl]
            y_true = y_true[sl]
            if sample_weight is not None:
                sample_weight = sample_weight[sl]

        if self.one_dim:
            y_pred = y_pred[:, 0]

        value = self.metric(y_true, y_pred, sample_weight=sample_weight)
        sign = 2 * float(self.greater_is_better) - 1
        return value * sign


class Task:
    """Specify task (binary classification, multiclass classification, regression), metrics, losses.

    Args:
        name: Task name.
        loss: Objective function or dict of functions.
        loss_params: Additional loss parameters,
            if dict there is no presence check for loss_params.
        metric: String name or callable.
        metric_params: Additional metric parameters.
        greater_is_better: Whether or not higher value is better.
        device: Which mode (CPU or GPU or Multi-GPU) is used for the task.
    Note:
        There is 3 different task types:

            - `'binary'` - for binary classification.
            - `'reg'` - for regression.
            - `'multiclass'` - for multiclass classification.

        Avaliable losses for binary task:

            - `'logloss'` - (uses by default) Standard logistic loss.

        Avaliable losses for regression task:

            - `'mse'` - (uses by default) Mean Squared Error.
            - `'mae'` - Mean Absolute Error.
            - `'mape'` - Mean Absolute Percentage Error.
            - `'rmsle'` - Root Mean Squared Log Error.
            - `'huber'` - Huber loss, reqired params:
                ``a`` - threshold between MAE and MSE losses.
            - `'fair'` - Fair loss, required params:
                ``c`` - sets smoothness.
            - `'quantile'` - Quantile loss, required params:
                ``q`` - sets quantile.

        Avaliable losses for multi-classification task:

            - `'crossentropy'` - (uses by default) Standard crossentropy function.
            - `'f1'` - Optimizes F1-Macro Score, now avaliable for
                LightGBM and NN models. Here we implicitly assume
                that the prediction lies not in the set ``{0, 1}``,
                but in the interval ``[0, 1]``.

        Available metrics for binary task:

            - `'auc'` - (uses by default) ROC-AUC score.
            - `'accuracy'` - Accuracy score (uses argmax prediction).
            - `'logloss'` - Standard logistic loss.

        Avaliable metrics for regression task:

            - `'mse'` - (uses by default) Mean Squared Error.
            - `'mae'` - Mean Absolute Error.
            - `'mape'` - Mean Absolute Percentage Error.
            - `'rmsle'` - Root Mean Squared Log Error.
            - `'huber'` - Huber loss, reqired params:
                ``a`` - threshold between MAE and MSE losses.
            - `'fair'` - Fair loss, required params:
                ``c`` - sets smoothness.
            - `'quantile'` - Quantile loss, required params:
                ``q`` - sets quantile.

        Avaliable metrics for multi-classification task:

            - `'crossentropy'` - (uses by default) Standard cross-entropy loss.
            - `'auc'` - ROC-AUC of each class against the rest.
            - `'auc_mu'` - AUC-Mu. Multi-class extension of standard AUC
                for binary classification. In short,
                mean of n_classes * (n_classes - 1) / 2 binary AUCs.
                More info on http://proceedings.mlr.press/v97/kleiman19a/kleiman19a.pdf


    Example:
        >>> task = Task('binary', metric='auc')

    """

    @property
    def name(self) -> str:
        """Name of task."""
        return self._name

    def __init__(
        self,
        name: str,
        loss: Optional[Union[dict, str]] = None,
        loss_params: Optional[Dict] = None,
        metric: Optional[Union[str, Callable]] = None,
        metric_params: Optional[Dict] = None,
        greater_is_better: Optional[bool] = None,
        device: Optional[str] = None,
        no_gpu=False
    ):

        assert name in _valid_task_names, "Invalid task name: {}, allowed task names: {}".format(
            name, _valid_task_names
        )
        self._name = name

        if device is None:
            device = "cpu"
        assert device in [
            "cpu",
            "gpu",
            "mgpu",
        ], "The device must be either `cpu`, `gpu` or `mgpu`!"

        self.device = device
        self.loss = loss
        # add losses
        # if None - infer from task
        self.losses = {}
        if loss is None:
            loss = _default_losses[self.name]

        if loss_params is None:
            loss_params = {}

        # case - infer from string
        if type(loss) is str:

            # case when parameters defined
            if len(loss_params) > 0:
                self._check_loss_from_params(loss, loss_params)
                # check if loss and metric are the same - rewrite loss params
                # ??? "rewrite METRIC params" ???
                if loss == metric:
                    metric_params = loss_params
                    logger.info("As loss and metric are equal, metric params are ignored.")

            else:
                assert (
                    loss not in _valid_loss_args
                ), "Loss should be defined with arguments. Ex. loss='quantile', loss_params={'q': 0.7}."
                loss_params = None

            assert loss in _valid_str_loss_names[self.name], "Invalid loss name: {} for task {}.".format(
                loss, self.name
            )

            loss_factories = [LGBLoss, SKLoss, TORCHLoss, CBLoss]
            loss_keys = ["lgb", "sklearn", "torch", "cb"]

            loss_factories.extend([XGBLoss, PBLoss])
            loss_keys.extend(["xgb", "pb"])
            if not no_gpu and torch.cuda.is_available():
                loss_factories.extend([TORCHLossGPU, CUMLLoss])
                loss_keys.extend(["torch_gpu", "cuml"])
            for loss_key, loss_factory in zip(loss_keys, loss_factories):
                try:
                    self.losses[loss_key] = loss_factory(loss, loss_params=loss_params)
                except (AssertionError, TypeError, ValueError):
                    logger.info("{0} doesn't support in general case {1} and will not be used.".format(loss_key, loss))

            assert len(self.losses) > 0, "None of frameworks supports {0} loss.".format(loss)

        elif type(loss) is dict:
            # case - dict passed directly
            # TODO: check loss parameters?
            #  Or it there will be assert when use functools.partial
            # assert all(map(lambda x: x in _valid_loss_types, loss)), 'Invalid loss key.'
            assert len([key for key in loss.keys() if key in _valid_loss_types]) != len(loss), "Invalid loss key."
            self.losses = loss

        else:
            raise TypeError("Loss passed incorrectly.")

        # set callback metric for loss
        # if no metric - infer from task
        if metric is None:
            metric = _default_metrics[self.name]

        self.metric_params = {}
        if metric_params is not None:
            self.metric_params = metric_params

        if type(metric) is str:

            self._check_metric_from_params(metric, self.metric_params)
            if self.device == "cpu":
                metric_func = _valid_str_metric_names[self.name][metric]
            else:
                metric_func = _valid_str_metric_names_gpu[self.name][metric]
            metric_func = partial(metric_func, **self.metric_params)
            self.metric_func = metric_func
            self.metric_name = metric

        else:
            metric = ArgsWrapper(metric, self.metric_params)
            self.metric_params = {}
            self.metric_func = metric
            self.metric_name = None

        if greater_is_better is None:
            if self.device == "cpu":
                infer_gib_fn = infer_gib_multiclass if (name == "multiclass" or name == "multilabel") else infer_gib
            else:
                infer_gib_fn = infer_gib_multiclass_gpu if (name == "multiclass" or name == "multilabel") else infer_gib_gpu
            greater_is_better = infer_gib_fn(self.metric_func)

        self.greater_is_better = greater_is_better

        for loss_key in self.losses:
            try:
                self.losses[loss_key].set_callback_metric(metric, greater_is_better, self.metric_params, self.name)
            except:
                print(f"{self.name} isn`t supported in {loss_key}")

    def get_dataset_metric(self) -> LAMLMetric:
        """Create metric for dataset.

        Get metric that is called on dataset.

        Returns:
            Metric in scikit-learn compatible format.

        """
        # for now - case of sklearn metric only
        one_dim = self.name in _one_dim_output_tasks
        if self.device == "cpu":
            dataset_metric = SkMetric(
                self.metric_func,
                name=self.metric_name,
                one_dim=one_dim,
                greater_is_better=self.greater_is_better)
        elif self.device == "gpu":
            dataset_metric = CumlMetric(
                self.metric_func,
                name=self.metric_name,
                one_dim=one_dim,
                greater_is_better=self.greater_is_better)
        else:
            dataset_metric = DaskmlMetric(
                self.metric_func,
                name=self.metric_name,
                one_dim=one_dim,
                greater_is_better=self.greater_is_better)

        return dataset_metric

    @staticmethod
    def _check_loss_from_params(loss_name, loss_params):
        if loss_name in _valid_loss_args:
            required_params = set(_valid_loss_args[loss_name])
        else:
            required_params = set()
        given_params = set(loss_params)
        extra_params = given_params - required_params
        assert len(extra_params) == 0, "For loss {0} given extra params {1}".format(loss_name, extra_params)
        needed_params = required_params - given_params
        assert len(needed_params) == 0, "For loss {0} required params {1} are not defined".format(
            loss_name, needed_params
        )

    @staticmethod
    def _check_metric_from_params(metric_name, metric_params):
        if metric_name in _valid_metric_args:
            required_params = set(_valid_loss_args[metric_name])
        else:
            required_params = set()
        given_params = set(metric_params)
        extra_params = given_params - required_params
        assert len(extra_params) == 0, "For metric {0} given extra params {1}".format(metric_name, extra_params)
        needed_params = required_params - given_params
        assert len(needed_params) == 0, "For metric {0} required params {1} are not defined".format(
            metric_name, needed_params
        )
