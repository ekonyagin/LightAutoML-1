from time import perf_counter
import cudf
import dask_cudf
import cupy as cp
import numpy as np
import pandas as pd
import torch
import random
from numba import jit
import string
from sklearn.model_selection import train_test_split

from lightautoml.reader.gpu.cudf_reader import CudfReader
from lightautoml.reader.base import PandasToPandasReader
from lightautoml.reader.gpu.daskcudf_reader import DaskCudfReader

from lightautoml.transformers.base import SequentialTransformer
from lightautoml.pipelines.utils import get_columns_by_role

from lightautoml.transformers.gpu import numeric_gpu, categorical_gpu, datetime_gpu
from lightautoml.transformers import numeric, categorical, datetime

from lightautoml.tasks import Task

RANDS_CHARS = np.array(list(string.ascii_letters + string.digits),
                       dtype=(np.str_, 1))


@jit(nopython=True)
def gen_cols(n_cols):
    cols = [""]*n_cols
    for i in range(n_cols):
        cols[i] = "col_" + str(i)
    return cols

def gen_string_data(n, n_str):
    string_db = ["algorithm", "analog", "app", "application", "array",
                 "backup", "bandwidth", "binary", "bit", "byte"]#,
                 #"bitmap", "blog", "bookmark", "boot", "broadband",
                 #"browser" , "buffer", "bug"]
    inds = np.random.randint(0, len(string_db), (n, n_str))
    output = np.empty(inds.shape, dtype=object)
    for i in range(inds.shape[0]):
        for j in range(inds.shape[1]):
            output[i][j] = string_db[inds[i][j]]

    return output

def generate_data(n, n_num, n_cat, n_date, n_str, max_n_cat):
    n_cols = n_num+n_cat+n_str+n_date
    cols = gen_cols(n_cols)
    data = np.random.random((n, n_num))*100-50

    category_data = np.random.randint(0, np.random.randint(1,max_n_cat), (n, n_cat))
    string_data = gen_string_data(n, n_str)

    string_data = np.reshape(string_data, (n, n_str))

    date_data = np.random.randint(0, 1000,
                               (n, n_date)).astype(np.dtype("timedelta64[D]")) \
                               + np.datetime64("2018-01-01")

    data = pd.DataFrame(data, columns = cols[:n_num]).astype('f')
    
    ix = [(row, col) for row in range(data.shape[0]) for col in range(data.shape[1])]
    for row, col in random.sample(ix, int(round(.1*len(ix)))):
        data.iat[row, col] = np.nan
    
    nn = len(data.columns)
    for i in range(n_cat):
        data[cols[nn+i]] = pd.Series(category_data[:,i]).astype('f')
    nn = len(data.columns)
    for i in range(n_str):
        data[cols[nn+i]] = pd.Series(string_data[:,i]).astype(object)
    nn = len(data.columns)
    for i in range(n_date):
        data[cols[nn+i]] = pd.Series(date_data[:,i])

    data['TARGET0'] = pd.Series( np.random.random(n) )
    data['TARGET1'] = pd.Series( np.random.random(n)*2. )
    data['TARGET2'] = pd.Series( np.random.random(n)+2. )

    return 'TARGET', cols, data

if __name__ == "__main__":
    target, _, data = generate_data(n=40, n_num=4, n_cat=2, n_date=2,
                                    n_str=3, max_n_cat=10)
    roles = {"target": {'TARGET0', 'TARGET1', 'TARGET2'}}
    np.random.seed(42)
    torch.set_num_threads(4)
    adv_roles = True


    #task_cpu = Task("multi:reg")
    #task_gpu = Task("multi:reg", device="gpu")
    task_mgpu = Task("multi:reg", device="mgpu")

    #reader = PandasToPandasReader(task=task_cpu, 
    #                          advanced_roles = adv_roles,
    #                          n_jobs=1)
    #pd_data = reader.fit_read(data, roles = roles)

    #cudf_reader = CudfReader(task=task_gpu,
    #                advanced_roles = adv_roles,
    #                n_jobs = 1)
    #cudf_data = cudf_reader.fit_read(data, roles = roles)

    dd_reader = DaskCudfReader(task_mgpu, advanced_roles=adv_roles,
                    index_ok = False, n_jobs=1, compute=False, npartitions=2)
    dd_data = dd_reader.fit_read(data, roles=roles)

    #cats = pd_data[:, get_columns_by_role(pd_data, 'Category')]
    #gpu_cats = cudf_data[:, get_columns_by_role(cudf_data, 'Category')]
    #dd_cats = dd_data[:, get_columns_by_role(dd_data, 'Category')]
    dd_cats = dd_data[:, get_columns_by_role(dd_data, 'Category')]
    cats = dd_cats.to_pandas()
    gpu_cats = dd_cats.to_cudf()

    trf = SequentialTransformer(
        [categorical.LabelEncoder(), categorical.MultioutputTargetEncoder()]
    )
    gpu_trf = SequentialTransformer(
        [categorical_gpu.LabelEncoder_gpu(), categorical_gpu.MultioutputTargetEncoder_gpu()]
    )

    enc_cpu = trf.fit_transform(cats)
    enc_gpu = gpu_trf.fit_transform(gpu_cats)
    enc_mgpu = gpu_trf.fit_transform(dd_cats)

    assert np.allclose(enc_cpu.data, cp.asnumpy(enc_gpu.data))
    assert np.allclose(enc_cpu.data, 
                       enc_mgpu.data.compute().values_host)





