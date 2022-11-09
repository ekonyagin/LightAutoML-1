"""Utilities for working with the structure of a dataset."""

from typing import Callable
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

from .base import LAMLDataset
from .np_pd_dataset import CSRSparseDataset
from .np_pd_dataset import NumpyDataset
from .np_pd_dataset import PandasDataset
from .roles import ColumnRole
from .seq_np_pd_dataset import SeqNumpyPandasDataset

gpu_available = True
try:
    from lightautoml.dataset.gpu.gpu_dataset import (
        CudfDataset,
        CupyDataset,
        DaskCudfDataset,
        SeqCudfDataset,
        SeqDaskCudfDataset
    )
    
except ModuleNotFoundError:
    gpu_available = False
    print("No GPUs detected. Switching  to CPU...")

# RoleType = TypeVar("RoleType", bound=ColumnRole)


def roles_parser(init_roles: Dict[Union[ColumnRole, str], Union[str, Sequence[str]]]) -> Dict[str, ColumnRole]:
    """Parser of roles.

    Parse roles from old format numeric:
    ``[var1, var2, ...]`` to ``{var1:numeric, var2:numeric, ...}``.

    Args:
        init_roles: Mapping between roles and feature names.

    Returns:
        Roles dict in format key - feature names, value - roles.

    """
    roles = {}
    for r in init_roles:

        feat = init_roles[r]

        if isinstance(feat, str):
            roles[feat] = r

        else:
            for f in init_roles[r]:
                roles[f] = r

    return roles


def get_common_concat(
    datasets: Sequence[LAMLDataset],
) -> Tuple[Callable, Optional[type]]:
    """Get concatenation function for datasets of different types.

    Takes multiple datasets as input and check,
    if is's ok to concatenate it and return function.

    Args:
        datasets: Sequence of datasets.

    Returns:
        Function, that is able to concatenate datasets.

    """
    # TODO: Add pandas + numpy via transforming to numpy?
    dataset_types = set([type(x) for x in datasets])

    # general - if single type, concatenation for that type
    if len(dataset_types) == 1:
        klass = list(dataset_types)[0]
        return klass.concat, None

    # np and sparse goes to sparse
    elif dataset_types == {NumpyDataset, CSRSparseDataset}:
        return CSRSparseDataset.concat, CSRSparseDataset

    elif dataset_types == {NumpyDataset, PandasDataset}:
        return numpy_and_pandas_concat, None

    elif (dataset_types == {NumpyDataset, SeqNumpyPandasDataset}) or (
        dataset_types == {PandasDataset, SeqNumpyPandasDataset}
    ):
        return numpy_or_pandas_and_seq_concat, None

    elif gpu_available and dataset_types == {CudfDataset, CupyDataset}:
        return cupy_and_cudf_concat, None

    elif gpu_available and\
         (dataset_types == {CupyDataset, SeqCudfDataset}) or\
         (dataset_types == {CudfDataset, SeqCudfDataset}) or\
         (dataset_types == {DaskCudfDataset, SeqDaskCudfDataset}):
        return dask_or_cudf_and_seq_gpu_concat, None

    # elif dataset_types == {DaskCudfDataset, CupyDataset}:
    #    return daskcudf_and_cupy_concat, None

    raise TypeError("Unable to concatenate dataset types {0}".format(list(dataset_types)))

