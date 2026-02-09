"""
TokenEngine -- standalone token ledger with escrow and staking support.

Manages all AGN token operations: minting, transfers, escrow lock/release/refund,
validator staking/unstaking/slashing, and a tamper-evident hash-chained ledger.

Persists balances, stakes, and a full transaction ledger to disk (JSON).

Trust model (MVP): single-node coordinator. All state is local JSON files.
The hash chain provides tamper-evidence, not consensus.

Depends only on agents.base for the Transaction dataclass and custom exceptions.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agents.base import (
    EscrowNotFound,
    InsufficientBalance,
    InsufficientStake,
    InvalidAmount,
    StakeNotFound,
    Transaction,
    _generate_id,
)


class TokenEngine:
    """Core token ledger for the AgentEconomy marketplace.

    MVP runs as a single-node coordinator: one process owns all state.
    The hash-chained ledger provides tamper-evidence (any edit to a past
    entry breaks the chain and is detectable via verify_chain()).
    """

    def __init__(self, storage_dir: str = "storage", config_path: str = "config/marketplace.yaml") -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # Load config
        self._decimals: int = 2
        self._treasury_name: str = "marketplace"
        self._hash_chain_enabled: bool = True
        self._validator_stake_required: float = 50.0
        self._slash_percent: float = 0.20
        self._load_config(config_path)

        # Internal state
        self._balances: Dict[str, float] = {}
        self._escrows: Dict[str, dict] = {}  # task_id -> {"from": entity, "amount": float}
        self._stakes: Dict[str, float] = {}  # entity -> staked amount
        self._ledger: List[Transaction] = []

        # Load any existing state from disk
        self._load()

    # ------------------------------------------------------------------ #
    # Config
    # ------------------------------------------------------------------ #

    def _load_config(self, config_path: str) -> None:
        """Load marketplace settings from YAML."""
        p = Path(config_path)
        if p.exists():
            with open(p, "r") as f:
                cfg = yaml.safe_load(f)
            if cfg:
                token_cfg = cfg.get("token", {})
                self._decimals = int(token_cfg.get("decimals", 2))
                mp_cfg = cfg.get("marketplace", {})
                self._treasury_name = mp_cfg.get("treasury", "marketplace")
                trust_cfg = cfg.get("trust", {})
                self._hash_chain_enabled = trust_cfg.get("ledger_integrity", True)
                staking_cfg = cfg.get("staking", {})
                self._validator_stake_required = float(staking_cfg.get("validator_stake_required", 50.0))
                self._slash_percent = float(staking_cfg.get("slash_percent", 20)) / 100.0

    # ------------------------------------------------------------------ #
    # Public API -- Minting
    # ------------------------------------------------------------------ #

    def mint(self, entity: str, amount: float, memo: str = "") -> Transaction:
        """Create tokens out of thin air and credit them to *entity*.

        Args:
            entity: the account to credit.
            amount: number of tokens to create (must be > 0).
            memo: optional human-readable note.

        Returns:
            The minting Transaction.

        Raises:
            InvalidAmount: if amount <= 0.
        """
        amount = self._round(amount)
        if amount <= 0:
            raise InvalidAmount(amount)

        self._balances.setdefault(entity, 0.0)
        self._balances[entity] = self._round(self._balances[entity] + amount)

        txn = self._create_transaction(
            type="mint",
            from_entity="",
            to_entity=entity,
            amount=amount,
            memo=memo,
        )
        self._ledger.append(txn)
        self._save()
        return txn

    # ------------------------------------------------------------------ #
    # Public API -- Transfers
    # ------------------------------------------------------------------ #

    def transfer(self, from_entity: str, to_entity: str, amount: float, memo: str = "") -> Transaction:
        """Transfer tokens between two entities.

        Args:
            from_entity: sender account.
            to_entity: receiver account.
            amount: tokens to transfer (must be > 0).
            memo: optional note.

        Returns:
            The transfer Transaction.

        Raises:
            InvalidAmount: if amount <= 0.
            InsufficientBalance: if sender lacks funds.
        """
        amount = self._round(amount)
        if amount <= 0:
            raise InvalidAmount(amount)

        available = self.get_balance(from_entity)
        if available < amount:
            raise InsufficientBalance(from_entity, amount, available)

        self._balances[from_entity] = self._round(self._balances[from_entity] - amount)
        self._balances.setdefault(to_entity, 0.0)
        self._balances[to_entity] = self._round(self._balances[to_entity] + amount)

        txn = self._create_transaction(
            type="transfer",
            from_entity=from_entity,
            to_entity=to_entity,
            amount=amount,
            memo=memo,
        )
        self._ledger.append(txn)
        self._save()
        return txn

    # ------------------------------------------------------------------ #
    # Public API -- Escrow
    # ------------------------------------------------------------------ #

    def escrow(self, from_entity: str, amount: float, task_id: str) -> Transaction:
        """Lock tokens for a task. Debits from_entity, holds in escrow.

        Args:
            from_entity: the funder whose balance is debited.
            amount: tokens to lock (must be > 0).
            task_id: unique task identifier for later release/refund.

        Returns:
            The escrow Transaction.

        Raises:
            InvalidAmount: if amount <= 0.
            InsufficientBalance: if funder lacks funds.
        """
        amount = self._round(amount)
        if amount <= 0:
            raise InvalidAmount(amount)

        available = self.get_balance(from_entity)
        if available < amount:
            raise InsufficientBalance(from_entity, amount, available)

        # Debit funder
        self._balances[from_entity] = self._round(self._balances[from_entity] - amount)

        # Record escrow
        self._escrows[task_id] = {"from": from_entity, "amount": amount}

        txn = self._create_transaction(
            type="escrow",
            from_entity=from_entity,
            to_entity="_escrow",
            amount=amount,
            memo=f"task:{task_id}",
            task_id=task_id,
        )
        self._ledger.append(txn)
        self._save()
        return txn

    def release_escrow(self, task_id: str) -> float:
        """Release escrowed tokens (on successful verification).

        The returned amount should be distributed by the RewardEngine.

        Args:
            task_id: the task whose escrow to release.

        Returns:
            The escrowed amount.

        Raises:
            EscrowNotFound: if task_id has no active escrow.
        """
        if task_id not in self._escrows:
            raise EscrowNotFound(task_id)

        record = self._escrows.pop(task_id)
        amount = record["amount"]

        txn = self._create_transaction(
            type="release",
            from_entity="_escrow",
            to_entity="",  # distributed later by RewardEngine
            amount=amount,
            memo=f"task:{task_id}",
            task_id=task_id,
        )
        self._ledger.append(txn)
        self._save()
        return amount

    def refund_escrow(self, task_id: str) -> Transaction:
        """Refund escrowed tokens to the original funder (on task rejection).

        Args:
            task_id: the task whose escrow to refund.

        Returns:
            The refund Transaction.

        Raises:
            EscrowNotFound: if task_id has no active escrow.
        """
        if task_id not in self._escrows:
            raise EscrowNotFound(task_id)

        record = self._escrows.pop(task_id)
        funder = record["from"]
        amount = record["amount"]

        self._balances.setdefault(funder, 0.0)
        self._balances[funder] = self._round(self._balances[funder] + amount)

        txn = self._create_transaction(
            type="refund",
            from_entity="_escrow",
            to_entity=funder,
            amount=amount,
            memo=f"task:{task_id}",
            task_id=task_id,
        )
        self._ledger.append(txn)
        self._save()
        return txn

    # ------------------------------------------------------------------ #
    # Public API -- Staking (for validators)
    # ------------------------------------------------------------------ #

    def stake(self, entity: str, amount: float) -> Transaction:
        """Lock tokens as a validator stake.

        Validators must stake to be eligible for verification work.
        Staked tokens are deducted from the entity's balance and held
        separately. They can be slashed for misaligned verifications
        or returned via unstake().

        Args:
            entity: the validator staking tokens.
            amount: tokens to stake (must be > 0).

        Returns:
            The stake Transaction.

        Raises:
            InvalidAmount: if amount <= 0.
            InsufficientBalance: if entity lacks funds.
        """
        amount = self._round(amount)
        if amount <= 0:
            raise InvalidAmount(amount)

        available = self.get_balance(entity)
        if available < amount:
            raise InsufficientBalance(entity, amount, available)

        self._balances[entity] = self._round(self._balances[entity] - amount)
        self._stakes.setdefault(entity, 0.0)
        self._stakes[entity] = self._round(self._stakes[entity] + amount)

        txn = self._create_transaction(
            type="stake",
            from_entity=entity,
            to_entity="_stake",
            amount=amount,
            memo="validator stake",
        )
        self._ledger.append(txn)
        self._save()
        return txn

    def unstake(self, entity: str, amount: Optional[float] = None) -> Transaction:
        """Return staked tokens to the validator's balance.

        Args:
            entity: the validator unstaking.
            amount: tokens to unstake (None = unstake all).

        Returns:
            The unstake Transaction.

        Raises:
            StakeNotFound: if entity has no active stake.
            InvalidAmount: if amount > staked amount or <= 0.
        """
        if entity not in self._stakes or self._stakes[entity] <= 0:
            raise StakeNotFound(entity)

        current_stake = self._stakes[entity]
        if amount is None:
            amount = current_stake
        else:
            amount = self._round(amount)
            if amount <= 0:
                raise InvalidAmount(amount)
            if amount > current_stake:
                raise InsufficientStake(entity, amount, current_stake)

        self._stakes[entity] = self._round(self._stakes[entity] - amount)
        self._balances.setdefault(entity, 0.0)
        self._balances[entity] = self._round(self._balances[entity] + amount)

        # Clean up zero stakes
        if self._stakes[entity] <= 0:
            del self._stakes[entity]

        txn = self._create_transaction(
            type="unstake",
            from_entity="_stake",
            to_entity=entity,
            amount=amount,
            memo="validator unstake",
        )
        self._ledger.append(txn)
        self._save()
        return txn

    def slash(self, entity: str, reason: str = "", task_id: Optional[str] = None) -> Transaction:
        """Slash a percentage of a validator's stake for misaligned verification.

        The slashed amount is transferred to the marketplace treasury.

        Args:
            entity: the validator being slashed.
            reason: human-readable reason for the slash.
            task_id: the task that triggered the slash (optional).

        Returns:
            The slash Transaction.

        Raises:
            StakeNotFound: if entity has no active stake.
        """
        if entity not in self._stakes or self._stakes[entity] <= 0:
            raise StakeNotFound(entity)

        slash_amount = self._round(self._stakes[entity] * self._slash_percent)
        if slash_amount <= 0:
            slash_amount = self._round(0.01)  # minimum slash

        self._stakes[entity] = self._round(self._stakes[entity] - slash_amount)

        # Slashed tokens go to marketplace treasury
        self._balances.setdefault(self._treasury_name, 0.0)
        self._balances[self._treasury_name] = self._round(
            self._balances[self._treasury_name] + slash_amount
        )

        # Clean up zero stakes
        if self._stakes[entity] <= 0:
            del self._stakes[entity]

        txn = self._create_transaction(
            type="slash",
            from_entity=entity,
            to_entity=self._treasury_name,
            amount=slash_amount,
            memo=f"slash: {reason}" if reason else "validator slash",
            task_id=task_id,
        )
        self._ledger.append(txn)
        self._save()
        return txn

    def get_stake(self, entity: str) -> float:
        """Get the current staked amount for an entity."""
        return self._round(self._stakes.get(entity, 0.0))

    def get_all_stakes(self) -> Dict[str, float]:
        """Return a copy of all entity stakes."""
        return {k: self._round(v) for k, v in self._stakes.items()}

    def is_eligible_validator(self, entity: str) -> bool:
        """Check if an entity has staked enough to act as a validator."""
        return self.get_stake(entity) >= self._validator_stake_required

    # ------------------------------------------------------------------ #
    # Public API -- Queries
    # ------------------------------------------------------------------ #

    def get_balance(self, entity: str) -> float:
        """Get the current balance for an entity (defaults to 0.0)."""
        return self._round(self._balances.get(entity, 0.0))

    def get_all_balances(self) -> Dict[str, float]:
        """Return a copy of all entity balances."""
        return {k: self._round(v) for k, v in self._balances.items()}

    def get_transactions(self, entity: Optional[str] = None, limit: int = 100) -> List[Transaction]:
        """Return recent transactions, optionally filtered by entity.

        Args:
            entity: if provided, only show transactions involving this entity.
            limit: maximum number of transactions to return (most recent first).
        """
        if entity is None:
            txns = self._ledger
        else:
            txns = [
                t for t in self._ledger
                if t.from_entity == entity or t.to_entity == entity
            ]
        return list(reversed(txns[-limit:]))

    # ------------------------------------------------------------------ #
    # Hash chain verification
    # ------------------------------------------------------------------ #

    def verify_chain(self) -> bool:
        """Verify the integrity of the hash chain.

        Walks the entire ledger and checks that each transaction's prev_hash
        matches the hash of the previous transaction. Returns True if the
        chain is intact, False if any tampering is detected.

        Only meaningful when hash chaining is enabled.
        """
        if not self._hash_chain_enabled or len(self._ledger) == 0:
            return True

        for i in range(1, len(self._ledger)):
            expected_prev = self._ledger[i - 1].compute_hash()
            if self._ledger[i].prev_hash != expected_prev:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _save(self) -> None:
        """Persist balances, escrows, stakes, and ledger to disk."""
        # Balances
        balances_path = self._storage_dir / "balances.json"
        with open(balances_path, "w") as f:
            json.dump(self._balances, f, indent=2)

        # Escrows
        escrows_path = self._storage_dir / "escrows.json"
        with open(escrows_path, "w") as f:
            json.dump(self._escrows, f, indent=2)

        # Stakes
        stakes_path = self._storage_dir / "stakes.json"
        with open(stakes_path, "w") as f:
            json.dump(self._stakes, f, indent=2)

        # Ledger
        ledger_path = self._storage_dir / "token_ledger.json"
        with open(ledger_path, "w") as f:
            json.dump([t.to_dict() for t in self._ledger], f, indent=2)

    def _load(self) -> None:
        """Load state from disk. If files don't exist, start fresh."""
        # Balances
        balances_path = self._storage_dir / "balances.json"
        if balances_path.exists():
            with open(balances_path, "r") as f:
                self._balances = json.load(f)

        # Escrows
        escrows_path = self._storage_dir / "escrows.json"
        if escrows_path.exists():
            with open(escrows_path, "r") as f:
                self._escrows = json.load(f)

        # Stakes
        stakes_path = self._storage_dir / "stakes.json"
        if stakes_path.exists():
            with open(stakes_path, "r") as f:
                self._stakes = json.load(f)

        # Ledger
        ledger_path = self._storage_dir / "token_ledger.json"
        if ledger_path.exists():
            with open(ledger_path, "r") as f:
                raw = json.load(f)
                self._ledger = [Transaction.from_dict(t) for t in raw]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _round(self, amount: float) -> float:
        """Round to configured decimal precision."""
        return round(amount, self._decimals)

    def _create_transaction(
        self,
        type: str,
        from_entity: str,
        to_entity: str,
        amount: float,
        memo: str = "",
        task_id: Optional[str] = None,
    ) -> Transaction:
        """Create a new Transaction with hash chain linking."""
        prev_hash = ""
        if self._hash_chain_enabled and self._ledger:
            prev_hash = self._ledger[-1].compute_hash()

        return Transaction(
            id=_generate_id(type),
            type=type,
            from_entity=from_entity,
            to_entity=to_entity,
            amount=amount,
            memo=memo,
            task_id=task_id,
            prev_hash=prev_hash,
        )
