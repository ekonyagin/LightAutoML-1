"""Wrapped pboost for tabular datasets."""

import logging
from copy import copy, deepcopy
from typing import Callable, Dict, Optional, Tuple

from py_boost import GradientBoosting, TLPredictor
from py_boost.multioutput.sketching import *

import cudf
import cupy as cp
import dask_cudf
import numpy as np
import pandas as pd
import torch
from torch.cuda import device_count

from lightautoml.dataset.gpu.gpu_dataset import CupyDataset, CudfDataset, DaskCudfDataset
from lightautoml.ml_algo.tuning.base import Uniform
from lightautoml.pipelines.selection.base import ImportanceEstimator
from lightautoml.validation.base import TrainValidIterator

from lightautoml.tasks.base import Task

from .base_gpu import TabularDatasetGpu, TabularMLAlgoGPU
from ..boost_pb import PBPredictor

logger = logging.getLogger(__name__)


class BoostPB(TabularMLAlgoGPU, ImportanceEstimator):
    """tba
    """

    _name: str = "PB"

    _default_params = {
        "ntrees": 100,
        "lr": 0.05,
        "min_gain_to_split": 0,
        "lambda_l2": 1,
        "gd_steps": 1,
        "max_depth": 6,
        "min_data_in_leaf": 10,
        "colsample": 1.,
        "subsample": 1.,
        "target_splitter": 'Single',
        "use_hess": True,
        "quantization": 'Quantile',
        "quant_sample": 2000000,
        "max_bin": 256,
        "min_data_in_bin": 3,
        "es": 100,
        "seed": 42,
        "verbose": 10
    }

    def _infer_params(
        self,
    ) -> Tuple[dict, int, int, int, Optional[Callable], Optional[Callable]]:
        """Infer all parameters in lightgbm format.

        Returns:
            Tuple (params, num_trees, early_stopping_rounds, verbose_eval, fobj, feval).
            About parameters: https://lightgbm.readthedocs.io/en/latest/_modules/lightgbm/engine.html

        """
        params = copy(self.params)
        early_stopping_rounds = params.pop("es")
        num_trees = params.pop("ntrees")

        root_logger = logging.getLogger()
        level = root_logger.getEffectiveLevel()

        if level in (logging.CRITICAL, logging.ERROR, logging.WARNING):
            params["verbose"] = 100
        elif level == logging.INFO:
            params["verbose"] = 100
        else:
            params["verbose"] = 10

        # get objective params
        loss = self.task.losses["pb"]
        params["loss"] = loss.fobj_name
        # get metric params
        params["metric"] = loss.metric_name

        params["sketch_arg"] = 1

        # add loss and tasks params if defined
        params = {**params}

        return params

    def init_params_on_input(self, train_valid_iterator: TrainValidIterator) -> dict:
        """Get model parameters depending on dataset parameters.

        Args:
            train_valid_iterator: Classic cv-iterator.

        Returns:
            Parameters of model.

        """

        rows_num = len(train_valid_iterator.train)
        task = train_valid_iterator.train.task.name
        suggested_params = copy(self.default_params)

        if self.freeze_defaults:
            return suggested_params

        if task == "reg":
            suggested_params = {"lr": 0.05, "max_depth": 5}

        if rows_num <= 10000:
            init_lr = 0.01
            ntrees = 3000
            es = 200

        elif rows_num <= 20000:
            init_lr = 0.02
            ntrees = 3000
            es = 200

        elif rows_num <= 100000:
            init_lr = 0.03
            ntrees = 1200
            es = 200
        elif rows_num <= 300000:
            init_lr = 0.04
            ntrees = 2000
            es = 100
        else:
            init_lr = 0.05
            ntrees = 2000
            es = 100

        if rows_num > 300000:
            suggested_params["max_depth"] = 7 if task == "reg" else 8
        elif rows_num > 100000:
            suggested_params["max_depth"] = 6 if task == "reg" else 7
        elif rows_num > 50000:
            suggested_params["max_depth"] = 5 if task == "reg" else 6
        elif rows_num > 20000:
            suggested_params["max_depth"] = 5 if task == "reg" else 5
            init_lr = 0.045
        elif rows_num > 10000:
            suggested_params["max_depth"] = 5 if task == "reg" else 6
            init_lr = 0.035
        elif rows_num > 5000:
            suggested_params["max_depth"] = 4 if task == "reg" else 5
            init_lr = 0.03
        else:
            suggested_params["max_depth"] = 4
            init_lr = 0.02

        suggested_params["lr"] = init_lr
        suggested_params["ntrees"] = ntrees
        suggested_params["es"] = es

        val_data = train_valid_iterator.get_validation_data().empty()
        outp_dim = val_data.target.shape[1]
        suggested_params["n_classes"] = outp_dim

        return suggested_params

    def _get_default_search_spaces(
        self, suggested_params: Dict, estimated_n_trials: int
    ) -> Dict:
        """Sample hyperparameters from suggested.

        Args:
            suggested_params: Dict with parameters.
            estimated_n_trials: Maximum number of hyperparameter estimations.

        Returns:
            dict with sampled hyperparameters.

        """
        optimization_search_space = {}

        optimization_search_space["max_depth"] = Uniform(low=3, high=7, q=1)

        if estimated_n_trials > 100:
            optimization_search_space["lambda_l2"] = Uniform(low=1e-8, high=10.0, log=True)

        optimization_search_space["sketch_arg"] = Uniform(low=1, high=suggested_params["n_classes"]//2, q=1)

        return optimization_search_space

    def to_cpu(self):
        print("PB:", self.__dict__)
        print("PB model type:", self.models[0].__class__.__name__)
        print("PB model:", self.models[0].__dict__)
        models = []
        for i in range(len(self.models)):
            models.append(TLPredictor(self.models[i]))

        task = Task(name=self.task._name,
                    device='cpu',
                    metric=self.task.metric_name,
                    greater_is_better=self.task.greater_is_better)

        algo = PBPredictor()
        algo.models = models
        algo.task = task

        return algo

    def fit_predict_single_fold(
        self, train: TabularDatasetGpu, valid: TabularDatasetGpu, dev_id: int = 0
    ) -> Tuple[GradientBoosting, np.ndarray]:
        """Implements training and prediction on single fold.

        Args:
            train: Train Dataset.
            valid: Validation Dataset.

        Returns:
            Tuple (model, predicted_values)

        """
        if type(train) == DaskCudfDataset:
            ngpus = device_count()
            train_len = len(train)
            sub_size = int(1./ngpus*train_len)
            idx = np.random.RandomState(42).permutation(train_len)[:sub_size]
            train_data = train.data.loc[idx].compute().values
            train_target = train.target.loc[idx].compute().values
            train_weights = train.weights.loc[idx].compute().values if train.weights is not None else None
        else:
            train = train.to_cupy()
            train_target = train.target
            train_weights = train.weights
            train_data = train.data

        valid_len = len(valid)
        valid = valid.to_cupy()
        valid_target = valid.target
        valid_weights = valid.weights
        valid_data = valid.data

        with cp.cuda.Device(dev_id):
            if type(train_target) == cp.ndarray:
                train_target = cp.asnumpy(train_target)
                if train_weights is not None:
                    train_weights = cp.asnumpy(train_weights)
                valid_target = cp.asnumpy(valid_target)
                if valid_weights is not None:
                    valid_weights = cp.asnumpy(valid_weights)
                train_data = cp.asnumpy(train_data)
                valid_data = cp.asnumpy(valid_data)
            else:
                raise NotImplementedError(
                    "given type of input should not appear:"
                    + str(type(train_target))
                    + "class:"
                    + str(self._name)
                )

        params = self._infer_params()
        if "n_classes" in params.keys():
            params.pop("n_classes")
        if "sketch_arg" in params.keys():
            sketch_arg = params.pop("sketch_arg")
            model = GradientBoosting(**params, multioutput_sketch=RandomProjectionSketch(sketch_arg) )
        else:
            model = GradientBoosting(**params)
        model.fit(train_data, train_target, 
                  eval_sets=[{'X': valid_data, 'y': valid_target}])
        val_pred = model.predict(valid_data)

        return model, val_pred

    def predict_single_fold(
        self, model, dataset: TabularDatasetGpu
    ) -> np.ndarray:
        """Predict target values for dataset.

        Args:
            model: Lightgbm object.
            dataset: Test Dataset.

        Return:
            Predicted target values.

        """
        dataset_data = dataset.to_cupy().data
        #if type(dataset) == DaskCudfDataset:
        #    ngpus = torch.cuda.device_count()
        #    dataset_data = dataset_data.sample(frac=1./ngpus).compute().values_host
        #elif type(dataset) == CudfDataset:
        #    dataset_data = dataset_data.values_host
        #elif type(dataset) == CupyDataset:
        dataset_data = cp.asnumpy(dataset_data)

        pred = model.predict(dataset_data)
        return pred

    def get_features_score(self) -> pd.Series:
        """Computes feature importance as mean values of feature importance provided by pyboost per all models.

        Returns:
            Series with feature importances.

        """

        imp = 0
        for model in self.models:
            imp = imp + model.get_feature_importance()

        imp = imp / len(self.models)

        return pd.Series(imp, index=self.features).sort_values(ascending=False)

    def fit(self, train_valid: TrainValidIterator):
        """Just to be compatible with :class:`~lightautoml.pipelines.selection.base.ImportanceEstimator`.

        Args:
            train_valid: Classic cv-iterator.

        """
        self.fit_predict(train_valid)
