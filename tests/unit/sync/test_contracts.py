"""Host export policy is also enforced at the cloud wire boundary."""

import hashlib

import pytest
from pydantic import ValidationError

from octop.infra.sync.contracts import FileData, MemoryData, Mutation


@pytest.mark.parametrize(
    "path",
    [
        "/inbound/a",
        "inbound/../secret",
        "inbound//a",
        "inbound/./a",
        "inbound\\a",
        "inbound/.env",
        "artifacts/.octop/key",
        "artifacts/checkpoint.sqlite",
        "artifacts/key.pem",
        "artifacts/cache/a",
        "artifacts/profile/a",
        "outside/file",
        "artifacts/CON.txt",
        "artifacts/a:stream",
        "artifacts/trailing.",
    ],
)
def test_export_policy_rejects_unsafe_or_system_paths(path):
    with pytest.raises(ValidationError):
        FileData(path=path, size=0, sha256=hashlib.sha256(b"").hexdigest())


@pytest.mark.parametrize(
    "table", ["checkpoints", "checkpoint_blobs", "meta", "sync_rows", "other_namespace_atoms"]
)
def test_memory_contract_excludes_operational_tables(table):
    with pytest.raises(ValidationError):
        MemoryData(table=table, key=["record"], record={})


def test_client_cannot_supply_an_authoritative_identity_or_binding():
    with pytest.raises(ValidationError):
        Mutation.model_validate(
            {
                "operation_id": "operation-one",
                "item_id": "thread-one",
                "agent_id": "agent-one",
                "resource_id": "resource-one",
                "kind": "thread",
                "expected_revision": 0,
                "data": {"kind": "thread"},
                "user_id": 42,
                "generation": 10,
            }
        )
