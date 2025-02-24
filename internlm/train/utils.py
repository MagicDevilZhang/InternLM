from typing import Dict, Tuple

import torch

from internlm.core.context.parallel_context import global_context as gpc
from internlm.model.utils import is_gate_param, is_moe_param, is_norm_param


def split_params_into_different_groups_for_optimizer(param_groups: Tuple[Dict]) -> Tuple[Dict]:
    """Split parameters into different groups for optimizer

    Args:
        param_groups (Tuple[Dict]): The list of parameter groups to split
        Input Example:
        >>> (
        >>>     {'name': 'default', 'params': [tensor], 'weight_decay' :xxx},
        >>> )

    Returns:
        Tuple[Dict]: list of params groups for optimizer
        Output Example:
        >>> (
        >>>     {'name': 'default','params': [tensor],'weight_decay' :xxx},
        >>>     {'name': 'fp32', 'params': [tensor],'weight_decay' :xxx},
        >>>     {'name': 'norm', 'norm': True, 'params': [tensor],'weight_decay' :xxx},
        >>>     {'name': 'gate', 'gate': True, 'params': [tensor],'weight_decay' :xxx},
        >>>     {'name': 'moe_ep_size_4', 'moe': True, 'params':  [tensor],'weight_decay' :xxx},
        >>> )
    """

    if isinstance(param_groups, tuple):
        param_groups = list(param_groups)  # Tuple cannot be modified
    elif isinstance(param_groups, dict):
        param_groups = [param_groups]
    elif not isinstance(param_groups, list):
        raise ValueError(f"Unknown param group type of {type(param_groups)}")

    # create new groups for fp32, norm, moe gate and moe expert
    new_groups = {}
    new_groups["fp32"] = {"name": "fp32", "params": []}
    if gpc.config.model.get("num_experts", 0) > 1:
        # norm and gate are special group to force sync (when enable MoE).
        for key in ["gate", "norm"]:
            new_groups[key] = {"name": key, key: True, "params": []}
        for key in gpc.expert_parallel_group_names:
            new_groups[key] = {"name": key, "moe": True, "params": []}

    for pgroup in param_groups:
        # copy attribute from origin group, we assume the input param_groups only
        # have one group, so the attribute will not be copyed multiple times.
        for ori_key in pgroup.keys():
            if ori_key not in ("name", "params"):
                for _, group in new_groups.items():
                    group[ori_key] = pgroup[ori_key]
        # assign param
        origin_params = []
        # first split the norm and gate groups, which are special case to force sync (when enable MoE),
        # then fp32 group and the moe group.
        for param in pgroup["params"]:
            if gpc.config.model.get("num_experts", 0) > 1 and is_norm_param(param):
                new_groups["norm"]["params"].append(param)
            # gate param means MoE is enabled
            elif is_gate_param(param):
                new_groups["gate"]["params"].append(param)
            elif param.dtype == torch.float32:
                new_groups["fp32"]["params"].append(param)
            # moe param means MoE is enabled
            elif is_moe_param(param):
                new_groups[param.group_name]["params"].append(param)
            else:
                origin_params.append(param)

        # bf16 param group, which is the first group in the param groups
        pgroup["params"] = origin_params

    for _, g in new_groups.items():
        if g["params"]:
            param_groups.append(g)

    return tuple(param_groups)


def create_param_groups(model, weight_decay):
    parameters = {"params": list(model.parameters()), "name": "default", "weight_decay": weight_decay}
    return split_params_into_different_groups_for_optimizer(parameters)
