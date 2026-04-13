from fastapi import APIRouter

from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory.decay import summarize_memory_reconciliation_state
from src.memory.providers import list_memory_provider_inventory

router = APIRouter()


@router.get("/memory/providers")
async def list_memory_providers():
    payload = list_memory_provider_inventory()
    reconciliation = await summarize_memory_reconciliation_state()
    payload["canonical_memory_reconciliation"] = reconciliation
    payload["guardian_memory_benchmark"] = await build_guardian_memory_benchmark_report(
        run_suite=False,
        reconciliation=reconciliation,
    )
    return payload
