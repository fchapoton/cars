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
Main 3D CARS Compute DSM pipeline module:
contains all the functions associated with its cars subcommand.
TODO: refactor in several files and remove too-many-lines
"""
# pylint: disable=too-many-lines

# Standard imports
from __future__ import print_function

import errno
import logging
import math
import multiprocessing as mp
import os
from collections import Counter
from glob import glob
from typing import Dict, List, Tuple

# Third party imports
import dask
import numpy as np
import xarray as xr
from json_checker import CheckerError
from osgeo import gdal
from shapely.geometry import Polygon
from tqdm import tqdm

# CARS imports
from cars import __version__
from cars.cluster.dask_mode import (
    ComputeDSMMemoryLogger,
    start_cluster,
    start_local_cluster,
    stop_cluster,
)
from cars.cluster.tbb import check_tbb_installed
from cars.conf import input_parameters as in_params
from cars.conf import (
    log_conf,
    mask_classes,
    output_compute_dsm,
    output_prepare,
    static_conf,
)
from cars.core import constants as cst
from cars.core import inputs, outputs, projection, tiling, utils
from cars.externals import otb_pipelines
from cars.pipelines import wrappers, write_dsm
from cars.steps import rasterization
from cars.steps.epi_rectif import grids
from cars.steps.matching import dense_matching


def write_3d_points(
    configuration, region, corr_config, tmp_dir, config_id, **kwargs
):
    """
    Wraps the call to wrappers.images_pair_to_3d_points
    and write down the output points and colors

    :param configuration: configuration values
    :param region: region to process
    :param corr_config: correlator configuration
    :param tmp_dir: temporary directory to store outputs
    :param config_id: id of the pair to process
    """
    config_id_dir = os.path.join(tmp_dir, config_id)
    hashed_region = tiling.region_hash_string(region)
    points_dir = os.path.join(config_id_dir, "points")
    color_dir = os.path.join(config_id_dir, "color")

    # Compute 3d points and colors
    points, colors = wrappers.images_pair_to_3d_points(
        configuration, region, corr_config, **kwargs
    )

    # Create output directories
    utils.safe_makedirs(points_dir)
    utils.safe_makedirs(color_dir)

    # Write to netcdf the 3D points and color,
    # # for both cst.STEREO_REF and cst.STEREO_SEC modes
    # # output paths end with '_ref.nc' or '_sec.nc'
    out_points = {}
    out_colors = {}
    for key, value in points.items():
        out_path = os.path.join(
            points_dir, "{}_{}.nc".format(hashed_region, key)
        )
        value.to_netcdf(out_path)
        out_points[key] = out_path

    for key, value in colors.items():
        out_path = os.path.join(
            color_dir, "{}_{}.nc".format(hashed_region, key)
        )
        value.to_netcdf(out_path)
        out_colors[key] = out_path

    # outputs are the temporary files paths
    return out_points, out_colors


def write_dsm_by_tile(
    clouds_and_colors_as_str_list: List[str],
    resolution: float,
    epsg: int,
    tmp_dir: str,
    nb_bands: int,
    color_dtype: np.dtype,
    output_stats: bool,
    write_msk: bool,
    **kwargs
) -> str:
    """
    Wraps the call to rasterization_wrapper and write the dsm tile on disk

    :param clouds_and_colors_as_str_list:  3D points & colors paths to rasterize
    :param resolution: output DSM resolution
    :param epsg: EPSG code of output DSM
    :param tmp_dir: directory to store output DSM tiles
    :param nb_bands: number of bands in color image
    :param color_dtype: type to use for the ortho-image
    :param output_stats: True if we save statistics with DSM tiles
    :param write_msk: boolean enabling the rasterized mask's writting
    :param kwargs: all the keyword arguments passed to rasterization_wrapper
    :return the region hash string
    """
    # replace paths by opened Xarray datasets
    def xr_open_dict(cloud_path: Dict) -> Dict:
        """
        Transform 3D cloud points or color path to xarray
        :param cloud_path: a 3D points or color str path dict
        :return: a xarray dict (as input cloud_path dict)
        """
        xr_dict = {
            name: xr.open_dataset(path) for name, path in cloud_path.items()
        }
        return xr_dict

    clouds_and_colors_as_xr_list = [
        (xr_open_dict(paths[0]), xr_open_dict(paths[1]))
        for paths in clouds_and_colors_as_str_list
    ]

    # Extract some kwargs keyword arguments from rasterization_wrapper
    xstart = kwargs.get("xstart")
    ystart = kwargs.get("ystart")
    xsize = kwargs.get("xsize")
    ysize = kwargs.get("ysize")

    dsm_nodata = kwargs.get("dsm_no_data")
    color_nodata = kwargs.get("color_no_data")
    msk_nodata = kwargs.get("msk_no_data")

    hashed_region = tiling.region_hash_string([xstart, ystart, xsize, ysize])

    # call to rasterization_wrapper
    dsm = rasterization_wrapper(
        clouds_and_colors_as_xr_list, resolution, epsg, **kwargs
    )

    # compute tile bounds
    tile_bounds = [
        xstart,
        ystart - resolution * ysize,
        xstart + resolution * xsize,
        ystart,
    ]

    # write DSM tile as geoTIFF
    write_dsm.write_geotiff_dsm(
        [dsm],
        tmp_dir,
        xsize,
        ysize,
        tile_bounds,
        resolution,
        epsg,
        nb_bands,
        dsm_nodata,
        color_nodata,
        color_dtype=color_dtype,
        write_color=True,
        write_stats=output_stats,
        write_msk=write_msk,
        msk_no_data=msk_nodata,
        prefix=hashed_region + "_",
    )

    return hashed_region


def rasterization_wrapper(clouds_and_colors, resolution, epsg, **kwargs):
    """
    Wrapper for rasterization step :
    - Convert a list of clouds to correct epsg
    - Rasterize it with associated colors

    :param clouds_and_colors: list of tuple (cloud, colors)
    :type clouds_and_colors: list of pair of xarray
    :param resolution: Produced DSM resolution (meter, degree [EPSG dependent])
    :type resolution: float
    :param  epsg_code: epsg code for the CRS of the output DSM
    :type epsg_code: int
    :return: digital surface model + projected colors
    :rtype: xarray 2d tuple
    """
    # Unpack list of clouds from tuple, and project them to correct EPSG if
    # needed
    clouds = [v[0][cst.STEREO_REF] for v in clouds_and_colors]

    # Unpack list of colors alike
    colors = [v[1][cst.STEREO_REF] for v in clouds_and_colors]

    # Add clouds and colors computed from the secondary disparity map
    if cst.STEREO_SEC in clouds_and_colors[0][0]:
        cloud_sec = [v[0][cst.STEREO_SEC] for v in clouds_and_colors]
        clouds.extend(cloud_sec)

        color_sec = [v[1][cst.STEREO_SEC] for v in clouds_and_colors]
        colors.extend(color_sec)

    # Call simple_rasterization
    return rasterization.simple_rasterization_dataset(
        clouds, resolution, epsg, colors, **kwargs
    )


def run(  # noqa: C901
    in_jsons: List[output_prepare.PreprocessingContentType],
    out_dir: str,
    resolution: float = 0.5,
    min_elevation_offset: float = None,
    max_elevation_offset: float = None,
    epsg: int = None,
    sigma: float = None,
    dsm_radius: int = 1,
    dsm_no_data: int = -32768,
    msk_no_data: int = 65535,
    color_no_data: int = 0,
    corr_config: Dict = None,
    output_stats: bool = False,
    mode: str = "local_dask",
    nb_workers: int = 4,
    walltime: str = "00:59:00",
    roi: Tuple[List[int], int] = None,
    use_geoid_alt: bool = False,
    use_sec_disp: bool = False,
    snap_to_img1: bool = False,
    align: bool = False,
    cloud_small_components_filter: bool = True,
    cloud_statistical_outliers_filter: bool = True,
    epi_tile_size: int = None,
):
    """
    Main function for the compute_dsm pipeline subcommand

    It computes independent tiles of the final DSM, with the following steps:

    1. Epipolar resampling (including mask)
    2. Disparity map estimation
    3. Triangulation of disparity map
    4. Rasterization to DSM

    :param in_jsons: Input pair dictionaries (as produced by cars prepare step)
    :param out_dir: Computed raster and color image output directory
    :param resolution: DSM resolution to produce
    :param min_elevation_offset: Override minimum disparity
                                 from prepare step with this offset in meters
    :param max_elevation_offset: Override maximum disparity
                                 from prepare step with this offset in meters
    :param epsg: Output DSM Coordinate Reference System EPSG code
    :param sigma: Rasterization width of gaussian weight
    :param dsm_radius: Rasterization radius around a cell for gathering points
    :param dsm_no_data: No data value to use in the final DSM file
    :param color_no_data: No data value to use in the final colored image
    :param msk_no_data: No data value to use in the final mask image
    :param corr_config: Correlator configuration
    :param output_stats: Ouput DSM associated quality statistics flag boolean
    :param mode: Parallelization mode
    :param nb_workers: Number of dask workers to use for the sift matching step
    :param walltime: Walltime of the dask workers
    :param roi: DSM Region Of Interest in final projection with EPSG reference
                ([xmin, ymin, xmax, ymax], roi_epsg))
                (roi_epsg can be set to None if the ROI is in final projection)
    :param use_geoid_alt: Geoid height reference for DSM altitude flag.
    :param use_sec_disp: Secondary disparity map activation flag.
    :param snap_to_img1: Force Img2 / Img1 Lines of Sight crossing flag.
    :param align: If this is True, use the correction estimated during prepare
                  to align to lowres DEM (if available)
    :param cloud_small_components_filter:
                Activating the points cloud small components filtering.
                The filter's parameters are set in static configuration json.
    :param cloud_statistical_outliers_filter:
                Activating the points cloud statistical outliers filtering.
                The filter's parameters are set in static configuration json.
    :param epi_tile_size: Force the size of epipolar tiles (None by default)
    """
    out_dir = os.path.abspath(out_dir)
    # Ensure that outdir exists
    try:
        os.makedirs(out_dir)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(out_dir):
            pass
        else:
            raise
    tmp_dir = os.path.join(out_dir, "tmp")

    log_conf.add_log_file(out_dir, "compute_dsm")
    logging.info(
        "Received {} stereo pairs configurations".format(len(in_jsons))
    )

    # Retrieve static parameters (rasterization and cloud filtering)
    static_params = static_conf.get_cfg()

    # Initiate ouptut json dictionary
    out_json = {
        output_compute_dsm.COMPUTE_DSM_INPUTS_SECTION_TAG: [],
        output_compute_dsm.COMPUTE_DSM_SECTION_TAG: {
            output_compute_dsm.COMPUTE_DSM_VERSION_TAG: __version__,
            output_compute_dsm.COMPUTE_DSM_PARAMETERS_SECTION_TAG: {
                output_compute_dsm.RESOLUTION_TAG: resolution,
                output_compute_dsm.SIGMA_TAG: sigma,
                output_compute_dsm.DSM_RADIUS_TAG: dsm_radius,
            },
            in_params.STATIC_PARAMS_TAG: {
                static_conf.compute_dsm_tag: static_params[
                    static_conf.compute_dsm_tag
                ],
                static_conf.plugins_tag: static_params[static_conf.plugins_tag],
            },
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG: {
                output_compute_dsm.ALIGN_OPTION: align,
                output_compute_dsm.SNAP_TO_IMG1_OPTION: snap_to_img1,
            },
        },
    }

    if use_geoid_alt:
        geoid_data = inputs.read_geoid_file()
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.ALT_REFERENCE_TAG] = "geoid"
    else:
        geoid_data = None
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.ALT_REFERENCE_TAG] = "ellipsoid"

    if epsg is not None:
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_PARAMETERS_SECTION_TAG
        ][output_compute_dsm.EPSG_TAG] = epsg

    roi_epsg = None
    if roi is not None:
        (roi_xmin, roi_ymin, roi_xmax, roi_ymax), roi_epsg = roi
        roi_poly = Polygon(
            [
                (roi_xmin, roi_ymin),
                (roi_xmax, roi_ymin),
                (roi_xmax, roi_ymax),
                (roi_xmin, roi_ymax),
                (roi_xmin, roi_ymin),
            ]
        )

    # set the timeout for each job in multiprocessing mode (in seconds)
    per_job_timeout = 600

    configurations_data = {}

    config_idx = 1

    ref_left_img = None

    write_msk = False

    for in_json in in_jsons:
        # Build config id
        config_id = "config_{}".format(config_idx)

        # Check configuration with respect to schema
        configuration = inputs.check_json(
            in_json, output_prepare.PREPROCESSING_CONTENT_SCHEMA
        )

        # retrieve masks classes usages
        mask1_classes = configuration[in_params.INPUT_SECTION_TAG].get(
            in_params.MASK1_CLASSES_TAG, None
        )
        mask2_classes = configuration[in_params.INPUT_SECTION_TAG].get(
            in_params.MASK2_CLASSES_TAG, None
        )

        classes_usage = {}
        if mask1_classes is not None:
            mask1_classes_dict = mask_classes.read_mask_classes(mask1_classes)
            classes_usage[
                output_compute_dsm.MASK1_IGNORED_BY_CORR_TAG
            ] = mask1_classes_dict.get(mask_classes.ignored_by_corr_tag, None)
            classes_usage[
                output_compute_dsm.MASK1_SET_TO_REF_ALT_TAG
            ] = mask1_classes_dict.get(mask_classes.set_to_ref_alt_tag, None)

        if mask2_classes is not None:
            mask2_classes_dict = mask_classes.read_mask_classes(mask2_classes)
            classes_usage[
                output_compute_dsm.MASK2_IGNORED_BY_CORR_TAG
            ] = mask2_classes_dict.get(mask_classes.ignored_by_corr_tag, None)
            classes_usage[
                output_compute_dsm.MASK2_SET_TO_REF_ALT_TAG
            ] = mask2_classes_dict.get(mask_classes.set_to_ref_alt_tag, None)

        # Append input configuration to output json
        out_json_config = {
            output_compute_dsm.COMPUTE_DSM_INPUT_TAG: configuration,
        }

        if mask1_classes is not None or mask2_classes is not None:
            out_json_config[
                output_compute_dsm.COMPUTE_DSM_MASK_CLASSES_USAGE_TAG
            ] = classes_usage

        out_json[output_compute_dsm.COMPUTE_DSM_INPUTS_SECTION_TAG].append(
            out_json_config
        )

        configurations_data[config_id] = {}

        configurations_data[config_id]["configuration"] = configuration

        # Get local conf left image for this in_json iteration
        conf_left_img = configuration[in_params.INPUT_SECTION_TAG][
            in_params.IMG1_TAG
        ]

        # Check left image and raise a warning
        # if different left images are used along with snap_to_img1 mode
        if ref_left_img is None:
            ref_left_img = conf_left_img
        else:
            if snap_to_img1 and ref_left_img != conf_left_img:
                logging.warning(
                    "--snap_to_left_image mode is used but input "
                    "configurations have different images as their "
                    "left image in pair. This may result in "
                    "increasing registration discrepencies between pairs"
                )

        # If mask1 and/or mask2 are set in the prepare input configuration json
        # then the DSM rasterized mask will be written alongside the DSM
        # TODO : Mask 2 ?
        mask1 = configuration[in_params.INPUT_SECTION_TAG].get(
            in_params.MASK1_TAG, None
        )
        if mask1 is not None:
            write_msk = True

        # Get Preprocessing output config
        preprocessing_output_config = configuration[
            output_prepare.PREPROCESSING_SECTION_TAG
        ][output_prepare.PREPROCESSING_OUTPUT_SECTION_TAG]

        # Get largest epipolar regions from configuration file
        largest_epipolar_region = [
            0,
            0,
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_X_TAG],
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_Y_TAG],
        ]

        configurations_data[config_id][
            "largest_epipolar_region"
        ] = largest_epipolar_region

        disp_min = preprocessing_output_config[
            output_prepare.MINIMUM_DISPARITY_TAG
        ]
        disp_max = preprocessing_output_config[
            output_prepare.MAXIMUM_DISPARITY_TAG
        ]
        disp_to_alt_ratio = preprocessing_output_config[
            output_prepare.DISP_TO_ALT_RATIO_TAG
        ]

        # Check if we need to override disp_min
        if min_elevation_offset is not None:
            user_disp_min = min_elevation_offset / disp_to_alt_ratio
            if user_disp_min > disp_min:
                logging.warning(
                    (
                        "Overriden disparity minimum "
                        "= {:.3f} pix. (= {:.3f} m.) "
                        "is greater than disparity minimum estimated "
                        "in prepare step = {:.3f} pix. (or {:.3f} m.) "
                        "for configuration {}"
                    ).format(
                        user_disp_min,
                        min_elevation_offset,
                        disp_min,
                        disp_min * disp_to_alt_ratio,
                        config_id,
                    )
                )
                disp_min = user_disp_min

        # Check if we need to override disp_max
        if max_elevation_offset is not None:
            user_disp_max = max_elevation_offset / disp_to_alt_ratio
            if user_disp_max < disp_max:
                logging.warning(
                    (
                        "Overriden disparity maximum "
                        "= {:.3f} pix. (or {:.3f} m.) "
                        "is lower than disparity maximum estimated "
                        "in prepare step = {:.3f} pix. (or {:.3f} m.) "
                        "for configuration {}"
                    ).format(
                        user_disp_max,
                        max_elevation_offset,
                        disp_max,
                        disp_max * disp_to_alt_ratio,
                        config_id,
                    )
                )
            disp_max = user_disp_max

        logging.info(
            "Disparity range for config {}: [{:.3f} pix., {:.3f} pix.] "
            "(or [{:.3f} m., {:.3f} m.])".format(
                config_id,
                disp_min,
                disp_max,
                disp_min * disp_to_alt_ratio,
                disp_max * disp_to_alt_ratio,
            )
        )

        configurations_data[config_id]["disp_min"] = disp_min
        configurations_data[config_id]["disp_max"] = disp_max

        origin = [
            preprocessing_output_config[output_prepare.EPIPOLAR_ORIGIN_X_TAG],
            preprocessing_output_config[output_prepare.EPIPOLAR_ORIGIN_Y_TAG],
        ]
        spacing = [
            preprocessing_output_config[output_prepare.EPIPOLAR_SPACING_X_TAG],
            preprocessing_output_config[output_prepare.EPIPOLAR_SPACING_Y_TAG],
        ]

        configurations_data[config_id]["origin"] = origin
        configurations_data[config_id]["spacing"] = spacing

        logging.info(
            "Size of epipolar image: {}".format(largest_epipolar_region)
        )
        logging.debug("Origin of epipolar grid: {}".format(origin))
        logging.debug("Spacing of epipolar grid: {}".format(spacing))

        # Warning if align is set but correction is missing
        param_lowres_tag = output_prepare.LOWRES_DEM_SPLINES_FIT_TAG
        if align and param_lowres_tag not in preprocessing_output_config:
            logging.warning(
                (
                    "Align with low resolution DSM option is set but splines "
                    "correction file is not available for configuration {}. "
                    "Correction will not be applied for this configuration"
                ).format(config_id)
            )

        # Numpy array with corners of largest epipolar region.
        # Order does not matter here,
        # since it will be passed to grids.compute_epipolar_grid_min_max
        corners = np.array(
            [
                [
                    [largest_epipolar_region[0], largest_epipolar_region[1]],
                    [largest_epipolar_region[0], largest_epipolar_region[3]],
                ],
                [
                    [largest_epipolar_region[2], largest_epipolar_region[3]],
                    [largest_epipolar_region[2], largest_epipolar_region[1]],
                ],
            ],
            dtype=np.float64,
        )

        # get UTM zone with the middle point of terrain_min if epsg is None
        if epsg is None:
            # Compute epipolar image terrain position corners
            # for min and max disparity
            (
                terrain_dispmin,
                terrain_dispmax,
            ) = grids.compute_epipolar_grid_min_max(
                corners, 4326, configuration, disp_min, disp_max
            )

            epsg = otb_pipelines.get_utm_zone_as_epsg_code(
                *np.mean(terrain_dispmin, axis=0)
            )

            logging.info("EPSG code: {}".format(epsg))

        # Compute terrain min and max again, this time using estimated epsg code
        terrain_dispmin, terrain_dispmax = grids.compute_epipolar_grid_min_max(
            corners, epsg, configuration, disp_min, disp_max
        )

        if roi_epsg is not None:
            if roi_epsg != epsg:
                roi_poly = projection.polygon_projection(
                    roi_poly, roi_epsg, epsg
                )

        # Compute bounds from epipolar image corners and dispmin/dispmax
        terrain_bounds = np.stack((terrain_dispmin, terrain_dispmax), axis=0)
        terrain_min = np.amin(terrain_bounds, axis=(0, 1))
        terrain_max = np.amax(terrain_bounds, axis=(0, 1))

        terrain_area = (terrain_max[0] - terrain_min[0]) * (
            terrain_max[1] - terrain_min[1]
        )

        configurations_data[config_id]["terrain_area"] = terrain_area

        logging.info(
            "Terrain area covered: {} square meters (or square degrees)".format(
                terrain_area
            )
        )

        # Retrieve bounding box of the ground intersection of the envelopes
        inter_poly, inter_epsg = inputs.read_vector(
            preprocessing_output_config[
                output_prepare.ENVELOPES_INTERSECTION_TAG
            ]
        )

        if epsg != inter_epsg:
            inter_poly = projection.polygon_projection(
                inter_poly, inter_epsg, epsg
            )

        (inter_xmin, inter_ymin, inter_xmax, inter_ymax) = inter_poly.bounds

        # Align bounding box to integer resolution steps
        xmin, ymin, xmax, ymax = tiling.snap_to_grid(
            inter_xmin, inter_ymin, inter_xmax, inter_ymax, resolution
        )

        logging.info(
            "Terrain bounding box : [{}, {}] x [{}, {}]".format(
                xmin, xmax, ymin, ymax
            )
        )

        configurations_data[config_id]["terrain_bounding_box"] = [
            xmin,
            ymin,
            xmax,
            ymax,
        ]

        if roi is not None:
            if not roi_poly.intersects(inter_poly):
                logging.warning(
                    "The pair composed of {} and {} "
                    "does not intersect the requested ROI".format(
                        configuration[in_params.INPUT_SECTION_TAG][
                            in_params.IMG1_TAG
                        ],
                        configuration[in_params.INPUT_SECTION_TAG][
                            in_params.IMG2_TAG
                        ],
                    )
                )

        # Get optimal tile size
        if epi_tile_size is not None:
            opt_epipolar_tile_size = epi_tile_size
        else:
            tiling_params = static_conf.get_tiling_params()
            opt_epipolar_tile_size = (
                dense_matching.optimal_tile_size_pandora_plugin_libsgm(
                    disp_min,
                    disp_max,
                    getattr(tiling_params, static_conf.min_epi_tile_size_tag),
                    getattr(tiling_params, static_conf.max_epi_tile_size_tag),
                    margin=getattr(
                        tiling_params, static_conf.epi_tile_margin_tag
                    ),
                )
            )

        logging.info(
            "Optimal tile size for epipolar regions: "
            "{size}x{size} pixels".format(size=opt_epipolar_tile_size)
        )

        configurations_data[config_id][
            "opt_epipolar_tile_size"
        ] = opt_epipolar_tile_size

        # Split epipolar image in pieces
        epipolar_regions = tiling.split(
            0,
            0,
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_X_TAG],
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_Y_TAG],
            opt_epipolar_tile_size,
            opt_epipolar_tile_size,
        )

        epipolar_regions_grid = tiling.grid(
            0,
            0,
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_X_TAG],
            preprocessing_output_config[output_prepare.EPIPOLAR_SIZE_Y_TAG],
            opt_epipolar_tile_size,
            opt_epipolar_tile_size,
        )

        configurations_data[config_id]["epipolar_regions"] = epipolar_regions
        configurations_data[config_id][
            "epipolar_regions_grid"
        ] = epipolar_regions_grid

        logging.info(
            "Epipolar image will be processed in {} splits".format(
                len(epipolar_regions)
            )
        )

        # Increment config index
        config_idx += 1

    xmin, ymin, xmax, ymax = tiling.union(
        [
            conf["terrain_bounding_box"]
            for config_id, conf in configurations_data.items()
        ]
    )

    if roi is not None:
        # terrain bounding box polygon
        terrain_poly = Polygon(
            [
                (xmin, ymin),
                (xmax, ymin),
                (xmax, ymax),
                (xmin, ymax),
                (xmin, ymin),
            ]
        )

        if not roi_poly.intersects(terrain_poly):
            raise Exception(
                "None of the input pairs intersect the requested ROI"
            )
        # Show ROI if valid (no exception raised) :
        logging.info("Setting terrain bounding box to the requested ROI")
        xmin, ymin, xmax, ymax = roi_poly.bounds
        xmin, ymin, xmax, ymax = tiling.snap_to_grid(
            xmin, ymin, xmax, ymax, resolution
        )

    logging.info(
        "Total terrain bounding box : [{}, {}] x [{}, {}]".format(
            xmin, xmax, ymin, ymax
        )
    )

    # Compute optimal terrain tile size
    optimal_terrain_tile_widths = []

    for _, conf in configurations_data.items():
        # Compute terrain area covered by a single epipolar tile
        terrain_area_covered_by_epipolar_tile = conf["terrain_area"] / len(
            epipolar_regions
        )

        # Compute tile width in pixels
        optimal_terrain_tile_widths.append(
            math.sqrt(terrain_area_covered_by_epipolar_tile)
        )

    # In case of multiple json configuration, take the average optimal size,
    # and align to multiple of resolution
    optimal_terrain_tile_width = (
        int(math.ceil(np.mean(optimal_terrain_tile_widths) / resolution))
        * resolution
    )

    logging.info(
        "Optimal terrain tile size: {}x{} pixels".format(
            int(optimal_terrain_tile_width / resolution),
            int(optimal_terrain_tile_width / resolution),
        )
    )

    # Split terrain bounding box in pieces
    terrain_grid = tiling.grid(
        xmin,
        ymin,
        xmax,
        ymax,
        optimal_terrain_tile_width,
        optimal_terrain_tile_width,
    )

    # Start dask cluster
    cluster = None
    client = None

    # Use dask

    use_dask = {"local_dask": True, "pbs_dask": True, "mp": False}
    if mode not in use_dask.keys():
        raise NotImplementedError("{} mode is not implemented".format(mode))

    if use_dask[mode]:
        dask_config_used = dask.config.config
        outputs.write_dask_config(
            dask_config_used,
            out_dir,
            output_compute_dsm.COMPUTE_DSM_DASK_CONFIG_TAG,
        )

        if mode == "local_dask":
            cluster, client = start_local_cluster(nb_workers)
        else:
            cluster, client = start_cluster(nb_workers, walltime, out_dir)

        # Add plugin to monitor memory of workers
        plugin = ComputeDSMMemoryLogger(out_dir)
        client.register_worker_plugin(plugin)

        geoid_data_futures = None
        if geoid_data is not None:
            # Broadcast geoid data to all dask workers
            geoid_data_futures = client.scatter(geoid_data, broadcast=True)

    # Retrieve the epsg code which will be used
    # for the triangulation's output points clouds
    # (ecef if filters are activated)
    if cloud_small_components_filter or cloud_statistical_outliers_filter:
        stereo_out_epsg = 4978
    else:
        stereo_out_epsg = epsg

    # Submit all epipolar regions to be processed as delayed tasks, and
    # project terrain grid to epipolar
    for config_id, conf in configurations_data.items():
        # This list will hold the different epipolar tiles to be
        # processed as points cloud
        delayed_point_clouds = []

        if use_dask[mode]:
            # Use Dask delayed
            for region in conf["epipolar_regions"]:
                delayed_point_clouds.append(
                    dask.delayed(wrappers.images_pair_to_3d_points)(
                        conf["configuration"],
                        region,
                        corr_config,
                        disp_min=conf["disp_min"],
                        disp_max=conf["disp_max"],
                        geoid_data=geoid_data_futures,
                        out_epsg=stereo_out_epsg,
                        use_sec_disp=use_sec_disp,
                        snap_to_img1=snap_to_img1,
                        align=align,
                        add_msk_info=write_msk,
                    )
                )

            logging.info(
                "Submitted {} epipolar delayed tasks to dask "
                "for stereo configuration {}".format(
                    len(delayed_point_clouds), config_id
                )
            )
        else:
            # Use multiprocessing module

            # create progress bar with an update callback
            pbar = tqdm(total=len(conf["epipolar_regions"]))

            def update(args):  # pylint: disable=unused-argument
                pbar.update()

            # create a multiprocessing thread pool
            mp_mode = "fork"
            if not check_tbb_installed():
                mp_mode = "forkserver"
                logging.warning(
                    "Numba does not find TBB : "
                    "Multiprocessing forced to forkserver mode. "
                    "User might not get logs from workers."
                )

            pool = mp.get_context(mp_mode).Pool(
                nb_workers
            )  # pylint: disable=consider-using-with

            # launch several 'write_3d_points()' to process each epipolar region
            for region in conf["epipolar_regions"]:
                delayed_point_clouds.append(
                    pool.apply_async(
                        write_3d_points,
                        args=(
                            conf["configuration"],
                            region,
                            corr_config,
                            tmp_dir,
                            config_id,
                        ),
                        kwds={
                            "disp_min": conf["disp_min"],
                            "disp_max": conf["disp_max"],
                            "geoid_data": geoid_data,
                            "out_epsg": stereo_out_epsg,
                            "use_sec_disp": use_sec_disp,
                            "add_msk_info": write_msk,
                            "snap_to_img1": snap_to_img1,
                            "align": align,
                        },
                        callback=update,
                    )
                )

            # Wait computation results (timeout in seconds) and replace the
            # async objects by the actual output of write_3d_points(), meaning
            # the paths to cloud files
            delayed_point_clouds = [
                delayed_pc.get(timeout=per_job_timeout)
                for delayed_pc in delayed_point_clouds
            ]

            # closing thread pool when computation is done
            pool.close()
            pool.join()

        conf["delayed_point_clouds"] = delayed_point_clouds

        # build list of epipolar region hashes
        conf["epipolar_regions_hash"] = [
            tiling.region_hash_string(k) for k in conf["epipolar_regions"]
        ]

        points_min, points_max = tiling.terrain_grid_to_epipolar(
            terrain_grid,
            conf["epipolar_regions_grid"],
            conf["configuration"],
            conf["disp_min"],
            conf["disp_max"],
            epsg,
        )
        conf["epipolar_points_min"] = points_min
        conf["epipolar_points_max"] = points_max

    # Retrieve number of bands
    if in_params.COLOR1_TAG in configuration[in_params.INPUT_SECTION_TAG]:
        nb_bands = inputs.rasterio_get_nb_bands(
            configuration[in_params.INPUT_SECTION_TAG][in_params.COLOR1_TAG]
        )
    else:
        logging.info(
            "No color image has been given in input, "
            "{} will be used as the color image".format(
                configuration[in_params.INPUT_SECTION_TAG][in_params.IMG1_TAG]
            )
        )

        nb_bands = inputs.rasterio_get_nb_bands(
            configuration[in_params.INPUT_SECTION_TAG][in_params.IMG1_TAG]
        )

    logging.info("Number of bands in color image: {}".format(nb_bands))

    # This list will contained the different raster tiles to be written by cars
    delayed_dsm_tiles = []
    number_of_epipolar_tiles_per_terrain_tiles = []

    terrain_regions, corresponding_tiles, rank = tiling.get_corresponding_tiles(
        terrain_grid, configurations_data
    )

    number_of_terrain_splits = len(terrain_regions)

    if not use_dask[mode]:
        # create progress bar with update callback
        pbar = tqdm(
            total=number_of_terrain_splits,
            desc="Finding correspondences between terrain and epipolar tiles",
        )

        def update(args):  # pylint: disable=unused-argument
            pbar.update()

        # initialize a thread pool for multiprocessing mode
        pool = mp.Pool(nb_workers)  # pylint: disable=consider-using-with

    for terrain_region, required_point_clouds in zip(
        terrain_regions, corresponding_tiles
    ):

        # start and size parameters for the rasterization function
        xstart, ystart, xsize, ysize = tiling.roi_to_start_and_size(
            terrain_region, resolution
        )

        # cloud filtering params
        if cloud_small_components_filter:
            small_cpn_filter_params = (
                static_conf.get_small_components_filter_params()
            )
        else:
            small_cpn_filter_params = None

        if cloud_statistical_outliers_filter:
            statistical_filter_params = (
                static_conf.get_statistical_outliers_filter_params()
            )
        else:
            statistical_filter_params = None

        # rasterization grid division factor
        rasterization_params = static_conf.get_rasterization_params()
        grid_points_division_factor = getattr(
            rasterization_params, static_conf.grid_points_division_factor_tag
        )

        if len(required_point_clouds) > 0:
            logging.debug(
                "Number of clouds to process for this terrain tile: {}".format(
                    len(required_point_clouds)
                )
            )

            if use_dask[mode]:
                # Delayed call to rasterization operations using all required
                # point clouds
                rasterized = dask.delayed(rasterization_wrapper)(
                    required_point_clouds,
                    resolution,
                    epsg,
                    xstart=xstart,
                    ystart=ystart,
                    xsize=xsize,
                    ysize=ysize,
                    radius=dsm_radius,
                    sigma=sigma,
                    dsm_no_data=dsm_no_data,
                    color_no_data=color_no_data,
                    msk_no_data=msk_no_data,
                    small_cpn_filter_params=small_cpn_filter_params,
                    statistical_filter_params=statistical_filter_params,
                    grid_points_division_factor=grid_points_division_factor,
                )

                # Keep track of delayed raster tiles
                delayed_dsm_tiles.append(rasterized)

            else:
                # prepare local args and kwds for write_dsm_by_tile()
                local_args = (
                    required_point_clouds,
                    resolution,
                    epsg,
                    tmp_dir,
                    nb_bands,
                    static_conf.get_color_image_encoding(),
                    output_stats,
                    write_msk,
                )
                local_kwds = {
                    "xstart": xstart,
                    "ystart": ystart,
                    "xsize": xsize,
                    "ysize": ysize,
                    "radius": dsm_radius,
                    "sigma": sigma,
                    "dsm_no_data": dsm_no_data,
                    "color_no_data": color_no_data,
                    "small_cpn_filter_params": small_cpn_filter_params,
                    "statistical_filter_params": statistical_filter_params,
                    "grid_points_division_factor": grid_points_division_factor,
                    "msk_no_data": msk_no_data,
                }
                # Launch asynchronous job for write_dsm_by_tile()
                delayed_dsm_tiles.append(
                    pool.apply_async(
                        write_dsm_by_tile,
                        args=local_args,
                        kwds=local_kwds,
                        callback=update,
                    )
                )

            number_of_epipolar_tiles_per_terrain_tiles.append(
                len(required_point_clouds)
            )

    logging.info(
        "Number of epipolar tiles "
        "for each terrain tile (counter): {}".format(
            sorted(Counter(number_of_epipolar_tiles_per_terrain_tiles).items())
        )
    )

    logging.info(
        "Average number of epipolar tiles "
        "for each terrain tile: {}".format(
            int(np.round(np.mean(number_of_epipolar_tiles_per_terrain_tiles)))
        )
    )

    logging.info(
        "Max number of epipolar tiles "
        "for each terrain tile: {}".format(
            np.max(number_of_epipolar_tiles_per_terrain_tiles)
        )
    )

    bounds = (xmin, ymin, xmax, ymax)
    # Derive output image files parameters to pass to rasterio
    xsize, ysize = tiling.roi_to_start_and_size(
        [xmin, ymin, xmax, ymax], resolution
    )[2:]

    out_dsm = os.path.join(out_dir, "dsm.tif")
    out_clr = os.path.join(out_dir, "clr.tif")
    out_msk = None
    if write_msk:
        out_msk = os.path.join(out_dir, "msk.tif")
    out_dsm_mean = os.path.join(out_dir, "dsm_mean.tif")
    out_dsm_std = os.path.join(out_dir, "dsm_std.tif")
    out_dsm_n_pts = os.path.join(out_dir, "dsm_n_pts.tif")
    out_dsm_points_in_cell = os.path.join(out_dir, "dsm_pts_in_cell.tif")

    if use_dask[mode]:
        # Sort tiles according to rank
        delayed_dsm_tiles = [
            delayed
            for _, delayed in sorted(
                zip(rank, delayed_dsm_tiles), key=lambda pair: pair[0]
            )
        ]

        logging.info(
            "Submitting {} tasks to dask".format(len(delayed_dsm_tiles))
        )

        # Transform all delayed raster tiles to futures (computation starts
        # immediatly on workers, assynchronously)
        future_dsm_tiles = client.compute(delayed_dsm_tiles)

        logging.info("DSM output image size: {}x{} pixels".format(xsize, ysize))

        write_dsm.write_geotiff_dsm(
            future_dsm_tiles,
            out_dir,
            xsize,
            ysize,
            bounds,
            resolution,
            epsg,
            nb_bands,
            dsm_no_data,
            color_no_data,
            color_dtype=static_conf.get_color_image_encoding(),
            write_color=True,
            write_stats=output_stats,
            write_msk=write_msk,
            msk_no_data=msk_no_data,
        )

        # stop cluster
        stop_cluster(cluster, client)

    else:
        logging.info("Computing DSM tiles ...")
        # Wait for asynchrone jobs (timeout in seconds) and replace them by
        # write_dsm_by_tile() output
        delayed_dsm_tiles = [
            delayed_tile.get(timeout=per_job_timeout)
            for delayed_tile in delayed_dsm_tiles
        ]

        # closing the tread pool after computation
        pool.close()
        pool.join()

        # vrt to tif
        logging.info("Building VRT")
        vrt_options = gdal.BuildVRTOptions(resampleAlg="nearest")

        def vrt_mosaic(tiles_glob, vrt_name, vrt_options, output_file):
            vrt_file = os.path.join(out_dir, vrt_name)
            tiles_list = glob(os.path.join(out_dir, "tmp", tiles_glob))
            gdal.BuildVRT(vrt_file, tiles_list, options=vrt_options)
            vrt_file_descriptor = gdal.Open(vrt_file)
            vrt_file_descriptor = gdal.Translate(
                output_file, vrt_file_descriptor
            )
            vrt_file_descriptor = None

        vrt_mosaic("*_dsm.tif", "dsm.vrt", vrt_options, out_dsm)
        vrt_mosaic("*_clr.tif", "clr.vrt", vrt_options, out_clr)

        if write_msk:
            vrt_mosaic("*_msk.tif", "msk.vrt", vrt_options, out_msk)

        if output_stats:
            vrt_mosaic(
                "*_dsm_mean.tif", "dsm_mean.vrt", vrt_options, out_dsm_mean
            )
            vrt_mosaic("*_dsm_std.tif", "dsm_std.vrt", vrt_options, out_dsm_std)
            vrt_mosaic(
                "*_dsm_n_pts.tif", "dsm_n_pts.vrt", vrt_options, out_dsm_n_pts
            )
            vrt_mosaic(
                "*_pts_in_cell.tif",
                "dsm_pts_in_cell.vrt",
                vrt_options,
                out_dsm_points_in_cell,
            )

    # Fill output json file
    out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
        output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
    ][output_compute_dsm.EPSG_TAG] = epsg
    out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
        output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
    ][output_compute_dsm.DSM_TAG] = out_dsm
    out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
        output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
    ][output_compute_dsm.DSM_NO_DATA_TAG] = float(dsm_no_data)
    out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
        output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
    ][output_compute_dsm.COLOR_NO_DATA_TAG] = float(color_no_data)
    out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
        output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
    ][output_compute_dsm.COLOR_TAG] = out_clr

    if write_msk:
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.MSK_TAG] = out_msk

    if output_stats:
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.DSM_MEAN_TAG] = out_dsm_mean
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.DSM_STD_TAG] = out_dsm_std
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.DSM_N_PTS_TAG] = out_dsm_n_pts
        out_json[output_compute_dsm.COMPUTE_DSM_SECTION_TAG][
            output_compute_dsm.COMPUTE_DSM_OUTPUT_SECTION_TAG
        ][output_compute_dsm.DSM_POINTS_IN_CELL_TAG] = out_dsm_points_in_cell

    # Write the output json
    out_json_path = os.path.join(out_dir, "content.json")

    try:
        inputs.check_json(
            out_json, output_compute_dsm.COMPUTE_DSM_CONTENT_SCHEMA
        )
    except CheckerError as check_error:
        logging.warning(
            "content.json does not comply with schema: {}".format(check_error)
        )

    output_compute_dsm.write_stereo_content_file(out_json, out_json_path)
