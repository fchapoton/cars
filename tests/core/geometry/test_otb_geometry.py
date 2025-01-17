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
Test module for cars/core/geometry/otb_geometry.py
"""

import os
import tempfile
from shutil import copy2

# Third party imports
import numpy as np
import otbApplication
import pytest
import rasterio as rio

# CARS imports
from cars.core.geometry import AbstractGeometry

# CARS Tests imports
from ...helpers import (
    absolute_data_path,
    otb_geoid_file_set,
    otb_geoid_file_unset,
    temporary_dir,
)


def rigid_transform_resample(
    img: str, scale_x: float, scale_y: float, img_transformed: str
):
    """
    Execute RigidTransformResample OTB application

    :param img: path to the image to transform
    :param scale_x: scale factor to apply along x axis
    :param scale_y: scale factor to apply along y axis
    :param img_transformed: output image path
    """

    # create otb app to rescale input images
    app = otbApplication.Registry.CreateApplication("RigidTransformResample")

    app.SetParameterString("in", img)
    app.SetParameterString("transform.type", "id")
    app.SetParameterFloat("transform.type.id.scalex", abs(scale_x))
    app.SetParameterFloat("transform.type.id.scaley", abs(scale_y))
    app.SetParameterString("out", img_transformed)
    app.ExecuteAndWriteOutput()


@pytest.mark.unit_tests
def test_generate_epipolar_grids():
    """
    Test if the pipeline is correctly built and produces consistent grids
    """
    img1 = absolute_data_path("input/phr_ventoux/left_image.tif")
    img2 = absolute_data_path("input/phr_ventoux/right_image.tif")
    dem = absolute_data_path("input/phr_ventoux/srtm")
    step = 45

    # Set the geoid file from code source
    otb_geoid_file_set()

    geo_plugin = (
        AbstractGeometry(  # pylint: disable=abstract-class-instantiated
            "OTBGeometry"
        )
    )

    (
        left_grid_as_array,
        right_grid_as_array,
        origin,
        spacing,
        epipolar_size,
        disp_to_alt_ratio,
    ) = geo_plugin.generate_epipolar_grids(img1, img2, dem, epipolar_step=step)

    assert epipolar_size == [612, 612]
    assert left_grid_as_array.shape == (15, 15, 2)
    assert origin[0] == 0
    assert origin[1] == 0
    assert spacing[0] == step
    assert spacing[1] == step
    assert np.isclose(disp_to_alt_ratio, 1 / 0.7, 0.01)

    # Uncomment to update baseline
    # np.save(absolute_data_path("ref_output/left_grid.npy"), left_grid_np)

    left_grid_np_reference = np.load(
        absolute_data_path("ref_output/left_grid.npy")
    )
    np.testing.assert_allclose(left_grid_as_array, left_grid_np_reference)

    assert right_grid_as_array.shape == (15, 15, 2)

    # Uncomment to update baseline
    # np.save(absolute_data_path("ref_output/right_grid.npy"), right_grid_np)

    right_grid_np_reference = np.load(
        absolute_data_path("ref_output/right_grid.npy")
    )
    np.testing.assert_allclose(right_grid_as_array, right_grid_np_reference)

    # unset otb geoid file
    otb_geoid_file_unset()


@pytest.mark.unit_tests
def test_generate_epipolar_grids_scaled_inputs():
    """
    test different pixel sizes in input images
    """
    img1 = absolute_data_path("input/phr_ventoux/left_image.tif")
    img2 = absolute_data_path("input/phr_ventoux/right_image.tif")
    dem = absolute_data_path("input/phr_ventoux/srtm")
    step = 45

    # Set the geoid file from code source
    otb_geoid_file_set()

    geo_plugin = (
        AbstractGeometry(  # pylint: disable=abstract-class-instantiated
            "OTBGeometry"
        )
    )

    # reference
    (
        _,
        _,
        _,
        _,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
    ) = geo_plugin.generate_epipolar_grids(img1, img2, dem, epipolar_step=step)

    # define negative scale transform
    def create_negative_transform(srs_img, dst_img, reverse_x, reverse_y):
        """
        Reverse transform on x or y axis if reverse_x or reverse_y are activated
        :param srs_img:
        :type srs_img: str
        :param dst_img:
        :type dst_img: str
        :param reverse_x:
        :type srs_img: bool
        :param reverse_y:
        :type srs_img: bool
        :return:
        """
        with rio.open(srs_img, "r") as rio_former_dst:
            former_array = rio_former_dst.read(1)
            former_transform = rio_former_dst.transform
            # modify transform
            x_fact = 1
            y_fact = 1
            x_size = 0
            y_size = 0

            if reverse_x:
                x_fact = -1
                x_size = former_array.shape[0] * abs(former_transform[0])
            if reverse_y:
                y_fact = -1
                y_size = former_array.shape[1] * abs(former_transform[4])
            new_transform = rio.Affine(
                x_fact * former_transform[0],
                former_transform[1],
                x_size + former_transform[2],
                former_transform[3],
                y_fact * former_transform[4],
                y_size + former_transform[5],
            )

            with rio.open(
                dst_img,
                "w",
                driver="GTiff",
                height=former_array.shape[0],
                width=former_array.shape[1],
                count=1,
                dtype=former_array.dtype,
                crs=rio_former_dst.crs,
                transform=new_transform,
            ) as rio_dst:
                rio_dst.write(former_array, 1)

    # define generic test
    def test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex,
        scaley,
    ):
        """
        Test that epipolar image size and disp_to_alt_ratio remain unchanged
        when scaling the input images

        tested combinations:
        - scaled img1 and scaled img2
        - img1 and scaled img2
        - scaled img1 and img2
        """

        with tempfile.TemporaryDirectory(dir=temporary_dir()) as directory:
            # manage negative scaling
            negative_scale_x = scalex < 0
            negative_scale_y = scaley < 0

            # rescale inputs
            img1_transform = os.path.join(directory, "img1_transform.tif")
            img2_transform = os.path.join(directory, "img2_transform.tif")

            if negative_scale_x or negative_scale_y:
                # create new images
                img1_geom = img1.replace(".tif", ".geom")
                img2_geom = img2.replace(".tif", ".geom")
                img1_reversed = os.path.join(directory, "img1_reversed.tif")
                img2_reversed = os.path.join(directory, "img2_reversed.tif")
                img1_reversed_geom = os.path.join(
                    directory, "img1_reversed.geom"
                )
                img2_reversed_geom = os.path.join(
                    directory, "img2_reversed.geom"
                )
                copy2(img1_geom, img1_reversed_geom)
                copy2(img2_geom, img2_reversed_geom)
                create_negative_transform(
                    img1, img1_reversed, negative_scale_x, negative_scale_y
                )
                create_negative_transform(
                    img2, img2_reversed, negative_scale_x, negative_scale_y
                )
                img1 = img1_reversed
                img2 = img2_reversed

            rigid_transform_resample(img1, scalex, scaley, img1_transform)
            rigid_transform_resample(img2, scalex, scaley, img2_transform)

            with rio.open(img1_transform, "r") as rio_dst:
                pixel_size_x, pixel_size_y = (
                    rio_dst.transform[0],
                    rio_dst.transform[4],
                )
                assert pixel_size_x == 1 / scalex
                assert pixel_size_y == 1 / scaley

            with rio.open(img2_transform, "r") as rio_dst:
                pixel_size_x, pixel_size_y = (
                    rio_dst.transform[0],
                    rio_dst.transform[4],
                )
                assert pixel_size_x == 1 / scalex
                assert pixel_size_y == 1 / scaley

            geo_plugin = (
                AbstractGeometry(  # pylint: disable=abstract-class-instantiated
                    "OTBGeometry"
                )
            )

            # img1_transform / img2_transform
            (
                _,
                _,
                _,
                _,
                epipolar_size,
                disp_to_alt_ratio,
            ) = geo_plugin.generate_epipolar_grids(
                img1_transform, img2_transform, dem, epipolar_step=step
            )

            assert epipolar_size == ref_epipolar_size
            assert abs(disp_to_alt_ratio - ref_disp_to_alt_ratio) < 1e-06

            # img1_transform / img2
            (
                _,
                _,
                _,
                _,
                epipolar_size,
                disp_to_alt_ratio,
            ) = geo_plugin.generate_epipolar_grids(
                img1_transform, img2, dem, epipolar_step=step
            )

            assert epipolar_size == ref_epipolar_size
            assert abs(disp_to_alt_ratio - ref_disp_to_alt_ratio) < 1e-06

            # img1 / img2_transform
            (
                _,
                _,
                _,
                _,
                epipolar_size,
                disp_to_alt_ratio,
            ) = geo_plugin.generate_epipolar_grids(
                img1_transform, img2, dem, epipolar_step=step
            )

            assert epipolar_size == ref_epipolar_size
            assert abs(disp_to_alt_ratio - ref_disp_to_alt_ratio) < 1e-06

    # test with scalex= 2, scaley=2
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=2.0,
        scaley=2.0,
    )
    # test with scalex= 2, scaley=3
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=2.0,
        scaley=3.0,
    )
    # test with scalex= 0.5, scaley=0.5
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=1 / 2.0,
        scaley=1 / 2.0,
    )
    # test with scalex= 0.5, scaley=0.25
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=1 / 2.0,
        scaley=1 / 4.0,
    )

    # test with scalex= 1, scaley=-1
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=1.0,
        scaley=-1.0,
    )

    # test with scalex= -1, scaley=1
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=-1.0,
        scaley=1.0,
    )

    # test with scalex= -1, scaley=-2
    test_with_scaled_inputs(
        img1,
        img2,
        dem,
        step,
        ref_epipolar_size,
        ref_disp_to_alt_ratio,
        scalex=-1.0,
        scaley=-2.0,
    )

    # unset otb geoid file
    otb_geoid_file_unset()
