"""
Base data models and abstract classes for AgentEconomy.

Every module in the system imports from here. This file has zero
dependencies beyond the Python standard library + PyYAML.
"""

import hashlib
import json
import random
import string
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


# ====================================================================== #
# Exceptions
# ====================================================================== #


class InsufficientBalance(Exception):
    """Raised when an entity doesn't have enough tokens."""

    def __init__(self, entity: str, required: float, available: float) -> None:
        self.entity = entity
        self.required = required
        self.available = available
        super().__init__(
            f"{entity} has insufficient balance: needs {required:.2f}, has {available:.2f}"
        )


class EscrowNotFound(Exception):
    """Raised when trying to release/refund a non-existent escrow."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"No active escrow found for task: {task_id}")


class InvalidAmount(Exception):
    """Raised when a token amount is zero or negative."""

    def __init__(self, amount: float) -> None:
        self.amount = amount
        super().__init__(f"Invalid amount: {amount:.2f} (must be > 0)")


class InsufficientStake(Exception):
    """Raised when a validator doesn't have enough staked tokens."""

    def __init__(self, entity: str, required: float, staked: float) -> None:
        self.entity = entity
        self.required = required
        self.staked = staked
        super().__init__(
            f"{entity} has insufficient stake: needs {required:.2f}, has {staked:.2f}"
        )


class StakeNotFound(Exception):
    """Raised when trying to unstake/slash a non-existent stake."""

    def __init__(self, entity: str) -> None:
        self.entity = entity
        super().__init__(f"No active stake found for: {entity}")


# ====================================================================== #
# Data models
# ====================================================================== #


def _generate_id(prefix: str) -> str:
    """Generate a unique ID like 'mint_20260209T100000_a1b2'."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}_{ts}_{rand}"


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TaskSpec:
    """A task definition parsed from YAML."""

    name: str
    description: str
    type: str
    schedule: str
    assigned_to: str = "open"

    # Reward
    reward_amount: float = 0.0
    funded_by: str = ""
    quality_bonus: float = 0.0
    validator_reward: float = 0.0

    # Output
    output_format: str = "markdown"

    # Verification
    verification_required: bool = True
    verification_validator: str = "auto"
    min_quality_threshold: float = 0.0  # minimum quality score to accept output

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "TaskSpec":
        """Parse a task definition from a YAML file.

        Raises:
            FileNotFoundError: if the file doesn't exist.
            ValueError: if required fields are missing.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Task file not found: {path}")

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Task file must be a YAML mapping: {path}")

        # Required fields
        for field_name in ("name", "description", "type", "schedule"):
            if not data.get(field_name):
                raise ValueError(f"Task is missing required field '{field_name}': {path}")

        reward = data.get("reward", {})
        output = data.get("output", {})
        verification = data.get("verification", {})

        return cls(
            name=data["name"],
            description=data["description"].strip(),
            type=data["type"],
            schedule=data["schedule"],
            assigned_to=data.get("assigned_to", "open"),
            reward_amount=float(reward.get("amount", 0)),
            funded_by=reward.get("funded_by", ""),
            quality_bonus=float(reward.get("quality_bonus", 0)),
            validator_reward=float(reward.get("validator_reward", 0)),
            output_format=output.get("format", "markdown"),
            verification_required=verification.get("required", True),
            verification_validator=verification.get("validator", "auto"),
            min_quality_threshold=float(verification.get("min_quality_threshold", 0.0)),
        )

    @property
    def is_free(self) -> bool:
        """True if this task has no token reward."""
        return self.reward_amount <= 0

    @property
    def total_escrow(self) -> float:
        """Total tokens to escrow: reward + validator reward."""
        return self.reward_amount + self.validator_reward


@dataclass
class TaskResult:
    """Result returned by an agent after executing a task."""

    success: bool
    task_name: str
    agent_name: str
    title: str = ""
    summary: str = ""
    output_path: str = ""
    error: str = ""
    quality_score: float = 0.0


