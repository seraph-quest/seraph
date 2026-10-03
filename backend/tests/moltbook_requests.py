"""Build an explicit phase-bound request from the actual owner snapshot."""
import uuid


async def execution_body(client, job_id):
    response = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")
    if response.status_code != 200:
        # Still hit the actual POST auth route with a closed valid body. This
        # never invents operator authority or a canonical job row.
        return {"request_key": str(uuid.uuid4()), "expected_phase": "unattempted", "fencing_token": 0}
    job = response.json()
    value = next((item["payload"] for item in job["checkpoints"] if item["checkpoint_id"] == "moltbook:state"), {})
    if job["status"] == "succeeded":
        receipt = value["executions"][-1]
        return {"request_key": receipt["request_key"], "expected_phase": receipt["phase"], "fencing_token": receipt["fencing_token"]}
    phase = value.get("phase", "unattempted")
    if phase not in {"unattempted", "awaiting_create_approval", "awaiting_verify_approval"}:
        receipt = value.get("executions", [{"phase": "unattempted", "fencing_token": job["lease"]["fencing_token"]}])[-1]
        phase, fence = receipt["phase"], receipt["fencing_token"]
    else:
        fence = job["lease"]["fencing_token"]
    return {"request_key": str(uuid.uuid4()), "expected_phase": phase, "fencing_token": fence}


async def execute(client, job_id):
    return await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json=await execution_body(client, job_id))
