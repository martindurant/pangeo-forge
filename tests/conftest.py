import os
import socket
import subprocess
import time

import fsspec
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from dask.distributed import Client, LocalCluster

from pangeo_forge import recipe
from pangeo_forge.executors import (
    DaskPipelineExecutor,
    PrefectPipelineExecutor,
    PythonPipelineExecutor,
)
from pangeo_forge.patterns import VariableSequencePattern
from pangeo_forge.storage import CacheFSSpecTarget, FSSpecTarget, UninitializedTarget


def get_open_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    s.listen(1)
    port = str(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture(scope="session")
def daily_xarray_dataset():
    """Return a synthetic random xarray dataset."""
    np.random.seed(1)
    # TODO: change nt to 11 in order to catch the edge case where
    # items_per_input does not evenly divide the length of the sequence dimension
    nt, ny, nx = 10, 18, 36
    time = pd.date_range(start="2010-01-01", periods=nt, freq="D")
    lon = (np.arange(nx) + 0.5) * 360 / nx
    lon_attrs = {"units": "degrees_east", "long_name": "longitude"}
    lat = (np.arange(ny) + 0.5) * 180 / ny
    lat_attrs = {"units": "degrees_north", "long_name": "latitude"}
    foo = np.random.rand(nt, ny, nx)
    foo_attrs = {"long_name": "Fantastic Foo"}
    # make sure things work with heterogenous data types
    bar = np.random.randint(0, 10, size=(nt, ny, nx))
    bar_attrs = {"long_name": "Beautiful Bar"}
    dims = ("time", "lat", "lon")
    ds = xr.Dataset(
        {"bar": (dims, bar, bar_attrs), "foo": (dims, foo, foo_attrs)},
        coords={
            "time": ("time", time),
            "lat": ("lat", lat, lat_attrs),
            "lon": ("lon", lon, lon_attrs),
        },
        attrs={"conventions": "CF 1.6"},
    )
    return ds


def _split_up_files_by_day(ds, day_param):
    gb = ds.resample(time=day_param)
    _, datasets = zip(*gb)
    fnames = [f"{n:03d}.nc" for n in range(len(datasets))]
    return datasets, fnames


def _split_up_files_by_variable_and_day(ds, day_param):
    all_dsets = []
    all_fnames = []
    fnames_by_variable = {}
    for varname in ds.data_vars:
        var_dsets, fnames = _split_up_files_by_day(ds[[varname]], day_param)
        fnames = [f"{varname}_{fname}" for fname in fnames]
        all_dsets += var_dsets
        all_fnames += fnames
        fnames_by_variable[varname] = fnames
    return all_dsets, all_fnames, fnames_by_variable


@pytest.fixture(scope="session", params=["D", "2D"])
def netcdf_local_paths(daily_xarray_dataset, tmpdir_factory, request):
    """Return a list of paths pointing to netcdf files."""
    tmp_path = tmpdir_factory.mktemp("netcdf_data")
    # copy needed to avoid polluting metadata across multiple tests
    datasets, fnames = _split_up_files_by_day(daily_xarray_dataset.copy(), request.param)
    full_paths = [tmp_path.join(fname) for fname in fnames]
    xr.save_mfdataset(datasets, [str(path) for path in full_paths])
    items_per_file = {"D": 1, "2D": 2}[request.param]
    return full_paths, items_per_file


# TODO: this is quite repetetive of the fixture above. Replace with parametrization.
@pytest.fixture(scope="session", params=["D", "2D"])
def netcdf_local_paths_by_variable(daily_xarray_dataset, tmpdir_factory, request):
    """Return a list of paths pointing to netcdf files."""
    tmp_path = tmpdir_factory.mktemp("netcdf_data")
    datasets, fnames, fnames_by_variable = _split_up_files_by_variable_and_day(
        daily_xarray_dataset.copy(), request.param
    )
    full_paths = [tmp_path.join(fname) for fname in fnames]
    xr.save_mfdataset(datasets, [str(path) for path in full_paths])
    items_per_file = {"D": 1, "2D": 2}[request.param]
    path_format = str(tmp_path) + "/{variable}_{n:03d}.nc"
    return full_paths, items_per_file, fnames_by_variable, path_format


# TODO: refactor to allow netcdf_local_paths_by_variable to be passed without
# duplicating the whole test.
@pytest.fixture()
def netcdf_http_server(netcdf_local_paths, request):
    paths, items_per_file = netcdf_local_paths

    def make_netcdf_http_server(username="", password=""):
        first_path = paths[0]
        # assume that all files are in the same directory
        basedir = first_path.dirpath()
        fnames = [path.basename for path in paths]

        this_dir = os.path.dirname(os.path.abspath(__file__))
        port = get_open_port()
        command_list = [
            "python",
            os.path.join(this_dir, "http_auth_server.py"),
            port,
            "127.0.0.1",
            username,
            password,
        ]
        if username:
            command_list += [username, password]
        p = subprocess.Popen(command_list, cwd=basedir)
        url = f"http://127.0.0.1:{port}"
        time.sleep(1)  # let the server start up

        def teardown():
            p.kill()

        request.addfinalizer(teardown)
        return url, fnames, items_per_file

    return make_netcdf_http_server


@pytest.fixture()
def tmp_target(tmpdir_factory):
    fs = fsspec.get_filesystem_class("file")()
    path = str(tmpdir_factory.mktemp("target"))
    return FSSpecTarget(fs, path)


@pytest.fixture()
def tmp_cache(tmpdir_factory):
    path = str(tmpdir_factory.mktemp("cache"))
    fs = fsspec.get_filesystem_class("file")()
    cache = CacheFSSpecTarget(fs, path)
    return cache


@pytest.fixture()
def uninitialized_target():
    return UninitializedTarget()


@pytest.fixture
def netCDFtoZarr_sequential_recipe(daily_xarray_dataset, netcdf_local_paths, tmp_target, tmp_cache):
    paths, items_per_file = netcdf_local_paths
    kwargs = dict(
        input_urls=paths,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )
    return recipe.NetCDFtoZarrSequentialRecipe, kwargs, daily_xarray_dataset, tmp_target


@pytest.fixture
def netCDFtoZarr_sequential_multi_variable_recipe(
    daily_xarray_dataset, netcdf_local_paths_by_variable, tmp_target, tmp_cache
):
    paths, items_per_file, fnames_by_variable, path_format = netcdf_local_paths_by_variable
    nitems_per_input = items_per_file
    metadata_cache = None
    target_chunks = {}
    time_index = list(range(len(paths) // 2))
    pattern = VariableSequencePattern(
        path_format, keys={"variable": ["foo", "bar"], "n": time_index}
    )
    kwargs = dict(
        input_pattern=pattern,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=nitems_per_input,
        target=tmp_target,
        input_cache=tmp_cache,
        metadata_cache=metadata_cache,
        target_chunks=target_chunks,
    )
    return recipe.NetCDFtoZarrMultiVarSequentialRecipe, kwargs, daily_xarray_dataset, tmp_target


@pytest.fixture(scope="module")
def dask_cluster():
    cluster = LocalCluster()
    yield cluster
    cluster.close()


_executors = {
    "python": PythonPipelineExecutor,
    "dask": DaskPipelineExecutor,
    "prefect": PrefectPipelineExecutor,
    "prefect-dask": PrefectPipelineExecutor,
}


@pytest.fixture(params=["manual", "python", "dask", "prefect", "prefect-dask"])
def execute_recipe(request, dask_cluster):
    if request.param == "manual":

        def execute(r):
            for input_key in r.iter_inputs():
                r.cache_input(input_key)
            r.prepare_target()
            for chunk_key in r.iter_chunks():
                r.store_chunk(chunk_key)
            r.finalize_target()

    else:
        ExecutorClass = _executors[request.param]

        def execute(rec):
            ex = ExecutorClass()
            pipeline = rec.to_pipelines()
            plan = ex.pipelines_to_plan(pipeline)

            if request.param == "dask":
                _ = Client(dask_cluster)
            if request.param == "prefect-dask":
                from prefect.executors import DaskExecutor

                prefect_executor = DaskExecutor(address=dask_cluster.scheduler_address)
                plan.run(executor=prefect_executor)
            else:
                ex.execute_plan(plan)

    return execute
