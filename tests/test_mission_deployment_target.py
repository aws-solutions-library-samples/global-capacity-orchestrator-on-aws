"""Mission's DynamoDB state and memory reach the deployment's project and global Region.

The ``<project>-missions`` state table, the mission-memory table and their
SSM name parameters are created by the global stack, so they carry cdk.json's
``project_name`` and live in ``deployment_regions.global``. Mission used to
build ``/gco/...`` parameter names from ``GCO_PROJECT_NAME`` (default ``gco``)
and look tables up in the caller's default Region. moto stands in for an
``acme`` deployment whose global stack is in us-east-2, while the operator's
default Region is us-west-2.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

from mission import deployment, state
from mission.memory import MissionMemoryStore
from mission.state import DynamoDBBackend

from cli.main import cli

ROOT = Path(__file__).resolve().parent.parent

_ACME_CDK = {
    "app": "python3 app.py",
    "context": {
        "project_name": "acme",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-west-2"],
        },
    },
}


@pytest.fixture(autouse=True)
def _restore_mission_targets() -> Iterator[None]:
    saved = (
        deployment._configured_project_name,
        deployment._configured_global_region,
        deployment._configured_project_root,
    )
    saved_backend = state._BACKEND_INSTANCE
    yield
    (
        deployment._configured_project_name,
        deployment._configured_global_region,
        deployment._configured_project_root,
    ) = saved
    state._BACKEND_INSTANCE = saved_backend


@pytest.fixture
def acme_root(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "cdk.json").write_text(json.dumps(_ACME_CDK), encoding="utf-8")
    return root


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fake credentials, a us-west-2 default Region, no Mission overrides."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-west-2",
        "AWS_REGION": "us-west-2",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("GCO_PROJECT_NAME", "GCO_GLOBAL_REGION", "DYNAMODB_REGION", "GLOBAL_REGION"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        yield


def _acme_global_stack(*, memory: bool = False) -> None:
    """Create what the acme global stack creates in us-east-2."""
    ssm = boto3.client("ssm", region_name="us-east-2")
    ddb = boto3.client("dynamodb", region_name="us-east-2")
    tables = {"missions-table-name": "acme-missions"}
    if memory:
        tables["mission-memory-table-name"] = "acme-mission-memory"
    for suffix, table in tables.items():
        ssm.put_parameter(Name=f"/acme/{suffix}", Value=table, Type="String")
        ddb.create_table(
            TableName=table,
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


# ---------------------------------------------------------------------------
# State backend
# ---------------------------------------------------------------------------


def test_state_backend_reads_the_projects_table_in_the_global_region(
    aws: None, acme_root: Path
) -> None:
    _acme_global_stack()
    deployment.configure(project_root=acme_root)
    backend = DynamoDBBackend()
    assert backend.load_session("mission-0001") is None
    assert backend.list_sessions() == []
    table = backend._get_table()
    assert table.name == "acme-missions"
    assert table.meta.client.meta.region_name == "us-east-2"
    assert backend._get_table() is table  # cached


def test_without_the_deployment_the_backend_looks_for_gco_in_the_default_region(
    aws: None, tmp_path: Path
) -> None:
    """Control: no cdk.json and no overrides is the old ``/gco`` lookup."""
    _acme_global_stack()
    deployment.configure(project_root=tmp_path)
    with pytest.raises(Exception, match="ParameterNotFound"):
        DynamoDBBackend().load_session("mission-0001")


def test_explicit_project_and_region_win(aws: None, tmp_path: Path) -> None:
    _acme_global_stack()
    deployment.configure(project_root=tmp_path, project_name="other", global_region="eu-west-1")
    backend = DynamoDBBackend(project_name="acme", region="us-east-2")
    assert backend.load_session("mission-0001") is None


def test_a_given_table_name_skips_ssm_but_keeps_the_global_region(
    aws: None, acme_root: Path
) -> None:
    _acme_global_stack()
    boto3.client("ssm", region_name="us-east-2").delete_parameter(Name="/acme/missions-table-name")
    deployment.configure(project_root=acme_root)
    backend = DynamoDBBackend(table_name="acme-missions")
    assert backend.load_session("mission-0001") is None
    assert backend._resolve_table_name() == "acme-missions"


def test_get_backend_builds_a_dynamodb_backend_for_the_deployment(
    aws: None, acme_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _acme_global_stack()
    deployment.configure(project_root=acme_root)
    monkeypatch.setenv("GCO_MISSION_STATE_BACKEND", "dynamodb")
    state._BACKEND_INSTANCE = None
    backend = state.get_backend()
    assert isinstance(backend, DynamoDBBackend)
    assert backend.load_session("mission-0001") is None


# ---------------------------------------------------------------------------
# Mission memory
# ---------------------------------------------------------------------------


def test_memory_store_resolves_the_projects_parameters_in_the_global_region(
    aws: None, acme_root: Path
) -> None:
    _acme_global_stack(memory=True)
    deployment.configure(project_root=acme_root)
    store = MissionMemoryStore()
    assert store._resolve_table_name() == "acme-mission-memory"
    assert store._get_client().meta.region_name == "us-east-2"
    assert store.list_memories(limit=5) == []


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DYNAMODB_REGION": "ap-south-1"}, "ap-south-1"),
        ({"GLOBAL_REGION": "ca-central-1"}, "ca-central-1"),
        ({"GCO_GLOBAL_REGION": "eu-west-3"}, "eu-west-3"),
        ({}, "us-east-2"),
    ],
    ids=["dynamodb-region", "global-region", "gco-global-region", "cdk-json"],
)
def test_memory_region_order(
    aws: None,
    acme_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    expected: str,
) -> None:
    deployment.configure(project_root=acme_root)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert MissionMemoryStore()._get_client().meta.region_name == expected


def test_memory_falls_back_to_aws_region_without_a_deployment(aws: None, tmp_path: Path) -> None:
    deployment.configure(project_root=tmp_path)
    assert MissionMemoryStore()._get_client().meta.region_name == "us-west-2"


def test_memory_project_override(aws: None, acme_root: Path) -> None:
    deployment.configure(project_root=acme_root)
    store = MissionMemoryStore(project_name="beta", region="us-east-2")
    with pytest.raises(Exception, match="/beta/mission-memory-table-name"):
        store._resolve_table_name()


# ---------------------------------------------------------------------------
# The CLI passes its configuration; the MCP server passes its project root
# ---------------------------------------------------------------------------


def _invoke(args: list[str], cwd: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(cwd))  # no ~/.gco/config.yaml of the host
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    monkeypatch.setenv("GCO_ENABLE_SWARM", "true")
    return CliRunner().invoke(cli, args)


def test_gco_mission_memory_list_uses_the_cli_project_and_global_region(
    aws: None, acme_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _acme_global_stack(memory=True)
    deployment.configure(project_root=tmp_path)  # Mission's own fallback would miss acme
    result = _invoke(["mission", "memory", "list"], acme_root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"memories": []}
    assert deployment._configured_project_name == "acme"
    assert deployment._configured_global_region == "us-east-2"


def test_gco_swarm_list_uses_the_cli_project_and_global_region(
    aws: None, acme_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _acme_global_stack()
    deployment.configure(project_root=tmp_path)
    monkeypatch.setenv("GCO_MISSION_STATE_BACKEND", "dynamodb")
    state._BACKEND_INSTANCE = None
    result = _invoke(["swarm", "list"], acme_root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"swarms": []}


def test_cli_environment_overrides_reach_mission(
    aws: None, acme_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GCO_PROJECT_NAME", "beta")
    monkeypatch.setenv("GCO_GLOBAL_REGION", "eu-west-1")
    result = _invoke(["mission", "memory", "list"], acme_root, monkeypatch)
    assert result.exit_code == 1  # beta has no memory table in eu-west-1
    assert "/beta/mission-memory-table-name" in result.output
    assert deployment._configured_global_region == "eu-west-1"


def test_the_mcp_server_reads_cdk_json_from_its_project_root() -> None:
    probe = (
        "import sys; sys.path.insert(0, 'gco_mcp'); import run_mcp, cli_runner; "
        "from mission import deployment; "
        "print(deployment._configured_project_root == cli_runner.PROJECT_ROOT)"
    )
    child = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert child.stdout.strip().splitlines()[-1] == "True", child.stderr[-2000:]


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


def test_resolution_order(acme_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCO_PROJECT_NAME", raising=False)
    monkeypatch.delenv("GCO_GLOBAL_REGION", raising=False)
    deployment.configure(project_root=acme_root)
    assert (deployment.project_name(), deployment.global_region()) == ("acme", "us-east-2")
    monkeypatch.setenv("GCO_PROJECT_NAME", "env-project")
    monkeypatch.setenv("GCO_GLOBAL_REGION", "eu-west-1")
    assert (deployment.project_name(), deployment.global_region()) == ("env-project", "eu-west-1")
    deployment.configure(
        project_name="cli-project", global_region="eu-north-1", project_root=acme_root
    )
    assert (deployment.project_name(), deployment.global_region()) == ("cli-project", "eu-north-1")
    assert deployment.project_name("arg") == "arg"
    assert deployment.global_region("ap-east-1") == "ap-east-1"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps(["a", "list"]),
        json.dumps({"context": "not a mapping"}),
        json.dumps({"context": {"project_name": "", "deployment_regions": ["us-east-2"]}}),
        json.dumps({"context": {"project_name": 7, "deployment_regions": {"global": 3}}}),
    ],
    ids=["malformed", "not-an-object", "context-not-a-mapping", "empty-values", "wrong-types"],
)
def test_unusable_cdk_json_falls_back_to_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    monkeypatch.delenv("GCO_PROJECT_NAME", raising=False)
    monkeypatch.delenv("GCO_GLOBAL_REGION", raising=False)
    (tmp_path / "cdk.json").write_text(content, encoding="utf-8")
    deployment.configure(project_root=tmp_path)
    assert (deployment.project_name(), deployment.global_region()) == ("gco", None)


def test_without_a_configured_root_cdk_json_comes_from_the_working_directory(
    acme_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GCO_PROJECT_NAME", raising=False)
    monkeypatch.chdir(acme_root)
    deployment.configure()
    assert deployment.project_name() == "acme"