@dataclass
class VerificationResult:
    """Result returned by a validator after reviewing task output."""

    passed: bool
    quality_score: float
    feedback: str
    validator_name: str
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class AgentConfig:
    """An agent registration parsed from YAML."""

    name: str
    owner: str
    moltbook_api_key: str = ""
    description: str = ""

    capabilities: List[str] = field(default_factory=list)
    api_keys: Dict[str, str] = field(default_factory=dict)
    accept_free: bool = True

    # Reward split
    reward_split_owner: float = 0.55
    reward_split_agent: float = 0.30
    reward_split_provenance: float = 0.10

    # Proof of contribution
    poc_github_user: str = ""
    poc_merged_pr: str = ""

    # Bidding
    bidding_discount: float = 0.0
    bidding_max_concurrent: int = 3

    # Staking (for validators)
    stake_amount: float = 0.0  # currently staked AGN (set at runtime, not in YAML)

    # Provenance
    provenance_parent: Optional[str] = None
    provenance_version: str = "1.0"

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "AgentConfig":
        """Parse an agent registration from a YAML file.

        Raises:
            FileNotFoundError: if the file doesn't exist.
            ValueError: if required fields are missing.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Agent file not found: {path}")

        with open(path, "r") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Agent file must be a YAML mapping: {path}")

        # Required fields
        for field_name in ("name", "owner"):
            if not data.get(field_name):
                raise ValueError(f"Agent is missing required field '{field_name}': {path}")

        reward_split = data.get("reward_split", {})
        poc = data.get("proof_of_contribution", {})
        bidding = data.get("bidding", {})
        provenance = data.get("provenance", {})

        return cls(
            name=data["name"],
            owner=data["owner"],
            moltbook_api_key=data.get("moltbook_api_key", ""),
            description=str(data.get("description", "")).strip(),
            capabilities=data.get("capabilities", []),
            api_keys=data.get("api_keys", {}),
            accept_free=data.get("accept_free", True),
            reward_split_owner=float(reward_split.get("owner", 0.55)),
            reward_split_agent=float(reward_split.get("agent", 0.30)),
            reward_split_provenance=float(reward_split.get("provenance", 0.10)),
            poc_github_user=poc.get("github_user", ""),
            poc_merged_pr=poc.get("merged_pr", ""),
            bidding_discount=float(bidding.get("default_discount", 0.0)),
            bidding_max_concurrent=int(bidding.get("max_concurrent_tasks", 3)),
            provenance_parent=provenance.get("parent"),
            provenance_version=str(provenance.get("version", "1.0")),
        )

    def has_capability(self, capability: str) -> bool:
        """Check if this agent can handle a given task type."""
        return capability in self.capabilities


@dataclass
class Transaction:
    """A single token movement in the ledger.

    When hash chaining is enabled, each transaction includes a prev_hash
    linking to the previous entry, making the ledger tamper-evident.
    """

    id: str
    type: str  # "mint", "transfer", "escrow", "release", "refund", "fee", "stake", "unstake", "slash"
    from_entity: str
    to_entity: str
    amount: float
    memo: str = ""
    timestamp: str = field(default_factory=_now_iso)
    task_id: Optional[str] = None
    prev_hash: str = ""  # SHA-256 of previous transaction (hash chain)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "type": self.type,
            "from_entity": self.from_entity,
            "to_entity": self.to_entity,
            "amount": self.amount,
            "memo": self.memo,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "prev_hash": self.prev_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Transaction":
        """Deserialize from a dict."""
        return cls(
            id=data["id"],
            type=data["type"],
            from_entity=data.get("from_entity", ""),
            to_entity=data.get("to_entity", ""),
            amount=float(data["amount"]),
            memo=data.get("memo", ""),
            timestamp=data.get("timestamp", ""),
            task_id=data.get("task_id"),
            prev_hash=data.get("prev_hash", ""),
        )

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this transaction's content (excluding prev_hash).

        Used by the hash chain: the next transaction's prev_hash = this hash.
        """
        payload = json.dumps({
            "id": self.id,
            "type": self.type,
            "from_entity": self.from_entity,
            "to_entity": self.to_entity,
            "amount": self.amount,
            "memo": self.memo,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "prev_hash": self.prev_hash,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


# ====================================================================== #
# Abstract base classes
# ====================================================================== #


class BaseAgent(ABC):
    """Abstract base class for task-executing agents."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def capabilities(self) -> List[str]:
        return self.config.capabilities

    @abstractmethod
    def execute(self, task: TaskSpec) -> TaskResult:
        """Execute a task and return the result.

        Subclasses must implement this with their task-specific logic.
        """
        ...


class BaseValidator(ABC):
    """Abstract base class for peer validators."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    def verify(self, task: TaskSpec, result: TaskResult) -> VerificationResult:
        """Review task output and return a verification result.

        Subclasses must implement this with their validation logic.
        """
        ...


class BaseNotifier(ABC):
    """Abstract base class for notification channels."""

    @abstractmethod
    def notify(self, event: str, data: Dict[str, Any]) -> None:
        """Send a notification about an event.

        Args:
            event: event type (e.g., "task_completed", "verification_passed")
            data: event-specific data dict
        """
        ...
