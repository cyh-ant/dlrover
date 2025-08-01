# Copyright 2022 The DLRover Authors. All rights reserved.
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

import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from dlrover.python.common.constants import (
    DistributionStrategy,
    NodeEventType,
    NodeExitReason,
    NodeStatus,
    NodeType,
    PlatformType,
)
from dlrover.python.common.global_context import Context
from dlrover.python.common.node import Node, NodeGroupResource, NodeResource
from dlrover.python.master.node.job_context import get_job_context
from dlrover.python.master.node.worker import ChiefManager, WorkerManager
from dlrover.python.master.resource.job import JobResource
from dlrover.python.scheduler.factory import new_elastic_job
from dlrover.python.tests.test_utils import mock_k8s_client

_dlrover_ctx = Context.singleton_instance()


class WorkerManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        mock_k8s_client()
        self._job_resource = JobResource()
        self._job_resource.node_group_resources[NodeType.WORKER] = (
            NodeGroupResource(5, NodeResource(16, 2048))
        )
        self._elastic_job = new_elastic_job(
            PlatformType.KUBERNETES, "test", "default"
        )

        self.job_context = get_job_context()
        job_nodes = self._job_resource.init_job_node_meta(
            1,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self.job_context.update_job_nodes(job_nodes)

        self._worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )

    def tearDown(self) -> None:
        self.job_context.clear_job_nodes()

    def test_scale_up_workers(self):
        self._worker_manager._scale_up_workers(3)
        workers = self.job_context.get_mutable_worker_nodes()
        self.assertEqual(len(workers), 8)
        self.assertEqual(workers[7].id, 7)

    def test_scale_down_workers(self):
        workers = list(self.job_context.get_mutable_worker_nodes().values())
        self._worker_manager._scale_down_workers(2, workers)
        released_workers = []
        for worker in workers:
            if worker.is_released:
                released_workers.append(worker)
        self.assertEqual(len(released_workers), 2)

    def test_delete_exited_workers(self):
        workers = self.job_context.get_mutable_worker_nodes()
        workers[3].status = NodeStatus.FINISHED
        self.job_context.update_job_node(workers[3])
        workers[4].status = NodeStatus.FAILED
        self.job_context.update_job_node(workers[4])

        plan = self._worker_manager.delete_exited_workers()
        node_names = [node.name for node in plan.remove_nodes]
        self.assertListEqual(
            node_names,
            ["test-edljob-worker-3", "test-edljob-worker-4"],
        )

    def test_delete_running_workers(self):
        for node in self.job_context.get_mutable_worker_nodes().values():
            node.status = NodeStatus.RUNNING
            self.job_context.update_job_node(node)
        plan = self._worker_manager.delete_running_workers()
        node_names = [node.name for node in plan.remove_nodes]
        self.assertListEqual(
            node_names,
            [
                "test-edljob-worker-0",
                "test-edljob-worker-1",
                "test-edljob-worker-2",
                "test-edljob-worker-3",
                "test-edljob-worker-4",
            ],
        )

    def test_relaunch_node(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        failed_worker = self.job_context.get_mutable_worker_nodes()[4]
        failed_worker.status = NodeStatus.FAILED
        failed_worker.max_relaunch_count = 3
        self.job_context.update_job_node(failed_worker)
        plan = worker_manager.relaunch_node(
            failed_worker, remove_exited_node=True
        )
        self.assertEqual(plan.launch_nodes[0].config_resource.cpu, 16)
        self.assertEqual(self.job_context.get_mutable_worker_nodes()[5].id, 5)
        self.assertEqual(plan.launch_nodes[0].max_relaunch_count, 3)
        self.assertEqual(plan.remove_nodes[0].config_resource.cpu, 16)

    def test_relaunch_chief_node(self):
        tf_master_node = Node(
            NodeType.MASTER,
            node_id=0,
            config_resource=NodeResource(cpu=16, memory=10240),
        )
        job_nodes = {
            NodeType.MASTER: {0: tf_master_node},
        }
        self.job_context.update_job_nodes(job_nodes)
        manager = ChiefManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        plan = manager.relaunch_node(tf_master_node)
        nodes = self.job_context.job_nodes_by_type(NodeType.CHIEF)
        self.assertEqual(plan.launch_nodes[0].config_resource.cpu, 16)
        self.assertEqual(nodes[1].id, 1)

    def test_reduce_pending_node_resource(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        for node in self.job_context.get_mutable_worker_nodes().values():
            node.status = NodeStatus.PENDING
            node.create_time = datetime.now() + timedelta(days=-1)
            self.job_context.update_job_node(node)
        plan = worker_manager.reduce_pending_node_resource()
        self.assertEqual(len(plan.launch_nodes), 5)

        for node in self.job_context.get_mutable_worker_nodes().values():
            node.config_resource.gpu_num = 1
            self.job_context.update_job_node(node)

        plan = worker_manager.reduce_pending_node_resource()
        self.assertTrue(plan.empty())

    def test_pending_without_workers(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        for node in self.job_context.get_mutable_worker_nodes().values():
            node.status = NodeStatus.FAILED
            node.exit_reason = NodeExitReason.FATAL_ERROR
            self.job_context.update_job_node(node)
        exited = worker_manager.has_exited_worker()
        self.assertTrue(exited)

        for node in self.job_context.get_mutable_worker_nodes().values():
            node.exit_reason = NodeExitReason.KILLED
            self.job_context.update_job_node(node)
        exited = worker_manager.has_exited_worker()
        self.assertFalse(exited)

        self.job_context.get_mutable_worker_nodes()[
            0
        ].status = NodeStatus.SUCCEEDED
        self.job_context.update_job_node(
            self.job_context.get_mutable_worker_nodes()[0]
        )
        exited = worker_manager.has_exited_worker()
        self.assertTrue(exited)

        worker = self.job_context.get_mutable_worker_nodes()[0]
        worker.status = NodeStatus.DELETED
        worker.reported_status = (NodeEventType.SUCCEEDED_EXITED, 0)
        self.job_context.update_job_node(worker)
        exited = worker_manager.has_exited_worker()
        self.assertTrue(exited)

        wait = worker_manager.wait_worker_restart()
        self.assertTrue(wait)
        for node in self.job_context.get_mutable_worker_nodes().values():
            node.relaunch_count = node.max_relaunch_count
            self.job_context.update_job_node(node)

        wait = worker_manager.wait_worker_restart()
        self.assertFalse(wait)

    def test_verify_restarting_training(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        reset = worker_manager.verify_restarting_training(0)
        self.assertFalse(reset)
        self.job_context.get_mutable_worker_nodes()[0].restart_training = True
        self.job_context.update_job_node(
            self.job_context.get_mutable_worker_nodes()[0]
        )
        reset = worker_manager.verify_restarting_training(0)
        self.assertTrue(reset)
        self.job_context.get_mutable_worker_nodes()[0].is_released = True
        self.job_context.update_job_node(
            self.job_context.get_mutable_worker_nodes()[0]
        )
        reset = worker_manager.verify_restarting_training(0)
        self.assertFalse(reset)

    def test_all_failure_with_restarting(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self.assertFalse(worker_manager.is_all_workers_node_check_failed())
        self.assertFalse(worker_manager.verify_restarting_training(0))
        for node in self.job_context.get_mutable_worker_nodes().values():
            node.update_reported_status(NodeEventType.NODE_CHECK_FAILED)
        self.assertTrue(worker_manager.is_all_workers_node_check_failed())

        node = self.job_context.get_mutable_worker_nodes()[0]
        plan = worker_manager.relaunch_node(node)
        self.job_context.update_job_node(plan.launch_nodes[0])
        for node in self.job_context.get_mutable_worker_nodes().values():
            print(node)
        self.assertFalse(
            worker_manager.is_all_workers_node_check_failed()
        )  # include relaunched nodes
        self.assertTrue(
            worker_manager.is_all_initial_workers_node_check_failed(
                self._job_resource.worker_num
            )
        )

    def test_is_training_hang_by_pending_workers(self):
        self.job_context.clear_job_nodes()
        _dlrover_ctx.pending_fail_strategy = 2
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                4, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                4, DistributionStrategy.PS
            )
        )

        worker_manager.update_node_required_info((4, 8, 600))
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                4, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                4, DistributionStrategy.PS
            )
        )

        mock_nodes = {}

        # =========================================
        # condition: when node required is updated
        # =========================================

        # mock with 3 running + 1 pending short time
        worker_num = 4
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
                mock_node.create_time = datetime.now() + timedelta(minutes=-1)
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )
        mock_nodes.clear()
        self.job_context.clear_job_nodes()

        # mock with 3 running + 1 pending no time
        worker_num = 4
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )
        mock_nodes.clear()
        self.job_context.clear_job_nodes()

        # mock with 3 running + 1 pending long time
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)

        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )
        mock_nodes.clear()
        self.job_context.clear_job_nodes()

        # mock with 4 running + 1 pending long time
        worker_num = 5
        for index in range(5):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )
        mock_nodes.clear()
        self.job_context.clear_job_nodes()

        # mock with 3 running + 1 initial long time
        worker_num = 4
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.INITIAL
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # =============================================
        # condition: when node required is not updated
        # =============================================
        worker_manager.update_node_required_info((0, 0, 600))

        # mock with 1 pending short time
        worker_num = 1
        for index in range(1):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
                mock_node.create_time = datetime.now() + timedelta(minutes=-10)
            else:
                mock_node.create_time = datetime.now() + timedelta(minutes=-10)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # mock with 1 pending long time
        for index in range(1):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.PENDING,
            )
            mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)

        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # mock with 2 pending long time
        worker_num = 2
        for index in range(2):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.PENDING,
            )
            mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # mock with 2 pending + 1 running long time
        worker_num = 2
        for index in range(2):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.PENDING,
            )
            if index == 0:
                mock_node.status = NodeStatus.RUNNING
                mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # mock timeout=0 with 2 pending long time
        worker_manager._get_pending_timeout = mock.MagicMock(return_value=0)
        for index in range(2):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.PENDING,
            )
            mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # with strategy 1 + 1 pending
        worker_manager._get_pending_timeout = mock.MagicMock(return_value=5)
        worker_manager.update_node_required_info((2, 4, 1))
        _dlrover_ctx.pending_fail_strategy = 1
        worker_num = 4
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
                rank_index=index,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
            mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertTrue(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

        # with strategy 1 + all running
        worker_manager._get_pending_timeout = mock.MagicMock(return_value=5)
        worker_manager.update_node_required_info((2, 4, 1))
        _dlrover_ctx.pending_fail_strategy = 1
        worker_num = 4
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
                rank_index=index,
            )
            mock_node.create_time = datetime.now() + timedelta(minutes=-20)
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.ALLREDUCE
            )
        )
        self.assertFalse(
            worker_manager.find_pending_node_caused_training_hang(
                worker_num, DistributionStrategy.PS
            )
        )

    def test_is_training_hang_by_insufficient_worker(self):
        self.job_context.clear_job_nodes()
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )
        self.assertFalse(
            worker_manager.is_training_hang_by_insufficient_worker()
        )

        # set the timeout interval 1s
        worker_manager.update_node_required_info((4, 8, 1))
        self.assertFalse(
            worker_manager.is_training_hang_by_insufficient_worker()
        )

        mock_nodes = {}
        is_insufficient = 0
        worker_manager._get_insufficient_timeout = mock.MagicMock(
            return_value=1
        )

        # mock with 2 succeeded
        for index in range(2):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.SUCCEEDED,
            )
            self.job_context.update_job_node(mock_node)
            mock_nodes[index] = mock_node
        for _ in range(3):
            self.assertFalse(
                worker_manager.is_training_hang_by_insufficient_worker()
            )
            time.sleep(0.1)

        # mock with 3 running + 1 pending
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.PENDING
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        for _ in range(5):
            if worker_manager.is_training_hang_by_insufficient_worker():
                is_insufficient += 1
            time.sleep(0.5)
        self.assertEqual(is_insufficient, 0)
        mock_nodes.clear()
        is_insufficient = 0
        self.job_context.clear_job_nodes()

        # mock with 3 running
        for index in range(3):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            mock_nodes[index] = mock_node
            self.job_context.update_job_node(mock_node)
        for _ in range(5):
            if worker_manager.is_training_hang_by_insufficient_worker():
                is_insufficient += 1
            time.sleep(0.5)
        self.assertTrue(is_insufficient >= 2)
        mock_nodes.clear()
        is_insufficient = 0
        self.job_context.clear_job_nodes()

        # mock with 3 running + 1 released
        for index in range(4):
            mock_node = Node(
                NodeType.WORKER,
                index,
                NodeResource(0, 0),
                "test-" + str(index),
                NodeStatus.RUNNING,
            )
            if index == 0:
                mock_node.status = NodeStatus.DELETED
                mock_node.is_released = True
            self.job_context.update_job_node(mock_node)
            mock_nodes[index] = mock_node
        for _ in range(5):
            if worker_manager.is_training_hang_by_insufficient_worker():
                is_insufficient += 1
            time.sleep(0.5)
        self.assertTrue(is_insufficient >= 2)

    def test_is_all_workers_succeeded_exited(self):
        worker_manager = WorkerManager(
            self._job_resource,
            3,
            self._elastic_job.get_node_service_addr,
            self._elastic_job.get_node_name,
        )

        # all succeeded exited
        for _, node in self.job_context.job_nodes()[NodeType.WORKER].items():
            node.reported_status = (NodeEventType.SUCCEEDED_EXITED, 1)
        self.assertTrue(worker_manager.is_all_workers_succeeded_exited())

        # some succeeded exited
        for node_id, node in self.job_context.job_nodes()[
            NodeType.WORKER
        ].items():
            if node_id < 2:
                node.reported_status = (NodeEventType.FAILED_EXITED, 1)
        self.assertFalse(worker_manager.is_all_workers_succeeded_exited())
