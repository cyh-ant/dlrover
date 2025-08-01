# Copyright 2025 The DLRover Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dlrover.python.unified.common.args import parse_job_args
from dlrover.python.unified.common.config import JobConfig
from dlrover.python.unified.common.dl_context import RLContext
from dlrover.python.unified.common.job_context import get_job_context
from dlrover.python.unified.tests.base import RayBaseTest
from dlrover.python.unified.tests.test_data import TestData


class BaseMasterTest(RayBaseTest):
    def setUp(self):
        super().setUp()
        args = [
            "--job_name",
            "test",
            "--dl_type",
            "RL",
            "--dl_config",
            f"{TestData.UD_SIMPLE_TEST_RL_CONF_0}",
        ]
        parsed_args = parse_job_args(args)
        job_config = JobConfig.build_from_args(parsed_args)
        rl_context = RLContext.build_from_args(parsed_args)

        self._job_context = get_job_context()
        self._job_context.init(job_config, rl_context)

    def tearDown(self):
        self.close_ray_safely()
        super().tearDown()
