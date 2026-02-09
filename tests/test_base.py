"""Tests for agents.base -- data models, YAML factories, and abstract contracts."""

import os
import re
import tempfile
from pathlib import Path

import pytest
import yaml

from agents.base import (
    AgentConfig,
    BaseAgent,
    BaseNotifier,
    BaseValidator,
    EscrowNotFound,
    InsufficientBalance,
    InsufficientStake,
    InvalidAmount,
    StakeNotFound,
    TaskResult,
    TaskSpec,
    Transaction,
    VerificationResult,
    _generate_id,
)


# ====================================================================== #
# Fixtures -- temp YAML files
# ====================================================================== #


@pytest.fixture
def valid_task_yaml(tmp_path: Path) -> Path:
    """Write a minimal valid task YAML and return its path."""
    data = {
        "name": "test-task",
        "description": "A test task for unit tests.",
        "type": "research",
        "schedule": "daily",
        "assigned_to": "test-agent",
        "reward": {
            "amount": 100,
            "funded_by": "alice",
            "quality_bonus": 50,
            "validator_reward": 15,
        },
        "output": {"format": "json"},
        "verification": {"required": True, "validator": "auto"},
    }
    p = tmp_path / "task.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def minimal_task_yaml(tmp_path: Path) -> Path:
    """Task YAML with only required fields."""
    data = {
        "name": "min-task",
        "description": "Minimal task.",
        "type": "validation",
        "schedule": "every_6_hours",
    }
    p = tmp_path / "min-task.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def valid_agent_yaml(tmp_path: Path) -> Path:
    """Write a full valid agent YAML and return its path."""
    data = {
        "name": "test-agent",
        "owner": "bob",
        "moltbook_api_key": "mlt_test_key",
        "description": "A test agent.",
        "capabilities": ["research", "validation"],
        "api_keys": {"openai": "sk-test"},
        "accept_free": True,
        "reward_split": {"owner": 0.60, "agent": 0.25, "provenance": 0.10},
        "proof_of_contribution": {
            "github_user": "bobdev",
            "merged_pr": "https://github.com/org/repo/pull/42",
        },
        "bidding": {"default_discount": 0.1, "max_concurrent_tasks": 5},
        "provenance": {"parent": "parent-agent", "version": "2.0"},
    }
    p = tmp_path / "agent.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def minimal_agent_yaml(tmp_path: Path) -> Path:
    """Agent YAML with only required fields."""
    data = {"name": "min-agent", "owner": "carol"}
    p = tmp_path / "min-agent.yaml"
    p.write_text(yaml.dump(data))
    return p


# ====================================================================== #
# TaskSpec tests
# ====================================================================== #


class TestTaskSpec:
    def test_from_yaml_full(self, valid_task_yaml: Path) -> None:
        task = TaskSpec.from_yaml(valid_task_yaml)
        assert task.name == "test-task"
        assert "test task" in task.description.lower()
        assert task.type == "research"
        assert task.schedule == "daily"
        assert task.assigned_to == "test-agent"
        assert task.reward_amount == 100.0
        assert task.funded_by == "alice"
        assert task.quality_bonus == 50.0
        assert task.validator_reward == 15.0
        assert task.output_format == "json"
        assert task.verification_required is True
        assert task.verification_validator == "auto"

    def test_from_yaml_minimal(self, minimal_task_yaml: Path) -> None:
        task = TaskSpec.from_yaml(minimal_task_yaml)
        assert task.name == "min-task"
        assert task.assigned_to == "open"  # default
        assert task.reward_amount == 0.0
        assert task.output_format == "markdown"  # default
        assert task.verification_required is True  # default

    def test_from_yaml_missing_name(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"description": "x", "type": "y", "schedule": "z"}))
        with pytest.raises(ValueError, match="name"):
            TaskSpec.from_yaml(p)

    def test_from_yaml_missing_description(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"name": "x", "type": "y", "schedule": "z"}))
        with pytest.raises(ValueError, match="description"):
            TaskSpec.from_yaml(p)

    def test_from_yaml_missing_type(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"name": "x", "description": "y", "schedule": "z"}))
        with pytest.raises(ValueError, match="type"):
            TaskSpec.from_yaml(p)

    def test_from_yaml_missing_schedule(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"name": "x", "description": "y", "type": "z"}))
        with pytest.raises(ValueError, match="schedule"):
            TaskSpec.from_yaml(p)

    def test_from_yaml_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            TaskSpec.from_yaml("/nonexistent/path.yaml")

    def test_from_yaml_not_a_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("- just a list")
        with pytest.raises(ValueError, match="YAML mapping"):
            TaskSpec.from_yaml(p)

    def test_is_free(self) -> None:
        task = TaskSpec(name="t", description="d", type="r", schedule="daily")
        assert task.is_free is True
        task.reward_amount = 50.0
        assert task.is_free is False

    def test_total_escrow(self) -> None:
        task = TaskSpec(
            name="t",
            description="d",
            type="r",
            schedule="daily",
            reward_amount=100.0,
            validator_reward=15.0,
        )
        assert task.total_escrow == 115.0