if gpu_available:
    def dask_or_cudf_and_seq_gpu_concat(datasets):

        assert len(datasets) == 2, "should be 1 sequential and 1 plain dataset"
        # get 1 numpy / pandas dataset
        for n, dataset in enumerate(datasets):
            if isinstance(dataset, (SeqCudfDataset, SeqDaskCudfDataset)):
                seq_dataset = dataset
            else:
                plain_dataset = dataset

        if len(seq_dataset.data) == len(plain_dataset):
            if isinstance(dataset, SeqCudfDataset):
                return SeqCudfDataset.concat([seq_dataset, plain_dataset.to_cudf()])
            elif isinstance(dataset, SeqDaskCudfDataset):
                return SeqDaskCudfDataset.concat([seq_dataset, plain_dataset.to_daskcudf()])
            else:
                raise NotImplementedError

        else:
            if hasattr(plain_dataset, "seq_data"):
                plain_dataset.seq_data[seq_dataset.name] = seq_dataset
            else:
                plain_dataset.seq_data = {seq_dataset.name: seq_dataset}

            return plain_dataset

    def cupy_and_cudf_concat(
        datasets: Sequence[Union[CupyDataset, CudfDataset]]
    ) -> CudfDataset:
        """Concat of cupy and cudf dataset.

        Args:
            datasets: Sequence of datasets to concatenate.

        Returns:
            Concatenated dataset.

        """

        # is it better to convert to cudf?
        # concating to cudf made problem with convert_to_holdout_iterator (shape difference)
        datasets = [x.to_cupy() for x in datasets]

        return CupyDataset.concat(datasets)


    def daskcudf_and_cupy_concat(
        datasets: Sequence[Union[DaskCudfDataset, CupyDataset]]
    ) -> CudfDataset:
        """Concat of daskcudf and cupy dataset.

            Args:
                datasets: Sequence of datasets to concatenate.

            Returns:
                Concatenated dataset.

            """
        # raise ValueError("TestException")
        for x in datasets:
            if type(x) is DaskCudfDataset:
                n_parts = len(x.data._divisions) - 1
        datasets = [x.to_daskcudf(n_parts) for x in datasets]

        out = DaskCudfDataset.concat(datasets)
        return out

def numpy_and_pandas_concat(datasets: Sequence[Union[NumpyDataset, PandasDataset]]) -> PandasDataset:
    """Concat of numpy and pandas dataset.

    Args:
        datasets: Sequence of datasets to concatenate.

    Returns:
        Concatenated dataset.

    """
    datasets = [x.to_pandas() for x in datasets]

    return PandasDataset.concat(datasets)

def numpy_or_pandas_and_seq_concat(
    datasets: Sequence[Union[NumpyDataset, PandasDataset, SeqNumpyPandasDataset]]
) -> Union[NumpyDataset, PandasDataset]:
    """Concat plain and sequential dataset.

    If both datasets have same size then concat them as plain, otherwise include seq dataset inside plain one.

    Args:
        datasets: one plain and one seq dataset.

    Returns:
        Concatenated dataset.

    """
    assert len(datasets) == 2, "should be 1 sequential and 1 plain dataset"
    # get 1 numpy / pandas dataset
    for n, dataset in enumerate(datasets):
        if type(dataset) == SeqNumpyPandasDataset:
            seq_dataset = dataset
        else:
            plain_dataset = dataset

    if len(seq_dataset.data) == len(plain_dataset):
        return SeqNumpyPandasDataset.concat([seq_dataset, plain_dataset.to_pandas()])
    else:
        if hasattr(plain_dataset, "seq_data"):
            plain_dataset.seq_data[seq_dataset.name] = seq_dataset
        else:
            plain_dataset.seq_data = {seq_dataset.name: seq_dataset}

        return plain_dataset


def concatenate(datasets: Sequence[LAMLDataset]) -> LAMLDataset:
    """Dataset concatenation function.

    Check if datasets have common concat function and then apply.
    Assume to take target/folds/weights etc from first one.

    Args:
        datasets: Sequence of datasets.

    Returns:
        Dataset with concatenated features.

    """
    conc, klass = get_common_concat([ds for ds in datasets if ds is not None])

    # this part is made to avoid setting first dataset of required type
    if klass is not None:

        n = 0
        for n, ds in enumerate(datasets):
            if type(ds) is klass:
                break

        datasets = [datasets[n]] + [x for (y, x) in enumerate(datasets) if n != y]

    return conc(datasets)
