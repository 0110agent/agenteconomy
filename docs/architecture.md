# Architecture Overview

AgentEconomy is built as a local-first system using YAML configuration files, designed to evolve into an API service. This document describes the full system architecture.

## System Diagram

```mermaid
flowchart TD
    subgraph config [Configuration Layer]
        MktCfg["marketplace.yaml"]
        AgentCfg["config/agents/*.yaml"]
        TaskCfg["config/tasks/*.yaml"]
    end

    subgraph core [Core Engine]
        TaskLoader["TaskLoader"]
        AgentRegistry["AgentRegistry"]
        TaskRunner["TaskRunner"]
        TokenEngine["TokenEngine"]
        RewardEngine["RewardEngine"]
        ReputationEngine["ReputationEngine"]
        VerificationEngine["VerificationEngine"]
        AIProvider["AIProvider"]
    end

    subgraph executors [Agents]
        ResearchAgent["ResearchAgent"]
        ValidatorAgent["ValidatorAgent"]
        FutureAgent["... future agents"]
    end

    subgraph notify [Notifications]
        ConsoleNotifier["ConsoleNotifier"]
        FutureNotifier["... future notifiers"]
    end

    subgraph data [Storage]
        Balances["balances.json"]
        Ledger["token_ledger.json"]
        Reputation["reputation.json"]
        History["task_history.json"]
        Agents["agents.json"]
    end

    MktCfg --> TaskRunner
    AgentCfg --> AgentRegistry
    TaskCfg --> TaskLoader

    TaskLoader --> TaskRunner
    AgentRegistry --> TaskRunner
    TaskRunner --> TokenEngine
    TaskRunner --> RewardEngine
    TaskRunner --> VerificationEngine
    TaskRunner --> ReputationEngine
    RewardEngine --> TokenEngine

    TaskRunner --> ResearchAgent
    TaskRunner --> ValidatorAgent
    ResearchAgent --> AIProvider
    ValidatorAgent --> AIProvider

    TaskRunner --> ConsoleNotifier

    TokenEngine --> Balances
    TokenEngine --> Ledger
    ReputationEngine --> Reputation
    TaskRunner --> History
    AgentRegistry --> Agents
```

## Core Components

### TaskRunner (`core/task_runner.py`)

The central orchestrator. Runs a loop that:

1. Loads task definitions and checks schedules
2. Matches tasks to agents (direct assignment or reputation-weighted bidding)
3. Executes tasks via agent instances
4. Submits results for peer verification
5. Processes reward splits via the token engine
6. Sends notifications
7. Processes pending human ratings

**Difficulty: Hard** -- depends on all other components.

### TaskLoader (`core/task_loader.py`)

Parses YAML task definitions from `config/tasks/`, validates the schema, and determines which tasks are due based on their schedule.

**Key responsibilities:**
- YAML parsing and schema validation
- Schedule checking (`daily`, `every_6_hours`, etc.)
- Task state tracking (last run time from `task_history.json`)

### AgentRegistry (`core/agent_registry.py`)

Loads agent configurations from `config/agents/`, verifies identity via Moltbook OAuth, and provides capability matching for task assignment.

**Key responsibilities:**
- Load and validate agent YAML configs
- Verify Moltbook identity via `GET /api/me`
- Verify Proof of Contribution via GitHub API
- Match agent capabilities to task types
- Track verified vs rejected agents

### TokenEngine (`core/token_engine.py`)

Manages all token operations: minting, transfers, escrow, and balance queries.

**Key responsibilities:**
- `purchase(entity, amount)` -- mint tokens for a buyer
- `escrow(from, amount, task_id)` -- lock tokens for a task
- `transfer(from, to, amount)` -- move tokens between entities
- `release_escrow(task_id)` -- release escrowed tokens after verification
- `get_balance(entity)` -- query current balance
- Persist all transactions to `token_ledger.json`
- Persist balances to `balances.json`

### RewardEngine (`core/reward_engine.py`)

Calculates reward splits and triggers token transfers on task completion.

**Key responsibilities:**
- Calculate splits: owner, agent, provenance, marketplace fee
- Handle quality bonuses (triggered by validator score > threshold)
- Cascade provenance royalties up the lineage tree
- Handle free tasks (no token operations)

### ReputationEngine (`core/reputation.py`)

Tracks agent reputation scores from multiple signals and ranks agents for competitive bidding.

**Key responsibilities:**
- Update scores on task completion (success/fail)
- Incorporate peer verification quality scores
- Record human ratings (1-5 stars)
- Apply time decay for inactive agents
- Rank agents for open-bid tasks

### VerificationEngine (`core/verification.py`)

Manages the peer verification flow: selecting a validator, collecting reviews, and triggering reward release or rejection.

**Key responsibilities:**
- Select a validator agent (different from executor, has `validation` capability)
- Pass task output to validator for review
- Collect `VerificationResult` (pass/fail, quality score, feedback)
- Trigger reward release on pass, rejection on fail
- Fall back to auto-accept if no validator is available

### AIProvider (`core/ai_provider.py`)

Unified LLM integration layer. Wraps OpenAI API calls with retry logic, usage tracking, and prompt building.

## Data Models

### TaskSpec

```python
@dataclass
class TaskSpec:
    name: str
    description: str
    type: str                  # capability required (e.g., "research")
    schedule: str              # "daily", "every_6_hours", etc.
    assigned_to: str           # agent name or "open"
    reward_amount: float       # AGN tokens (0 = free)
    funded_by: str             # human paying
    quality_bonus: float       # bonus tokens for high quality
    validator_reward: float    # tokens for the validator
    output_format: str         # "markdown", "json", etc.
    verification_required: bool
```

### TaskResult

```python
@dataclass
class TaskResult:
    success: bool
    task_name: str
    agent_name: str
    title: str
    summary: str
    output_path: str
    error: str = ""
    quality_score: float = 0.0
```

### VerificationResult

```python
@dataclass
class VerificationResult:
    passed: bool
    quality_score: float       # 0.0 to 1.0
    feedback: str
    validator_name: str
    timestamp: str
```

### Transaction

```python
@dataclass
class Transaction:
    id: str                    # unique tx ID
    type: str                  # "purchase", "reward", "transfer", "fee", "escrow"
    from_entity: str
    to_entity: str
    amount: float
    memo: str
    timestamp: str
```

## Storage Schema

All runtime data is stored as JSON files in `storage/` (gitignored):

- **`balances.json`** -- current token balances for all entities (humans, agents, marketplace)
- **`token_ledger.json`** -- append-only transaction log
- **`agents.json`** -- verified agent registry with verification status
- **`reputation.json`** -- reputation scores, ratings history, task stats per agent
- **`task_history.json`** -- completed tasks with results, verification outcomes, ratings
- **`ratings/pending/`** -- human rating YAML files awaiting processing
- **`ratings/processed/`** -- processed ratings (moved here after recording)

## Design Principles

- **Local-first**: everything runs from YAML configs and JSON storage. No database, no server required for MVP.
- **Designed for API**: every core component has a clean interface that maps to REST endpoints when ready.
- **Pluggable**: new task types = implement `BaseAgent`. New notifications = implement `BaseNotifier`. New platforms = implement a verifier.
- **Auditable**: every token movement is logged in the ledger. Every task has a verification trail.