# ====================================================================== #
# AgentConfig tests
# ====================================================================== #


class TestAgentConfig:
    def test_from_yaml_full(self, valid_agent_yaml: Path) -> None:
        agent = AgentConfig.from_yaml(valid_agent_yaml)
        assert agent.name == "test-agent"
        assert agent.owner == "bob"
        assert agent.moltbook_api_key == "mlt_test_key"
        assert agent.capabilities == ["research", "validation"]
        assert agent.api_keys == {"openai": "sk-test"}
        assert agent.accept_free is True
        assert agent.reward_split_owner == 0.60
        assert agent.reward_split_agent == 0.25
        assert agent.reward_split_provenance == 0.10
        assert agent.poc_github_user == "bobdev"
        assert agent.poc_merged_pr == "https://github.com/org/repo/pull/42"
        assert agent.bidding_discount == 0.1
        assert agent.bidding_max_concurrent == 5
        assert agent.provenance_parent == "parent-agent"
        assert agent.provenance_version == "2.0"

    def test_from_yaml_minimal(self, minimal_agent_yaml: Path) -> None:
        agent = AgentConfig.from_yaml(minimal_agent_yaml)
        assert agent.name == "min-agent"
        assert agent.owner == "carol"
        # Defaults
        assert agent.capabilities == []
        assert agent.accept_free is True
        assert agent.reward_split_owner == 0.55
        assert agent.provenance_parent is None

    def test_from_yaml_missing_name(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"owner": "x"}))
        with pytest.raises(ValueError, match="name"):
            AgentConfig.from_yaml(p)

    def test_from_yaml_missing_owner(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"name": "x"}))
        with pytest.raises(ValueError, match="owner"):
            AgentConfig.from_yaml(p)

    def test_from_yaml_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            AgentConfig.from_yaml("/nonexistent/agent.yaml")

    def test_from_yaml_not_a_mapping(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("just a string")
        with pytest.raises(ValueError, match="YAML mapping"):
            AgentConfig.from_yaml(p)

    def test_has_capability(self, valid_agent_yaml: Path) -> None:
        agent = AgentConfig.from_yaml(valid_agent_yaml)
        assert agent.has_capability("research") is True
        assert agent.has_capability("validation") is True
        assert agent.has_capability("code_review") is False


# ====================================================================== #
# Transaction tests
# ====================================================================== #


class TestTransaction:
    def test_auto_id_generation(self) -> None:
        txn_id = _generate_id("mint")
        assert txn_id.startswith("mint_")
        # format: mint_YYYYMMDDTHHMMSS_xxxx
        assert re.match(r"^mint_\d{8}T\d{6}_[a-z0-9]{4}$", txn_id)

    def test_unique_ids(self) -> None:
        ids = {_generate_id("test") for _ in range(100)}
        # Should be highly unique (collision extremely unlikely in 100 runs)
        assert len(ids) >= 99

    def test_to_dict_roundtrip(self) -> None:
        txn = Transaction(
            id="test_id",
            type="transfer",
            from_entity="alice",
            to_entity="bob",
            amount=42.50,
            memo="payment",
            timestamp="2026-01-01T00:00:00Z",
            task_id="task-1",
        )
        d = txn.to_dict()
        assert d["id"] == "test_id"
        assert d["amount"] == 42.50
        assert d["task_id"] == "task-1"

        # Round-trip
        txn2 = Transaction.from_dict(d)
        assert txn2.id == txn.id
        assert txn2.type == txn.type
        assert txn2.from_entity == txn.from_entity
        assert txn2.to_entity == txn.to_entity
        assert txn2.amount == txn.amount
        assert txn2.memo == txn.memo
        assert txn2.timestamp == txn.timestamp
        assert txn2.task_id == txn.task_id


# ====================================================================== #
# TaskResult and VerificationResult tests
# ====================================================================== #


class TestTaskResult:
    def test_defaults(self) -> None:
        r = TaskResult(success=True, task_name="t", agent_name="a")
        assert r.title == ""
        assert r.summary == ""
        assert r.quality_score == 0.0

    def test_fields(self) -> None:
        r = TaskResult(
            success=False,
            task_name="my-task",
            agent_name="my-agent",
            error="something broke",
        )
        assert r.success is False
        assert r.error == "something broke"


class TestVerificationResult:
    def test_defaults(self) -> None:
        v = VerificationResult(passed=True, quality_score=0.9, feedback="Good", validator_name="v1")
        assert v.passed is True
        assert v.quality_score == 0.9
        assert v.timestamp  # auto-generated, should be non-empty


# ====================================================================== #
# Abstract class contracts
# ====================================================================== #


class TestAbstractContracts:
    def test_base_agent_not_instantiable(self, minimal_agent_yaml: Path) -> None:
        config = AgentConfig.from_yaml(minimal_agent_yaml)
        with pytest.raises(TypeError):
            BaseAgent(config)  # type: ignore[abstract]

    def test_base_validator_not_instantiable(self, minimal_agent_yaml: Path) -> None:
        config = AgentConfig.from_yaml(minimal_agent_yaml)
        with pytest.raises(TypeError):
            BaseValidator(config)  # type: ignore[abstract]

    def test_base_notifier_not_instantiable(self) -> None:
        with pytest.raises(TypeError):
            BaseNotifier()  # type: ignore[abstract]

    def test_concrete_agent_works(self, minimal_agent_yaml: Path) -> None:
        config = AgentConfig.from_yaml(minimal_agent_yaml)

        class DummyAgent(BaseAgent):
            def execute(self, task: TaskSpec) -> TaskResult:
                return TaskResult(success=True, task_name=task.name, agent_name=self.name)

        agent = DummyAgent(config)
        assert agent.name == "min-agent"
        task = TaskSpec(name="t", description="d", type="r", schedule="daily")
        result = agent.execute(task)
        assert result.success is True
        assert result.agent_name == "min-agent"

    def test_concrete_validator_works(self, minimal_agent_yaml: Path) -> None:
        config = AgentConfig.from_yaml(minimal_agent_yaml)

        class DummyValidator(BaseValidator):
            def verify(self, task: TaskSpec, result: TaskResult) -> VerificationResult:
                return VerificationResult(
                    passed=True,
                    quality_score=0.95,
                    feedback="Looks great",
                    validator_name=self.name,
                )

        val = DummyValidator(config)
        task = TaskSpec(name="t", description="d", type="r", schedule="daily")
        result = TaskResult(success=True, task_name="t", agent_name="a")
        vr = val.verify(task, result)
        assert vr.passed is True
        assert vr.quality_score == 0.95

    def test_concrete_notifier_works(self) -> None:
        class DummyNotifier(BaseNotifier):
            def __init__(self) -> None:
                self.events: list[str] = []

            def notify(self, event: str, data: dict) -> None:
                self.events.append(event)

        n = DummyNotifier()
        n.notify("task_completed", {"task": "t"})
        assert n.events == ["task_completed"]


# ====================================================================== #
# Custom exceptions
# ====================================================================== #


class TestExceptions:
    def test_insufficient_balance_message(self) -> None:
        e = InsufficientBalance("alice", 100.0, 50.0)
        assert "alice" in str(e)
        assert "100.00" in str(e)
        assert "50.00" in str(e)

    def test_escrow_not_found_message(self) -> None:
        e = EscrowNotFound("task-42")
        assert "task-42" in str(e)

    def test_invalid_amount_message(self) -> None:
        e = InvalidAmount(-5.0)
        assert "-5.00" in str(e)

    def test_insufficient_stake_message(self) -> None:
        e = InsufficientStake("val-1", 50.0, 10.0)
        assert "val-1" in str(e)
        assert "50.00" in str(e)
        assert "10.00" in str(e)

    def test_stake_not_found_message(self) -> None:
        e = StakeNotFound("ghost-validator")
        assert "ghost-validator" in str(e)


# ====================================================================== #
# Hash chain on Transaction
# ====================================================================== #


class TestTransactionHashChain:
    def test_compute_hash_deterministic(self) -> None:
        txn = Transaction(
            id="test_id",
            type="mint",
            from_entity="",
            to_entity="alice",
            amount=100.0,
            memo="funding",
            timestamp="2026-01-01T00:00:00Z",
            prev_hash="",
        )
        h1 = txn.compute_hash()
        h2 = txn.compute_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_hash_changes_with_content(self) -> None:
        txn1 = Transaction(
            id="id1", type="mint", from_entity="", to_entity="alice",
            amount=100.0, timestamp="2026-01-01T00:00:00Z",
        )
        txn2 = Transaction(
            id="id1", type="mint", from_entity="", to_entity="alice",
            amount=200.0, timestamp="2026-01-01T00:00:00Z",
        )
        assert txn1.compute_hash() != txn2.compute_hash()

    def test_prev_hash_included_in_hash(self) -> None:
        txn1 = Transaction(
            id="id1", type="mint", from_entity="", to_entity="alice",
            amount=100.0, timestamp="2026-01-01T00:00:00Z", prev_hash="",
        )
        txn2 = Transaction(
            id="id1", type="mint", from_entity="", to_entity="alice",
            amount=100.0, timestamp="2026-01-01T00:00:00Z", prev_hash="abc123",
        )
        assert txn1.compute_hash() != txn2.compute_hash()

    def test_to_dict_includes_prev_hash(self) -> None:
        txn = Transaction(
            id="test", type="mint", from_entity="", to_entity="alice",
            amount=100.0, prev_hash="deadbeef",
        )
        d = txn.to_dict()
        assert d["prev_hash"] == "deadbeef"

    def test_from_dict_preserves_prev_hash(self) -> None:
        d = {
            "id": "test", "type": "mint", "from_entity": "", "to_entity": "alice",
            "amount": 100.0, "memo": "", "timestamp": "t", "task_id": None,
            "prev_hash": "cafebabe",
        }
        txn = Transaction.from_dict(d)
        assert txn.prev_hash == "cafebabe"


# ====================================================================== #
# New TaskSpec field: min_quality_threshold
# ====================================================================== #


class TestTaskSpecMinQuality:
    def test_default_min_quality(self) -> None:
        task = TaskSpec(name="t", description="d", type="r", schedule="daily")
        assert task.min_quality_threshold == 0.0

    def test_from_yaml_with_min_quality(self, tmp_path: Path) -> None:
        data = {
            "name": "quality-task",
            "description": "Task with quality gate",
            "type": "code_review",
            "schedule": "daily",
            "verification": {
                "required": True,
                "min_quality_threshold": 0.7,
            },
        }
        p = tmp_path / "quality-task.yaml"
        p.write_text(yaml.dump(data))
        task = TaskSpec.from_yaml(p)
        assert task.min_quality_threshold == 0.7
