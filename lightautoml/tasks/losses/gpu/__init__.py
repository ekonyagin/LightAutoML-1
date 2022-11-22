"""Set of loss functions for different machine learning algorithms."""

from lightautoml.tasks.losses.gpu.cuml import CUMLLoss
from lightautoml.tasks.losses.gpu.torch_gpu import TORCHLossGPU
from lightautoml.tasks.losses.gpu.xgb_gpu import XGBLoss
from lightautoml.tasks.losses.gpu.pb_gpu import PBLoss

from ..base import _valid_str_metric_names

__all__ = [
    "_valid_str_metric_names",
    "TORCHLossGPU",
    "CUMLLoss",
    "XGBLoss",
    "PBLoss"
]
