"""Bounded admission for remote inference transport.

The original broker was introduced for the one-GPU deployment.  The active
OpenRouter phase keeps its identity and compatibility imports so existing
receipts/tests remain readable, but the runtime resource is now a single
bounded remote-inference lane.  This module is the canonical import surface
for new callers; it does not inspect CUDA, start a model server, or contact a
local VLM.
"""

from __future__ import annotations

from .gpu_admission import (
    GPU_ADMISSION_SCHEMA_VERSION,
    GPU_ADMISSION_STATUSES,
    GpuAdmissionBroker,
    GpuAdmissionCapacityError,
    GpuAdmissionCancelledError,
    GpuAdmissionError,
    GpuAdmissionExpiredError,
    GpuAdmissionIdentityError,
    GpuAdmissionLease,
    GpuAdmissionLeaseError,
    GpuAdmissionReceipt,
    GpuAdmissionRequest,
    GpuAdmissionUncertainError,
    GpuPriority,
    gpu_admission_broker,
    priority_for_inference_context,
)


REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION = "seraph.remote-inference-admission.v1"
REMOTE_INFERENCE_RESOURCE_CLASS = "remote_inference"

RemoteInferenceAdmissionBroker = GpuAdmissionBroker
RemoteInferenceAdmissionCapacityError = GpuAdmissionCapacityError
RemoteInferenceAdmissionCancelledError = GpuAdmissionCancelledError
RemoteInferenceAdmissionError = GpuAdmissionError
RemoteInferenceAdmissionExpiredError = GpuAdmissionExpiredError
RemoteInferenceAdmissionIdentityError = GpuAdmissionIdentityError
RemoteInferenceAdmissionLease = GpuAdmissionLease
RemoteInferenceAdmissionLeaseError = GpuAdmissionLeaseError
RemoteInferenceAdmissionReceipt = GpuAdmissionReceipt
RemoteInferenceAdmissionRequest = GpuAdmissionRequest
RemoteInferenceAdmissionUncertainError = GpuAdmissionUncertainError
RemoteInferencePriority = GpuPriority
remote_inference_admission_broker = gpu_admission_broker


__all__ = [
    "REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION",
    "REMOTE_INFERENCE_RESOURCE_CLASS",
    "GPU_ADMISSION_SCHEMA_VERSION",
    "GPU_ADMISSION_STATUSES",
    "RemoteInferenceAdmissionBroker",
    "RemoteInferenceAdmissionCapacityError",
    "RemoteInferenceAdmissionCancelledError",
    "RemoteInferenceAdmissionError",
    "RemoteInferenceAdmissionExpiredError",
    "RemoteInferenceAdmissionIdentityError",
    "RemoteInferenceAdmissionLease",
    "RemoteInferenceAdmissionLeaseError",
    "RemoteInferenceAdmissionReceipt",
    "RemoteInferenceAdmissionRequest",
    "RemoteInferenceAdmissionUncertainError",
    "RemoteInferencePriority",
    "remote_inference_admission_broker",
    "priority_for_inference_context",
]
