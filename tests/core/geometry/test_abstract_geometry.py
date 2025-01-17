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
Test module for cars.core.geometry
"""
import pytest

from .dummy_abstract_classes import (  # noqa; pylint: disable=unused-import
    NoMethodClass,
)

from cars.core.geometry import (  # noqa;  isort:skip; pylint: disable=wrong-import-order
    AbstractGeometry,
)


@pytest.mark.unit_tests
def test_missing_abstract_methods():
    """
    Test cars geometry abstract class
    """
    with pytest.raises(Exception) as error:
        AbstractGeometry(  # pylint: disable=abstract-class-instantiated
            "NoMethodClass"
        )
    assert (
        str(error.value) == "Can't instantiate abstract class"
        " NoMethodClass with abstract methods "
        "generate_epipolar_grids, triangulate"
    )


@pytest.mark.unit_tests
def test_wrong_class_name():
    """
    Test cars geometry abstract class
    """
    with pytest.raises(KeyError) as error:
        AbstractGeometry("test")  # pylint: disable=abstract-class-instantiated
    assert str(error.value) == "'No geometry plugin named test registered'"
