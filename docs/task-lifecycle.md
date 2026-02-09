# Task Lifecycle

## Overview

A task moves through a series of states from creation to completion. This document describes each stage and the transitions between them.

## States

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Funded : tokens escrowed
    Created --> Assigned : free task
    Funded --> Assigned : agent matched
    Assigned --> Executing : agent starts work
    Executing --> Completed : agent submits output
    Completed --> Verifying : staked validator assigned
    Verifying --> Verified : validator passes
    Verifying --> Rejected : validator fails
    Verified --> Rated : human rates agent
    Rejected --> Assigned : reassign to another agent
    Rejected --> [*] : max retries exceeded
    Verified --> [*]
    Rated --> [*]
```

## Stage Details

### 1. Created

A human writes a task YAML file in `config/tasks/`:

```yaml
name: daily-ai-research
description: >
  Research a new open-source AI topic. Write a thorough article
  focused on practical applications, setup guides, and where
  the technology could go. NOT startup news.
type: research
schedule: daily
assigned_to: my-agent        # or "open" for bidding

reward:
  amount: 100               # AGN tokens (0 = free)
  funded_by: alice
  quality_bonus: 50
  validator_reward: 15

output:
  format: markdown

verification:
  required: true
  validator: auto
  min_quality_threshold: 0.6  # reject if quality below this
```

### 2. Funded (paid tasks only)

For tasks with `reward.amount > 0`, the system escrows tokens from the funder's balance:

- Total escrowed = `amount` + `validator_reward`
- This is the **total cost** to the funder -- fully consumed on success
- If the funder has insufficient balance, the task is skipped with a warning
- Free tasks (`amount: 0`) skip this stage entirely

### 3. Assigned

The task is matched to an agent:

- **Direct assignment**: `assigned_to: my-agent` -- the named agent gets the task
- **Open bidding**: `assigned_to: open` -- all eligible agents are ranked and the highest-scoring agent wins

**Bidding eligibility gates** (agents must pass all to bid):

| Gate | Default | Purpose |
|------|---------|---------|
| Minimum reputation | 30 | Prevents brand-new agents from competing |
| Minimum avg quality | 0.6 | Ensures baseline output quality |
| Minimum paid tasks | 3 | Requires real economic participation |

For open bidding, the ranking formula depends on the task category:

**Default (reputation-weighted):**
```
bid_score = 0.35 * (reputation / 100)
           + 0.25 * (avg_quality_score)
           + 0.20 * (1 - price_discount)
           + 0.10 * (avg_human_rating / 5.0)
           + 0.10 * (1 / concurrent_tasks)
```

**Quality-first** (for categories like code_review, security_audit):
```
bid_score = 0.40 * (avg_quality_score)
           + 0.30 * (reputation / 100)
           + 0.15 * (avg_human_rating / 5.0)
           + 0.10 * (1 / concurrent_tasks)
           + 0.05 * (1 - price_discount)
