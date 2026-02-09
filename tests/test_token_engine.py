"""Tests for core.token_engine -- all business rules, persistence, error cases."""

import json
from pathlib import Path

import pytest

from agents.base import (
    EscrowNotFound,
    InsufficientBalance,
    InsufficientStake,
    InvalidAmount,
    StakeNotFound,
    Transaction,
)
from core.token_engine import TokenEngine


# ====================================================================== #
# Fixtures
# ====================================================================== #


@pytest.fixture
def engine(tmp_path: Path) -> TokenEngine:
    """Create a TokenEngine backed by a temp directory (no marketplace.yaml)."""
    return TokenEngine(
        storage_dir=str(tmp_path / "storage"),
        config_path=str(tmp_path / "nonexistent_config.yaml"),
    )


@pytest.fixture
def engine_with_config(tmp_path: Path) -> TokenEngine:
    """Create a TokenEngine with a real marketplace config."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "marketplace.yaml"
    cfg_file.write_text(
        "token:\n"
        "  name: AGN\n"
        "  decimals: 3\n"
        "marketplace:\n"
        "  treasury: treasury-pool\n"
    )
    return TokenEngine(
        storage_dir=str(tmp_path / "storage"),
        config_path=str(cfg_file),
    )


# ====================================================================== #
# Minting
# ====================================================================== #


class TestMint:
    def test_mint_increases_balance(self, engine: TokenEngine) -> None:
        txn = engine.mint("alice", 500.0, memo="initial funding")
        assert engine.get_balance("alice") == 500.0
        assert txn.type == "mint"
        assert txn.to_entity == "alice"
        assert txn.amount == 500.0
        assert txn.from_entity == ""

    def test_mint_multiple(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.mint("alice", 200.0)
        assert engine.get_balance("alice") == 300.0

    def test_mint_zero_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(InvalidAmount):
            engine.mint("alice", 0.0)

    def test_mint_negative_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(InvalidAmount):
            engine.mint("alice", -10.0)


# ====================================================================== #
# Transfer
# ====================================================================== #


class TestTransfer:
    def test_transfer_between_entities(self, engine: TokenEngine) -> None:
        engine.mint("alice", 1000.0)
        txn = engine.transfer("alice", "bob", 250.0, memo="payment")
        assert engine.get_balance("alice") == 750.0
        assert engine.get_balance("bob") == 250.0
        assert txn.type == "transfer"
        assert txn.from_entity == "alice"
        assert txn.to_entity == "bob"

    def test_transfer_insufficient_balance(self, engine: TokenEngine) -> None:
        engine.mint("alice", 50.0)
        with pytest.raises(InsufficientBalance) as exc_info:
            engine.transfer("alice", "bob", 100.0)
        assert exc_info.value.entity == "alice"
        assert exc_info.value.required == 100.0
        assert exc_info.value.available == 50.0

    def test_transfer_from_empty_account(self, engine: TokenEngine) -> None:
        with pytest.raises(InsufficientBalance):
            engine.transfer("ghost", "bob", 1.0)

    def test_transfer_exact_balance(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.transfer("alice", "bob", 100.0)
        assert engine.get_balance("alice") == 0.0
        assert engine.get_balance("bob") == 100.0

    def test_transfer_zero_raises(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        with pytest.raises(InvalidAmount):
            engine.transfer("alice", "bob", 0.0)

    def test_transfer_negative_raises(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        with pytest.raises(InvalidAmount):
            engine.transfer("alice", "bob", -10.0)


# ====================================================================== #
# Escrow
# ====================================================================== #


class TestEscrow:
    def test_escrow_locks_tokens(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        txn = engine.escrow("alice", 115.0, task_id="task-1")
        assert engine.get_balance("alice") == 385.0
        assert txn.type == "escrow"
        assert txn.task_id == "task-1"
        assert txn.to_entity == "_escrow"

    def test_escrow_insufficient_balance(self, engine: TokenEngine) -> None:
        engine.mint("alice", 50.0)
        with pytest.raises(InsufficientBalance):
            engine.escrow("alice", 100.0, task_id="task-1")

    def test_escrow_zero_raises(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        with pytest.raises(InvalidAmount):
            engine.escrow("alice", 0.0, task_id="task-1")

    def test_escrow_negative_raises(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        with pytest.raises(InvalidAmount):
            engine.escrow("alice", -5.0, task_id="task-1")


# ====================================================================== #
# Release escrow
# ====================================================================== #


class TestReleaseEscrow:
    def test_release_returns_amount(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        engine.escrow("alice", 115.0, task_id="task-1")
        amount = engine.release_escrow("task-1")
        assert amount == 115.0

    def test_release_removes_escrow(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        engine.escrow("alice", 100.0, task_id="task-1")
        engine.release_escrow("task-1")
        # Second release should fail
        with pytest.raises(EscrowNotFound):
            engine.release_escrow("task-1")

    def test_release_nonexistent_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(EscrowNotFound) as exc_info:
            engine.release_escrow("no-such-task")
        assert exc_info.value.task_id == "no-such-task"


# ====================================================================== #
# Refund escrow
# ====================================================================== #


class TestRefundEscrow:
    def test_refund_returns_to_funder(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        engine.escrow("alice", 100.0, task_id="task-1")
        assert engine.get_balance("alice") == 400.0
        txn = engine.refund_escrow("task-1")
        assert engine.get_balance("alice") == 500.0
        assert txn.type == "refund"
        assert txn.to_entity == "alice"
        assert txn.task_id == "task-1"

    def test_refund_removes_escrow(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        engine.escrow("alice", 100.0, task_id="task-1")
        engine.refund_escrow("task-1")
        # Second refund should fail
        with pytest.raises(EscrowNotFound):
            engine.refund_escrow("task-1")

    def test_refund_nonexistent_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(EscrowNotFound):
            engine.refund_escrow("no-such-task")


# ====================================================================== #
# Balances
# ====================================================================== #


class TestBalances:
    def test_unknown_entity_returns_zero(self, engine: TokenEngine) -> None:
        assert engine.get_balance("nobody") == 0.0

    def test_get_all_balances(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.mint("bob", 200.0)
        all_b = engine.get_all_balances()
        assert all_b["alice"] == 100.0
        assert all_b["bob"] == 200.0

    def test_get_all_balances_is_copy(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        all_b = engine.get_all_balances()
        all_b["alice"] = 9999.0  # mutate the copy
        assert engine.get_balance("alice") == 100.0  # original unchanged


# ====================================================================== #
# Transactions / Ledger
# ====================================================================== #


class TestTransactions:
    def test_ledger_grows(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.mint("bob", 50.0)
        engine.transfer("alice", "bob", 25.0)
        txns = engine.get_transactions()
        assert len(txns) == 3

    def test_filter_by_entity(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.mint("bob", 50.0)
        engine.transfer("alice", "bob", 25.0)
        # Alice is involved in mint + transfer = 2 txns
        alice_txns = engine.get_transactions(entity="alice")
        assert len(alice_txns) == 2

    def test_limit(self, engine: TokenEngine) -> None:
        for i in range(10):
            engine.mint("alice", 1.0)
        txns = engine.get_transactions(limit=3)
        assert len(txns) == 3

    def test_most_recent_first(self, engine: TokenEngine) -> None:
        engine.mint("alice", 1.0, memo="first")
        engine.mint("alice", 2.0, memo="second")
        txns = engine.get_transactions()
        assert txns[0].memo == "second"
        assert txns[1].memo == "first"


# ====================================================================== #
# Persistence
# ====================================================================== #


class TestPersistence:
    def test_save_and_reload(self, tmp_path: Path) -> None:
        storage = str(tmp_path / "storage")
        cfg = str(tmp_path / "no_config.yaml")

        # Create engine, do operations
        e1 = TokenEngine(storage_dir=storage, config_path=cfg)
        e1.mint("alice", 1000.0)
        e1.transfer("alice", "bob", 250.0)
        e1.escrow("alice", 100.0, task_id="task-1")

        # Create a new engine from the same storage directory
        e2 = TokenEngine(storage_dir=storage, config_path=cfg)
        assert e2.get_balance("alice") == 650.0
        assert e2.get_balance("bob") == 250.0
        assert len(e2.get_transactions()) == 3

        # Escrow should also be restored
        amount = e2.release_escrow("task-1")
        assert amount == 100.0

    def test_files_created_on_disk(self, engine: TokenEngine, tmp_path: Path) -> None:
        engine.mint("alice", 100.0)
        storage = tmp_path / "storage"
        assert (storage / "balances.json").exists()
        assert (storage / "token_ledger.json").exists()
        assert (storage / "escrows.json").exists()

    def test_ledger_json_format(self, engine: TokenEngine, tmp_path: Path) -> None:
        engine.mint("alice", 100.0)
        ledger_path = tmp_path / "storage" / "token_ledger.json"
        data = json.loads(ledger_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["type"] == "mint"
        assert data[0]["to_entity"] == "alice"
        assert data[0]["amount"] == 100.0


# ====================================================================== #
# Decimal rounding
# ====================================================================== #


class TestDecimalRounding:
    def test_default_two_decimal_places(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.999)
        assert engine.get_balance("alice") == 101.0

    def test_custom_precision_from_config(self, engine_with_config: TokenEngine) -> None:
        # Config says decimals: 3
        engine_with_config.mint("alice", 100.9999)
        assert engine_with_config.get_balance("alice") == 101.0

    def test_rounding_in_transfer(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.transfer("alice", "bob", 33.335)
        # 33.335 rounds to 33.34 at 2 decimal places
        assert engine.get_balance("bob") == 33.34
        assert engine.get_balance("alice") == 66.66


# ====================================================================== #
# Edge cases
# ====================================================================== #


class TestEdgeCases:
    def test_multiple_escrows_different_tasks(self, engine: TokenEngine) -> None:
        engine.mint("alice", 1000.0)
        engine.escrow("alice", 100.0, task_id="task-1")
        engine.escrow("alice", 200.0, task_id="task-2")
        assert engine.get_balance("alice") == 700.0

        engine.release_escrow("task-1")
        engine.refund_escrow("task-2")
        assert engine.get_balance("alice") == 900.0  # 700 + 200 refunded

    def test_escrow_then_refund_then_re_escrow(self, engine: TokenEngine) -> None:
        engine.mint("alice", 500.0)
        engine.escrow("alice", 200.0, task_id="task-1")
        engine.refund_escrow("task-1")
        assert engine.get_balance("alice") == 500.0
        # Can escrow the same task_id again
        engine.escrow("alice", 200.0, task_id="task-1")
        assert engine.get_balance("alice") == 300.0

    def test_chain_of_transfers(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.transfer("alice", "bob", 100.0)
        engine.transfer("bob", "carol", 100.0)
        engine.transfer("carol", "dave", 100.0)
        assert engine.get_balance("alice") == 0.0
        assert engine.get_balance("bob") == 0.0
        assert engine.get_balance("carol") == 0.0
        assert engine.get_balance("dave") == 100.0

    def test_mixed_operations_ledger_integrity(self, engine: TokenEngine) -> None:
        """Verify the full ledger after a mix of operations."""
        engine.mint("alice", 1000.0)
        engine.transfer("alice", "bob", 200.0)
        engine.escrow("alice", 300.0, task_id="t1")
        engine.release_escrow("t1")
        engine.escrow("alice", 150.0, task_id="t2")
        engine.refund_escrow("t2")

        txns = engine.get_transactions()
        types = [t.type for t in txns]
        # Most recent first
        assert types == ["refund", "escrow", "release", "escrow", "transfer", "mint"]

        # mint 1000 -> alice = 1000
        # transfer 200 -> alice = 800, bob = 200
        # escrow 300 -> alice = 500
        # release t1 -> no balance change (escrow popped, amount returned to caller)
        # escrow 150 -> alice = 350
        # refund t2 -> alice = 500
        assert engine.get_balance("alice") == 500.0
        assert engine.get_balance("bob") == 200.0


# ====================================================================== #
# Staking
# ====================================================================== #


class TestStake:
    def test_stake_deducts_balance(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        txn = engine.stake("validator", 50.0)
        assert engine.get_balance("validator") == 150.0
        assert engine.get_stake("validator") == 50.0
        assert txn.type == "stake"

    def test_stake_accumulates(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 30.0)
        engine.stake("validator", 20.0)
        assert engine.get_stake("validator") == 50.0
        assert engine.get_balance("validator") == 150.0

    def test_stake_insufficient_balance(self, engine: TokenEngine) -> None:
        engine.mint("validator", 10.0)
        with pytest.raises(InsufficientBalance):
            engine.stake("validator", 50.0)

    def test_stake_zero_raises(self, engine: TokenEngine) -> None:
        engine.mint("validator", 100.0)
        with pytest.raises(InvalidAmount):
            engine.stake("validator", 0.0)

    def test_stake_negative_raises(self, engine: TokenEngine) -> None:
        engine.mint("validator", 100.0)
        with pytest.raises(InvalidAmount):
            engine.stake("validator", -10.0)


class TestUnstake:
    def test_unstake_all(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 50.0)
        txn = engine.unstake("validator")
        assert engine.get_balance("validator") == 200.0
        assert engine.get_stake("validator") == 0.0
        assert txn.type == "unstake"
        assert txn.amount == 50.0

    def test_unstake_partial(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 50.0)
        engine.unstake("validator", 20.0)
        assert engine.get_balance("validator") == 170.0
        assert engine.get_stake("validator") == 30.0

    def test_unstake_no_stake_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(StakeNotFound):
            engine.unstake("nobody")

    def test_unstake_more_than_staked_raises(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 50.0)
        with pytest.raises(InsufficientStake):
            engine.unstake("validator", 100.0)

    def test_unstake_zero_raises(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 50.0)
        with pytest.raises(InvalidAmount):
            engine.unstake("validator", 0.0)


class TestSlash:
    def test_slash_reduces_stake(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 100.0)
        txn = engine.slash("validator", reason="rubber-stamped bad work", task_id="t1")
        # Default slash is 20% of stake = 20.0
        assert engine.get_stake("validator") == 80.0
        assert txn.type == "slash"
        assert txn.amount == 20.0
        assert txn.task_id == "t1"

    def test_slash_goes_to_treasury(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 100.0)
        engine.slash("validator")
        # Slashed amount should go to marketplace treasury
        assert engine.get_balance("marketplace") == 20.0

    def test_slash_no_stake_raises(self, engine: TokenEngine) -> None:
        with pytest.raises(StakeNotFound):
            engine.slash("nobody")

    def test_repeated_slashing_depletes_stake(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 100.0)
        # Slash 5 times: 100 -> 80 -> 64 -> 51.2 -> 40.96 -> 32.77
        for _ in range(5):
            engine.slash("validator")
        assert engine.get_stake("validator") < 40.0


class TestEligibility:
    def test_eligible_with_sufficient_stake(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 50.0)
        assert engine.is_eligible_validator("validator") is True

    def test_not_eligible_with_low_stake(self, engine: TokenEngine) -> None:
        engine.mint("validator", 200.0)
        engine.stake("validator", 10.0)
        assert engine.is_eligible_validator("validator") is False

    def test_not_eligible_with_no_stake(self, engine: TokenEngine) -> None:
        assert engine.is_eligible_validator("nobody") is False

    def test_get_all_stakes(self, engine: TokenEngine) -> None:
        engine.mint("val-a", 100.0)
        engine.mint("val-b", 100.0)
        engine.stake("val-a", 50.0)
        engine.stake("val-b", 30.0)
        stakes = engine.get_all_stakes()
        assert stakes["val-a"] == 50.0
        assert stakes["val-b"] == 30.0


# ====================================================================== #
# Hash chain
# ====================================================================== #


class TestHashChain:
    def test_chain_valid_after_operations(self, engine: TokenEngine) -> None:
        engine.mint("alice", 1000.0)
        engine.transfer("alice", "bob", 100.0)
        engine.escrow("alice", 200.0, task_id="t1")
        assert engine.verify_chain() is True

    def test_chain_entries_linked(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        engine.mint("bob", 200.0)
        txns = engine.get_transactions()
        # Most recent first, so txns[0] is bob's mint, txns[1] is alice's mint
        # alice's mint (first in ledger) should have empty prev_hash
        assert txns[1].prev_hash == ""
        # bob's mint should reference alice's mint hash
        assert txns[0].prev_hash == txns[1].compute_hash()

    def test_tampered_ledger_detected(self, engine: TokenEngine) -> None:
        engine.mint("alice", 1000.0)
        engine.mint("bob", 200.0)
        engine.transfer("alice", "bob", 50.0)
        assert engine.verify_chain() is True

        # Tamper with the first transaction
        engine._ledger[0].amount = 9999.0
        assert engine.verify_chain() is False

    def test_empty_ledger_valid(self, engine: TokenEngine) -> None:
        assert engine.verify_chain() is True

    def test_single_entry_valid(self, engine: TokenEngine) -> None:
        engine.mint("alice", 100.0)
        assert engine.verify_chain() is True


# ====================================================================== #
# Staking persistence
# ====================================================================== #


class TestStakePersistence:
    def test_stakes_survive_reload(self, tmp_path: Path) -> None:
        storage = str(tmp_path / "storage")
        cfg = str(tmp_path / "no_config.yaml")

        e1 = TokenEngine(storage_dir=storage, config_path=cfg)
        e1.mint("validator", 200.0)
        e1.stake("validator", 60.0)

        e2 = TokenEngine(storage_dir=storage, config_path=cfg)
        assert e2.get_stake("validator") == 60.0
        assert e2.get_balance("validator") == 140.0

    def test_hash_chain_valid_after_reload(self, tmp_path: Path) -> None:
        storage = str(tmp_path / "storage")
        cfg = str(tmp_path / "no_config.yaml")

        e1 = TokenEngine(storage_dir=storage, config_path=cfg)
        e1.mint("alice", 1000.0)
        e1.transfer("alice", "bob", 100.0)
        e1.stake("alice", 50.0)

        e2 = TokenEngine(storage_dir=storage, config_path=cfg)
        assert e2.verify_chain() is True
