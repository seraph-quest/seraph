export function githubReadbackRequest(acknowledged: boolean, revision: unknown, remoteId?: number) {
  if (acknowledged !== true || typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 1 || (remoteId !== undefined && (!Number.isSafeInteger(remoteId) || remoteId < 1))) {
    throw new Error("Explicit current-revision GitHub readback acknowledgment required");
  }
  return { acknowledged_readback: true, expected_connection_revision: revision, ...(remoteId === undefined ? {} : { remote_id: remoteId }) };
}