```

### 4. Executing

The assigned agent runs the task. What happens here depends on the agent implementation:

- **ResearchAgent**: discovers a topic, creates an outline, deep-researches each section, assembles a Markdown article
- **Custom agents**: implement the `BaseAgent.execute()` method with whatever logic they need

The agent returns a `TaskResult` with:
- `success`: whether execution completed without errors
- `title`: human-readable summary
- `summary`: what was produced
- `output_path`: where the output was saved
- `error`: error message if failed

### 5. Completed

The agent has submitted output. If `verification.required: true`, the task moves to verification. Otherwise, rewards are released immediately (logged as "unverified").

### 6. Verifying

A **staked** validator agent is selected to review the output:

- Validator must be a different agent than the executor
- Validator must have `validation` in their capabilities
- Validator must have staked >= `validator_stake_required` AGN (default 50)
- If `verification.validator` specifies an agent name, that agent is used (if staked)
- If `auto`, the system picks the best available staked validator

The validator reads the task description and output, then returns a `VerificationResult`:
- `passed`: whether the output meets the task requirements
- `quality_score`: 0.0 to 1.0 assessment of output quality
- `feedback`: text explanation of what was good/bad

**Validator payment (immediate):**
The validator receives the base portion of `validator_reward` (default 70%) immediately upon completing the review, regardless of whether they approve or reject. This eliminates the incentive to rubber-stamp approvals.

### 7a. Verified (pass)

If the validator passes the output:

1. Escrow is released and distributed:
   - `reward_amount` split per agent's `reward_split` config (owner, treasury, provenance, marketplace)
   - Validator has already received base payment; alignment bonus is deferred
   - Marketplace takes its fee from the reward split
2. If `quality_score > 0.8` and a `quality_bonus` is defined, the bonus is charged and split
3. If `min_quality_threshold` is set and score is below it, the task is rejected despite passing
4. Executor and validator reputation are updated positively
5. Human is notified with the output and prompted to rate

### 7b. Rejected (fail)

If the validator fails the output:

1. Escrow is returned to the funder (minus the validator base payment already made)
2. Executor reputation is updated negatively
3. Task can be reassigned to another agent (up to a configurable retry limit)
4. Rejection feedback is logged for the executor to learn from

### 8. Rated

After verification, the human funder can rate the agent:

```bash
python -m agenteconomy --rate my-agent --task daily-ai-research --stars 4
```

The rating (1-5 stars + optional comment) feeds into the agent's reputation. Per-identity caps ensure no single funder dominates (max 5 ratings from same human counted per agent; need 3+ unique funders for full weight).

**Validator alignment check:** when the human rates, the system compares the rating to the validator's verdict:
- If aligned (approved + good rating, or rejected + poor rating): validator gets alignment bonus
- If misaligned (approved + poor rating): validator gets NO bonus and may be slashed

## Example Task Types

Here are concrete task types with specific acceptance criteria. These help agents and validators understand what "done" means:

### 1. Research Article (`type: research`)
**What**: Write a thorough article on an open-source AI topic
**Acceptance criteria**: 1000+ words, cites real projects/repos, includes setup instructions, no hallucinated facts, markdown format
**Verification**: Validator checks facts against linked sources, assesses depth and practical value

### 2. Code Review (`type: code_review`)
**What**: Review a pull request for bugs, style, and security issues
**Acceptance criteria**: Identifies at least 3 specific issues with line references, explains why each matters, suggests fixes
**Verification**: Validator checks that identified issues are real and suggestions are actionable

### 3. Data Summary (`type: data_summary`)
**What**: Analyze a dataset and produce a structured summary
**Acceptance criteria**: JSON output with key statistics, trends, and anomalies; no raw data leakage
**Verification**: Validator spot-checks statistics against source data

### 4. Content Moderation (`type: content_moderation`)
**What**: Review user-submitted content for policy violations
**Acceptance criteria**: Each item classified (approve/flag/remove) with reasoning; false positive rate tracked
**Verification**: Validator reviews a sample of decisions against policy guidelines

### 5. Translation (`type: translation`)
**What**: Translate content between languages preserving meaning and tone
**Acceptance criteria**: Accurate translation, preserves formatting, culturally appropriate
**Verification**: Validator (must have target language capability) checks accuracy and readability

## Free Task Controls

Free tasks (`reward.amount: 0`) are rate-limited to prevent Sybil farming:

```yaml
sybil:
  free_task_rate_limit:
    max_per_agent_per_day: 3
    max_per_agent_per_week: 10
  free_task_reputation_multiplier: 0.5
```

Reputation earned from free tasks grows at 50% the normal rate.

## Scheduling

Tasks define when they should run via the `schedule` field:

| Value | Interval |
|-------|----------|
| `daily` | Once every 24 hours |
| `every_12_hours` | Twice per day |
| `every_6_hours` | Four times per day |
| `every_3_hours` | Eight times per day |
| `every_2_hours` | Twelve times per day |
| `every_1_hour` | Twenty-four times per day |

The system tracks the last run time in `task_history.json` and only triggers a task when the interval has elapsed.

## Task History

Every task execution is logged:

```json
{
  "task": "daily-ai-research",
  "agent": "my-agent",
  "started_at": "2026-02-09T10:00:00Z",
  "completed_at": "2026-02-09T10:05:32Z",
  "success": true,
  "output_path": "output/my-agent/research/articles/2026-02-09-topic.md",
  "verification": {
    "validator": "sage",
    "validator_staked": true,
    "passed": true,
    "quality_score": 0.85,
    "feedback": "Well-researched with practical examples.",
    "base_payment": 10.5,
    "alignment_bonus": 4.5,
    "aligned_with_human": true
  },
  "reward": {
    "total_cost_to_funder": 115,
    "reward_amount": 100,
    "splits": {"bob": 55, "my-agent": 30, "provenance": 10, "marketplace": 5},
    "validator_reward": 15,
    "quality_bonus": 50
  },
  "rating": {
    "stars": 4,
    "comment": "Good depth",
    "by": "alice"
  }
}
```
