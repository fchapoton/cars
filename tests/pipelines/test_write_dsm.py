#!/usr/bin/env python
# coding: utf8
#
# Copyright (c) 2020 Centre National d'Etudes Spatiales (CNES).
#
# This file is part of CARS
# (see https://github.com/CNES/cars).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Test module for cars/pipelines/write_dsm.py
"""
# TODO: refacto with write_dsm and compute_dsm

# Standard imports
import os
import tempfile

# Third party imports
import dask
import numpy as np
import pytest
import rasterio as rio
import xarray as xr
from affine import Affine
from osgeo import osr

# CARS imports
from cars.cluster.dask_mode import start_local_cluster, stop_local_cluster
from cars.pipelines import write_dsm
from cars.steps import rasterization

# CARS Tests imports
from ..helpers import temporary_dir


@pytest.mark.unit_tests
def test_compute_output_window():
    """
    Test the computation of destination indices slice of a given tile.
    """

    resolution = 0.3

    # add digits after the decimal point to check precision
    x_start = 25.200001
    y_start = 15.000001
    x_size = 300
    y_size = 250
    x_values_1d = np.linspace(
        x_start + 0.5 * resolution,
        x_start + resolution * (x_size + 0.5),
        x_size,
        endpoint=False,
    )
    y_values_1d = np.linspace(
        y_start - 0.5 * resolution,
        y_start - resolution * (y_size + 0.5),
        y_size,
        endpoint=False,
    )
    raster_coords = {"x": x_values_1d, "y": y_values_1d}
    tile = xr.Dataset({}, coords=raster_coords)
    bounds = (120, 150, 412.2, 511.8)

    indices = write_dsm.compute_output_window(tile, bounds, resolution)
    assert indices == (-316, 1656, -17, 1905)


@pytest.mark.unit_tests
def test_rasterio_handles():
    """
    Test to create a file handle(s) depending on whether the output data has a
    color layer or not.
    """

    bounds = (675248.0, 4897075.0, 675460.5, 4897173.0)
    resolution = 0.5
    geotransform = (bounds[0], resolution, 0.0, bounds[3], 0.0, -resolution)
    transform = Affine.from_gdal(*geotransform)
    rio_params = {
        "height": 196,
        "width": 425,
        "driver": "GTiff",
        "dtype": np.float32,
        "transform": transform,
        "crs": "EPSG:{}".format(32631),
        "tiled": True,
    }
    dsm_no_data = -32768
    color_no_data = 0
    nb_bands = 1

    # Create file handles
    with tempfile.TemporaryDirectory(dir=temporary_dir()) as directory:
        dsm_file = os.path.join(directory, "dsm.tif")
        clr_file = os.path.join(directory, "clr.tif")
        file_handles = write_dsm.rasterio_handles(
            ["hgt", "clr"],
            [dsm_file, clr_file],
            [rio_params, rio_params],
            [dsm_no_data, color_no_data],
            [1, nb_bands],
        )

        with file_handles as rio_handles:
            assert isinstance(rio_handles, dict)
            assert "hgt" in rio_handles.keys() and "clr" in rio_handles.keys()
            for _, rio_handle in rio_handles.items():
                assert isinstance(rio_handle, rio.io.DatasetWriter)


@pytest.mark.unit_tests
def test_write_geotiff_dsm():
    """
    Test to result tiles to GTiff file(s).
    """

    with tempfile.TemporaryDirectory(dir=temporary_dir()) as directory:

        # Set variables and parameters for create dataset
        resolution = 0.5
        epsg = 32630
        dsm_no_data = -32768
        hgt_no_data = dsm_no_data
        color_no_data = 0
        msk_no_data = 255
        xstart, ystart, xsize, ysize = [0, 10, 10, 10]

        raster = np.ndarray(shape=(10, 10, 2), dtype=np.float32)
        mean = np.ndarray(shape=(10, 10, 2), dtype=np.float32)
        stdev = np.ndarray(shape=(10, 10, 2), dtype=np.float32)
        n_pts = np.ndarray(shape=(10, 10), dtype=np.uint16)
        n_in_cell = np.ndarray(shape=(10, 10), dtype=np.uint16)
        msk = np.ndarray(shape=(10, 10), dtype=np.uint16)

        delayed_raster_datasets = []
        delayed_raster_datasets.append(
            dask.delayed(rasterization.create_raster_dataset)(
                raster,
                xstart,
                ystart,
                xsize,
                ysize,
                resolution,
                hgt_no_data,
                color_no_data,
                epsg,
                mean,
                stdev,
                n_pts,
                n_in_cell,
                msk,
            )
        )

        # Start cluster with a local cluster
        nb_workers = 4
        cluster, client = start_local_cluster(nb_workers)

        future_dsm = client.compute(delayed_raster_datasets)

        # Write netcdf with raster_datasets
        bounds = [xstart, ystart, xsize, ysize]
        nb_bands = 1
        dsm_file = os.path.join(directory, "dsm.tif")
        clr_file = os.path.join(directory, "clr.tif")
        msk_file = os.path.join(directory, "msk.tif")
        write_dsm.write_geotiff_dsm(
            future_dsm,
            directory,
            xsize,
            ysize,
            bounds,
            resolution,
            epsg,
            nb_bands,
            dsm_no_data,
            color_no_data,
            write_msk=True,
            msk_no_data=msk_no_data,
        )

        # stop cluster
        stop_local_cluster(cluster, client)

        width_ref = xsize
        height_ref = ysize
        crs = osr.SpatialReference()
        crs.ImportFromEPSG(epsg)
        crs_ref = crs.ExportToWkt()
        geotransform = (bounds[0], resolution, 0.0, bounds[3], 0.0, -resolution)
        transform_ref = Affine.from_gdal(*geotransform)

        # Compare DSM
        with rio.open(dsm_file) as rio_actual:
            np.testing.assert_equal(rio_actual.width, width_ref)
            np.testing.assert_equal(rio_actual.height, height_ref)
            assert rio_actual.transform == transform_ref
            assert rio_actual.crs == crs_ref
            assert rio_actual.nodata == hgt_no_data

            for i in range(rio_actual.width):
                for j in range(rio_actual.height):
                    if np.isnan(raster[i][j][0]):
                        assert rio_actual.read()[0][i][j] == dsm_no_data
                    else:
                        assert rio_actual.read()[0][i][j] == raster[i][j][0]

        # Compare CLR
        with rio.open(clr_file) as rio_actual:
            np.testing.assert_equal(rio_actual.width, width_ref)
            np.testing.assert_equal(rio_actual.height, height_ref)
            assert rio_actual.transform == transform_ref
            assert rio_actual.crs == crs_ref
            assert rio_actual.nodata == color_no_data

            for i in range(rio_actual.width):
                for j in range(rio_actual.height):
                    if np.isnan(raster[i][j][1]):
                        assert rio_actual.read()[0][i][j] == color_no_data
                    else:
                        assert rio_actual.read()[0][i][j] == raster[i][j][1]

        # Compare MSK
        with rio.open(msk_file) as rio_actual:
            np.testing.assert_equal(rio_actual.width, width_ref)
            np.testing.assert_equal(rio_actual.height, height_ref)
            assert rio_actual.transform == transform_ref
            assert rio_actual.crs == crs_ref
            assert rio_actual.nodata == msk_no_data

            for i in range(rio_actual.width):
                for j in range(rio_actual.height):
                    if np.isnan(raster[i][j][1]):
                        assert rio_actual.read()[0][i][j] == msk_no_data
                    else:
                        assert rio_actual.read()[0][i][j] == msk[i][j]
