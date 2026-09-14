"""TensorKV software replica of the paper's semantic in-network KV cache."""

from .appliance import ApplianceConfig, TensorKVAppliance
from .baselines import run_baseline_suite
from .constants import (
    BLOCK_SIZE_BYTES,
    BYTES_PER_TOKEN_LLAMA70B_INT4,
    TOKENS_PER_BLOCK,
)
from .engine import PagedEngine
from .libtkv import TensorKVContext, open_device

__all__ = [
    "ApplianceConfig",
    "TensorKVAppliance",
    "TensorKVContext",
    "PagedEngine",
    "open_device",
    "run_baseline_suite",
    "BLOCK_SIZE_BYTES",
    "BYTES_PER_TOKEN_LLAMA70B_INT4",
    "TOKENS_PER_BLOCK",
]

__version__ = "0.1.0"
