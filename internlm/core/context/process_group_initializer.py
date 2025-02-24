#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# adopted from https://github.com/hpcaitech/ColossalAI/blob/main/colossalai/context

import math
from abc import ABC, abstractmethod
from enum import Enum

import torch.distributed as dist

from internlm.utils.timeout import LLM_NCCL_TIMEOUT


# parallel modes
class ParallelMode(Enum):
    """This is an enumeration class containing all possible parallel modes."""

    GLOBAL = "global"

    # common parallel
    DATA = "data"

    # model parallel - containing tensor and pipeline parallel groups
    # this is added to facilitate amp and grad clipping in hybrid parallel
    MODEL = "model"

    # pipeline parallel
    PIPELINE = "pipe"

    # containing all ranks in tensor parallel
    TENSOR = "tensor"

    # zero1 parallel
    ZERO1 = "zero1"

    # runntime network test
    NETTEST = "nettest"

    # expert parallel
    EXPERT = "expert"

    # expert data parallel
    EXPERT_DATA = "expert_data"

    # dummy mode, only used during mode construction
    DUMMY = "dummy"


class ProcessGroupInitializer(ABC):
    """An object, knowing the parallelism configuration, that initializes parallel groups.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(
        self,
        rank: int,
        world_size: int,
        data_parallel_size: int,
        pipeline_parallel_size: int,
        tensor_parallel_size: int,
        zero1_parallel_size: int,
        nettest_parallel_size: int,
        expert_parallel_size: int,
    ):
        self.rank = rank
        self.world_size = world_size
        self.data_parallel_size = data_parallel_size
        self.pipeline_parallel_size = pipeline_parallel_size
        self.tensor_parallel_size = tensor_parallel_size
        self.zero1_parallel_size = zero1_parallel_size
        self.nettest_parallel_size = nettest_parallel_size
        self.expert_parallel_size = expert_parallel_size
        super().__init__()

    @abstractmethod
    def init_dist_group(self, use_cpu: bool = False):
        pass


class Initializer_Data(ProcessGroupInitializer):
    """A ProcessGroupInitializer for data parallelism.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rank_num_per_dp_group = self.world_size // self.data_parallel_size

        assert self.world_size % self.data_parallel_size == 0

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize data parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A Data parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.DATA

        for i in range(self.rank_num_per_dp_group):
            ranks = [i + j * self.rank_num_per_dp_group for j in range(self.data_parallel_size)]
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None

            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Model(ProcessGroupInitializer):
    """A ProcessGroupInitializer for model parallelism (model parallel group contains pipeline and tensor parallel
    groups).

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rank_num_per_group = self.tensor_parallel_size * self.pipeline_parallel_size
        self.num_group = self.world_size // self.rank_num_per_group

        assert self.world_size % self.rank_num_per_group == 0

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize model parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A Model parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.MODEL

        for i in range(self.num_group):
            ranks = [i * self.rank_num_per_group + j for j in range(self.rank_num_per_group)]
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None

            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Pipeline(ProcessGroupInitializer):
    """A ProcessGroupInitializer for pipeline parallelism.

    Args:
        rank (int): The rank of current process
        world_size (int): Size of whole communication world
        data_parallel_size (int): Size of data parallel
        pipeline_parallel_size (int): Size of pipeline parallel
        tensor_parallel_size (int): Size of tensor parallel
        zero1_parallel_size (int): Size of zero1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rank_num_per_dp_group = self.world_size // self.data_parallel_size
        self.pipeline_stage_size = self.rank_num_per_dp_group // self.pipeline_parallel_size

        assert self.world_size % self.data_parallel_size == 0
        assert self.rank_num_per_dp_group % self.pipeline_parallel_size == 0

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize pipeline parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            List[Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode)]:
                A Pipeline parallelism's information in list of tuples.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.PIPELINE

        for i in range(self.data_parallel_size):
            for j in range(self.pipeline_stage_size):
                ranks = list(
                    range(
                        i * self.rank_num_per_dp_group + j,
                        (i + 1) * self.rank_num_per_dp_group,
                        self.pipeline_stage_size,
                    )
                )
                pipe_group_size = len(ranks)
                pipe_group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
                if use_cpu:
                    group_cpu = (
                        dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                        if dist.get_backend() != "gloo"
                        else pipe_group
                    )
                else:
                    group_cpu = None

                if self.rank in ranks:
                    local_rank = ranks.index(self.rank)
                    group_world_size = pipe_group_size
                    process_group = pipe_group
                    cpu_group = group_cpu
                    ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Tensor(ProcessGroupInitializer):
    """A ProcessGroupInitializer for tensor parallelism.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_tensor_parallel_group = self.world_size // self.tensor_parallel_size

        assert self.world_size % self.tensor_parallel_size == 0

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize tensor parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A Tensor parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.TENSOR

        for i in range(self.num_tensor_parallel_group):
            ranks = [i * self.tensor_parallel_size + j for j in range(self.tensor_parallel_size)]
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None

            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Zero1(ProcessGroupInitializer):
    """A ProcessGroupInitializer for zero-1 parallelism.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero-1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rank_num_per_dp_group = self.world_size // self.data_parallel_size
        self.num_zero1_parallel_group = self.data_parallel_size // self.zero1_parallel_size

        assert self.world_size % self.data_parallel_size == 0
        assert self.world_size % self.zero1_parallel_size == 0

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize zero1 parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A zero1 parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.ZERO1

        for i in range(self.rank_num_per_dp_group):
            for j in range(self.num_zero1_parallel_group):
                ranks = [
                    i + (j * self.zero1_parallel_size + k) * self.rank_num_per_dp_group
                    for k in range(self.zero1_parallel_size)
                ]
                group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
                if use_cpu:
                    group_cpu = (
                        dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                        if dist.get_backend() != "gloo"
                        else group
                    )
                else:
                    group_cpu = None

                if self.rank in ranks:
                    local_rank = ranks.index(self.rank)
                    group_world_size = len(ranks)
                    process_group = group
                    cpu_group = group_cpu
                    ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Nettest(ProcessGroupInitializer):
    """A ProcessGroupInitializer for network test, especailly for NCCL.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        nettest_parallel_size (int): Size of a network test group.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_nettest_group = math.ceil(self.world_size / self.nettest_parallel_size)

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize tensor parallel groups, and assign local_ranks and groups to each gpu.

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A Tensor parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.NETTEST

        for i in range(self.num_nettest_group):
            ranks = []
            for j in range(self.nettest_parallel_size):
                rank = i * self.nettest_parallel_size + j
                if rank < self.world_size:
                    ranks.append(rank)
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None

            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Expert(ProcessGroupInitializer):
    """A ProcessGroupInitializer for expert parallelism.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero-1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_expert_parallel_group = self.world_size // self.expert_parallel_size

        assert self.world_size % self.num_expert_parallel_group == 0

        # TODO: to match expert parallel with differnt data parallel size
        assert self.data_parallel_size == self.expert_parallel_size

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize expert parallel groups, and assign local_ranks and groups to each gpu.

        Example: world_size = 8, model_parallel_size = 2, expert_parallel_size = 4
            model_parallel_group = [0,1], [2,3], [4,5], [6,7]
            expert_parallel_group = [0,2,4,6], [1,3,5,7]

        Returns:
            Tuple (local_rank, group_world_size, process_group, ranks_in_group, mode):
                A expert parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        mode = ParallelMode.EXPERT

        for i in range(self.num_expert_parallel_group):
            ranks = list(range(i, self.world_size, self.num_expert_parallel_group))
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None
            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks

        return local_rank, group_world_size, process_group, cpu_group, ranks_in_group, mode


class Initializer_Expert_Data(ProcessGroupInitializer):
    """A ProcessGroupInitializer for expert data parallelism.

    Args:
        rank (int): The rank of current process.
        world_size (int): Size of whole communication world.
        data_parallel_size (int): Size of data parallel.
        pipeline_parallel_size (int): Size of pipeline parallel.
        tensor_parallel_size (int): Size of tensor parallel.
        zero1_parallel_size (int): Size of zero-1 parallel.
        expert_parallel_size (int): Size of expert parallel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_expert_parallel_group = self.world_size // self.expert_parallel_size

    def _get_expert_parallel_ranks(self):
        """
        Create expert and data parallel groups
        Example: world_size = 8, model_parallel_size = 2, expert_parallel_size = 2
        model_parallel_group = [0,1], [2,3], [4,5], [6,7]
        data_parallel_group = [0,2,4,6],                [1,3,5,7]
        expert_parallel_group = [0,2], [4,6],           [1,3], [5,7]
        expert_data_parallel_group = [0,4], [2,6],      [1,5], [3,7]
        """
        data_parallel_groups = []
        model_parallel_size = self.pipeline_parallel_size * self.tensor_parallel_size
        for i in range(model_parallel_size):
            data_parallel_groups.append(list(range(i, self.world_size, model_parallel_size)))

        expert_parallel_groups = []
        expert_data_parallel_groups = []
        for dp_ranks in data_parallel_groups:
            # partition of expert parallel group, e.g. [0,2], [4,6]
            part_ep_group = []
            for i in range(0, self.data_parallel_size, self.expert_parallel_size):
                part_ep_group.append(dp_ranks[i : i + self.expert_parallel_size])
            expert_parallel_groups.extend(part_ep_group)

            for expert_dp_ranks in zip(*part_ep_group):
                expert_data_parallel_groups.append(list(expert_dp_ranks))

        return expert_parallel_groups, expert_data_parallel_groups

    def init_dist_group(self, use_cpu: bool = False):
        """Initialize expert parallel  and expert data groups, and assign local_ranks and groups to each gpu.

        Returns:
            list: [(local_rank, group_world_size, process_group, ranks_in_group, mode), ...]:
                A length 2 list consists of expert parallelism's and expert data parallelism's information tuple.
        """
        local_rank = None
        ranks_in_group = None
        process_group = None
        cpu_group = None
        group_world_size = None
        expert_parallel_groups, expert_data_parallel_groups = self._get_expert_parallel_ranks()

        groups = []
        for ranks in expert_parallel_groups:
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None
            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks
                groups.append(
                    (local_rank, group_world_size, process_group, cpu_group, ranks_in_group, ParallelMode.EXPERT)
                )

        for ranks in expert_data_parallel_groups:
            group = dist.new_group(ranks, timeout=LLM_NCCL_TIMEOUT)
            if use_cpu:
                group_cpu = (
                    dist.new_group(ranks, backend="gloo", timeout=LLM_NCCL_TIMEOUT)
                    if dist.get_backend() != "gloo"
                    else group
                )
            else:
                group_cpu = None
            if self.rank in ranks:
                local_rank = ranks.index(self.rank)
                group_world_size = len(ranks)
                process_group = group
                cpu_group = group_cpu
                ranks_in_group = ranks
                groups.append(
                    (local_rank, group_world_size, process_group, cpu_group, ranks_in_group, ParallelMode.EXPERT_DATA)
                )

        return groups
