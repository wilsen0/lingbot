# Requirements Document

## Introduction

The group-chat batching pipeline in `packages/agent/src/linling_agent/group_batch.py`
currently uses a rule-based check (`_is_attention_candidate`) to decide whether a
buffered batch is worth invoking the main LLM for. Batches that never trigger the
rule are held until `max_hold_s` (30s default) and then silently dropped.

This feature adds the **Attention Probe**: a lightweight, second-stage LLM call
that runs once at the `window_s` (8s default) flush boundary when no rule-based
attention has been seen. The probe answers a single yes/no question — "is any
message in this batch worth a reply?" — and, on yes, routes the batch through
the existing main-LLM selective-replier flow exactly as if the rule had fired.
On no, network failure, or malformed output, the existing drop behaviour is
preserved (fail-closed).

The probe reuses the existing `OpenAIProvider` against an OpenAI-compatible
endpoint, with a separate API key, base URL, and model that all fall back to
the main LLM's settings when unset. This gives operators the option of pointing
the probe at a cheaper model on the same vendor while leaving deployments that
do nothing identical to today.

The probe is **stateless** with respect to conversation history. It sees only
the buffered batch text, never the prior conversation. "Continuity" is at the
platform/tooling level (same provider class, same env-var conventions, same
HTTPS surface), not at the conversation level.

### Out of Scope

The following are deliberately deferred and MUST NOT be addressed by this
feature:

- Non-OpenAI-compatible probe providers (Anthropic, Gemini, etc.).
- Multi-shot or iterative probing within a single batch.
- Conversation-history-aware probing.
- Caching probe verdicts across batches.
- Per-group probe configuration.

## Glossary

- **Attention_Probe**: The lightweight second-stage LLM call introduced by this
  feature. Implemented as a small async component invoked from the
  `GroupBatchChatDispatcher` flush loop.
- **GroupBatchChatDispatcher**: Existing async dispatcher in
  `packages/agent/src/linling_agent/group_batch.py` that buffers group-chat
  messages and decides when to invoke the main LLM.
- **GroupBatchConfig**: Existing frozen dataclass that holds batching knobs
  (`window_s`, `max_hold_s`, `require_attention`, etc.). Extended by this
  feature with one new field.
- **Rule_Based_Attention_Detector**: The existing `_is_attention_candidate`
  method on `GroupBatchChatDispatcher`. Unchanged by this feature.
- **Main_LLM_Selector**: The existing tool-using selective-replier flow
  (`_dispatch_batch_with_tools` and the JSON-mode fallback). Invoked when
  `attention_seen` is true at flush time.
- **BotConfig**: The pydantic-settings-backed config in
  `packages/core/src/linling_core/config.py`, populated from `bot.yaml` with
  `${VAR}` env interpolation. Holds the `agent.group_batch_*` block.
- **AgentDef**: The YAML-loaded agent definition in
  `packages/agent/src/linling_agent/agent_def.py`. Source of the main LLM's
  model, base URL, and API key (with `OPENAI_*` env-var fallbacks).
- **OpenAIProvider**: Existing OpenAI-compatible HTTPS provider in
  `packages/agent/src/linling_agent/providers/openai.py`. Reused by the probe.
- **Probe_Verdict**: The boolean outcome of an `Attention_Probe` invocation.
  `true` means "at least one message in the batch is worth a reply"; `false`
  means "no message in the batch is worth a reply".
- **Probe_State**: Per-batch flag stored on the existing `_GroupState` that
  records whether the probe has already run for the current batch lifecycle.
- **Batch_Lifecycle**: The interval from when `_GroupState` first buffers a
  message until `_reset_state_locked` is called (flush, drop, stop, or
  history clear).
- **Probe_Configured**: The runtime condition that the probe has both
  `group_batch_attention_probe_enabled=true` in `BotConfig` and a usable API
  key resolved at startup (per Requirement 3).

## Requirements

### Requirement 1: Configurable Probe with Default On

**User Story:** As an operator, I want to enable or disable the attention probe
through `bot.yaml`, so that I can opt out of the second-stage LLM call without
editing code.

#### Acceptance Criteria

1. THE BotConfig SHALL expose a boolean field
   `agent.group_batch_attention_probe_enabled` with default value `true`.
2. THE GroupBatchConfig SHALL expose a boolean field
   `attention_probe_enabled` with default value `false` so that pre-existing
   test instantiations of `GroupBatchConfig(...)` without the new field
   continue to construct successfully.
3. WHEN the bot starts with `agent.group_batch_attention_probe_enabled=false`,
   THE GroupBatchChatDispatcher SHALL behave identically to the current
   implementation, with no Attention_Probe invocations for any batch.
4. WHEN the bot starts with `agent.group_batch_attention_probe_enabled=true`
   AND a usable API key is resolved per Requirement 3, THE
   GroupBatchChatDispatcher SHALL be eligible to invoke the Attention_Probe
   under the conditions stated in Requirement 5.

### Requirement 2: Credential Resolution from `.env`

**User Story:** As an operator, I want to point the probe at a different model
or endpoint than the main LLM through `.env`, so that I can use a cheaper or
faster model for the probe step without touching `bot.yaml`.

#### Acceptance Criteria

1. THE Attention_Probe SHALL read its configuration exclusively from the
   environment variables `ATTENTION_PROBE_API_KEY`,
   `ATTENTION_PROBE_BASE_URL`, and `ATTENTION_PROBE_MODEL`.
2. WHEN `ATTENTION_PROBE_API_KEY` is unset or empty, THE Attention_Probe
   SHALL fall back to the value of `OPENAI_API_KEY`.
3. WHEN `ATTENTION_PROBE_BASE_URL` is unset or empty, THE Attention_Probe
   SHALL fall back to the value of `OPENAI_BASE_URL`.
4. WHEN `ATTENTION_PROBE_MODEL` is unset or empty, THE Attention_Probe
   SHALL fall back to the `model` field of the default AgentDef resolved at
   bootstrap.
5. WHERE both `OPENAI_BASE_URL` and `ATTENTION_PROBE_BASE_URL` are unset, THE
   Attention_Probe SHALL use the same default base URL as the main LLM
   (`https://api.openai.com/v1`).
6. THE Attention_Probe SHALL NOT read configuration from `bot.yaml` other than
   the single boolean toggle defined in Requirement 1.

### Requirement 3: Auto-Skip on Missing Credentials

**User Story:** As an operator, I want the probe to silently disable itself
when no API key is available, so that I do not have to coordinate
`bot.yaml` and `.env` to avoid startup errors.

#### Acceptance Criteria

1. IF `agent.group_batch_attention_probe_enabled=true` AND both
   `ATTENTION_PROBE_API_KEY` and `OPENAI_API_KEY` resolve to empty strings at
   startup, THEN THE GroupBatchChatDispatcher SHALL treat the probe as
   disabled for the entire process lifetime and SHALL NOT invoke the
   Attention_Probe for any batch.
2. WHEN the auto-skip condition in acceptance criterion 1 is reached at
   startup, THE bootstrap SHALL emit exactly one structlog `info`-level log
   record indicating that the probe is disabled because no API key was
   resolved.
3. WHEN credentials are missing per acceptance criterion 1, THE bootstrap
   SHALL NOT raise an exception, abort startup, or block the existing
   dispatcher path.

### Requirement 4: Provider Reuse

**User Story:** As a maintainer, I want the probe to reuse the existing OpenAI
provider class, so that we do not maintain two parallel HTTP client paths to
the same vendor.

#### Acceptance Criteria

1. THE Attention_Probe SHALL invoke the LLM through an instance of
   `OpenAIProvider` constructed with `api_key`, `base_url`, and `model`
   resolved per Requirement 2.
2. THE Attention_Probe SHALL NOT introduce a new `LLMProvider` implementation
   class.
3. THE Attention_Probe SHALL hold its own `OpenAIProvider` instance separate
   from the main agent's provider so that the two clients can target
   different models, base URLs, or keys without interference.
4. THE Attention_Probe SHALL NOT include conversation history in the request
   sent to its `OpenAIProvider`; only a system prompt and a user prompt
   containing the buffered batch text are sent.

### Requirement 5: Trigger Conditions

**User Story:** As a maintainer, I want the probe to run only when the
existing rule-based attention check has not fired, so that we never spend a
second LLM call when the first stage already decided.

#### Acceptance Criteria

1. WHEN a buffered batch reaches a flush trigger (the `window_s` boundary,
   the `max_hold_s` boundary, the `max_messages` cap, or the `max_chars`
   cap), THE GroupBatchChatDispatcher SHALL evaluate the Rule_Based_Attention
   _Detector for every buffered message before considering the
   Attention_Probe.
2. WHEN at least one buffered message has been flagged by the
   Rule_Based_Attention_Detector for the current batch, THE
   GroupBatchChatDispatcher SHALL NOT invoke the Attention_Probe for that
   batch and SHALL proceed to the Main_LLM_Selector.
3. WHEN `require_attention=false` for the current `GroupBatchConfig`, THE
   GroupBatchChatDispatcher SHALL NOT invoke the Attention_Probe for any
   batch, because the existing gate is already disabled.
4. WHEN Probe_Configured is false (per Requirement 1 and Requirement 3), THE
   GroupBatchChatDispatcher SHALL NOT invoke the Attention_Probe for any
   batch.
5. WHEN all of the following hold for a batch — Probe_Configured is true,
   `require_attention=true`, no message in the batch has been flagged by
   the Rule_Based_Attention_Detector, the `window_s` boundary has been
   reached, and Probe_State has not recorded a prior probe call for this
   Batch_Lifecycle — THE GroupBatchChatDispatcher SHALL invoke the
   Attention_Probe exactly once.

### Requirement 6: One Probe Call per Batch Lifecycle

**User Story:** As an operator, I want the probe to run at most once per
batch, so that I can predict its cost as a fixed multiple of batch volume.

#### Acceptance Criteria

1. THE Attention_Probe SHALL be invoked at most once per Batch_Lifecycle,
   regardless of how many flush iterations occur within that lifecycle.
2. WHEN `_reset_state_locked` is called for a `_GroupState`, THE Probe_State
   SHALL be cleared so that the next batch starts a fresh lifecycle eligible
   for one probe call.
3. WHEN the Attention_Probe is invoked for a batch, THE Probe_State SHALL be
   updated before the next flush-loop iteration so that subsequent
   iterations within the same Batch_Lifecycle do not re-invoke the probe.
4. WHEN any subsequent flush-loop iteration within the same Batch_Lifecycle
   re-evaluates the probe-trigger conditions in Requirement 5, THE
   GroupBatchChatDispatcher SHALL ignore the trigger (no probe call, no
   queuing for a later iteration) because Probe_State already records that
   the probe ran for this Batch_Lifecycle.

### Requirement 7: Probe Verdict Yes Routes to Main LLM

**User Story:** As an operator, I want a positive probe verdict to drive the
existing main-LLM selective-replier flow, so that the probe is purely
additive and does not introduce a new reply path.

#### Acceptance Criteria

1. WHEN the Attention_Probe returns Probe_Verdict=true for a batch, THE
   GroupBatchChatDispatcher SHALL set `state.attention_seen=true` for the
   current batch and SHALL allow the same flush iteration to proceed to the
   Main_LLM_Selector under the existing flush rules.
2. WHEN Probe_Verdict=true causes the batch to be dispatched to the
   Main_LLM_Selector, THE Main_LLM_Selector SHALL be invoked exactly once
   for that batch, identical to the case where the
   Rule_Based_Attention_Detector had originally flagged the batch.
3. THE Attention_Probe SHALL NOT itself send any reply to the group; only
   the Main_LLM_Selector emits user-visible replies.

### Requirement 8: Probe Verdict No Preserves Drop Behaviour

**User Story:** As an operator, I want a negative probe verdict to keep the
batch silent, so that the cost-control semantic of the existing attention
gate is preserved.

#### Acceptance Criteria

1. WHEN the Attention_Probe returns Probe_Verdict=false for a batch, THE
   GroupBatchChatDispatcher SHALL leave `state.attention_seen` unchanged
   (i.e., still false) for the current batch.
2. WHEN Probe_Verdict=false and no further rule-based attention is observed
   before `max_hold_s`, THE GroupBatchChatDispatcher SHALL drop the batch
   under the existing drop path with no Main_LLM_Selector invocation.
3. WHEN Probe_Verdict=false, THE Main_LLM_Selector SHALL NOT be invoked for
   that batch.

### Requirement 9: Probe Failure Fail-Closed

**User Story:** As an operator, I want probe failures to never crash the bot
or cause unintended LLM spend, so that an upstream outage degrades the
feature gracefully rather than escalating cost or downtime.

#### Acceptance Criteria

1. IF the Attention_Probe raises an exception during the HTTP call (network
   error, timeout, HTTP 5xx, HTTP 4xx, JSON decode error), THEN THE
   GroupBatchChatDispatcher SHALL treat the outcome as Probe_Verdict=false
   and SHALL apply Requirement 8.
2. IF the Attention_Probe returns a response whose content cannot be
   interpreted as either yes or no per Requirement 13, THEN THE
   GroupBatchChatDispatcher SHALL treat the outcome as Probe_Verdict=false
   and SHALL apply Requirement 8.
3. IF the Attention_Probe fails per acceptance criterion 1 or 2, THEN THE
   GroupBatchChatDispatcher SHALL emit one structlog `warning`-level log
   record with the failure category and SHALL NOT propagate the exception
   out of the flush loop.
4. IF the Attention_Probe failure was caused by a 401 or 403 authentication
   error, THEN THE GroupBatchChatDispatcher SHALL still attempt the probe
   for subsequent batches (the failure is treated as transient, not as a
   permanent disable).

### Requirement 10: Empty Batch Short-Circuit

**User Story:** As a maintainer, I want the probe to skip the API call when
there is nothing to probe, so that we do not pay for trivially negative
verdicts.

#### Acceptance Criteria

1. WHEN the buffered batch contains zero messages at the moment the probe
   would be invoked, THE GroupBatchChatDispatcher SHALL skip the
   Attention_Probe for that batch and SHALL NOT call `OpenAIProvider.chat`.
2. WHEN the buffered batch contains messages but every message resolves to
   empty text after trimming, THE GroupBatchChatDispatcher SHALL skip the
   Attention_Probe for that batch and SHALL NOT call `OpenAIProvider.chat`.
3. WHEN the Attention_Probe is skipped per acceptance criterion 1 or 2, THE
   Probe_State SHALL still be marked as probed so that the same batch is
   not re-evaluated within the same Batch_Lifecycle.

### Requirement 11: Non-Blocking Latency

**User Story:** As an operator, I want the probe to never block message
ingestion, so that a slow probe endpoint does not stall the inbound event
loop.

#### Acceptance Criteria

1. THE Attention_Probe SHALL be invoked from inside `_flush_loop` and SHALL
   NOT be invoked from `GroupBatchChatDispatcher.run` or any other code
   path on the dispatcher.run() / event-ingest path.
2. WHILE the Attention_Probe HTTP call is in progress, THE
   GroupBatchChatDispatcher.run SHALL continue to accept new messages and
   append them to the buffer under the existing locking rules.
3. THE Attention_Probe HTTP call SHALL be subject to the timeout cap defined
   in Requirement 13 so that a hung endpoint cannot stall the flush loop
   indefinitely.

### Requirement 12: Clean Shutdown and State Reset

**User Story:** As an operator, I want probe state and HTTP resources to be
released on shutdown and history clears, so that the bot has no lingering
sockets or stale flags after a `/reset`.

#### Acceptance Criteria

1. WHEN `GroupBatchChatDispatcher.stop` is called, THE Attention_Probe SHALL
   close its underlying `OpenAIProvider` httpx client by awaiting
   `OpenAIProvider.aclose`.
2. WHEN `GroupBatchChatDispatcher.clear_history` is called for any scope,
   THE Probe_State for that scope's `_GroupState` SHALL be cleared as part
   of the existing `_reset_state_locked` invocation.
3. WHEN `_reset_state_locked` is called for any reason (flush, drop, stop,
   clear_history), THE Probe_State SHALL be cleared atomically with the
   other batch fields under the existing `_GroupState.lock`.
4. WHEN `GroupBatchChatDispatcher.stop` is called, THE Attention_Probe SHALL
   abort or await any in-flight probe HTTP call before returning so that
   shutdown does not leak a pending task.

### Requirement 13: Cost and Latency Caps

**User Story:** As an operator, I want every probe call to be cheap and bounded,
so that I cannot accidentally bankrupt the bot by enabling the feature.

#### Acceptance Criteria

1. THE Attention_Probe SHALL request `max_tokens` of 32 or fewer for every
   call.
2. THE Attention_Probe SHALL configure its `OpenAIProvider` with a request
   timeout of 10.0 seconds or fewer.
3. THE Attention_Probe SHALL pass `temperature=0.0` to `OpenAIProvider.chat`
   for every call.
4. THE Attention_Probe SHALL parse the response content with a case-
   insensitive match against a fixed set of yes/no tokens defined in the
   design phase, and SHALL return Probe_Verdict=true only when a yes token
   is matched.
5. THE Attention_Probe SHALL NOT pass any `tools` schema to
   `OpenAIProvider.chat` (the probe is a pure text-completion call).

### Requirement 14: Logging

**User Story:** As an operator, I want clear logs about probe behaviour, so
that I can troubleshoot routing decisions and verify the feature is doing
what I expect.

#### Acceptance Criteria

1. WHEN the bot finishes bootstrap, THE bootstrap SHALL emit exactly one
   structlog `info`-level log record indicating whether the
   Attention_Probe is enabled, including the resolved model and base URL
   when enabled and the reason when disabled (config off, no API key).
2. WHEN the Attention_Probe is invoked for a batch, THE
   GroupBatchChatDispatcher SHALL emit one structlog `debug`-level log
   record per invocation containing the scope id, batch size, and the
   resulting Probe_Verdict.
3. WHEN the Attention_Probe fails per Requirement 9, THE
   GroupBatchChatDispatcher SHALL emit one structlog `warning`-level log
   record with the scope id and the failure category.
4. THE Attention_Probe SHALL NOT log the full content of buffered messages
   at any level above `debug`.

### Requirement 15: Backward Compatibility

**User Story:** As a maintainer, I want existing tests and existing
deployments to keep working without changes, so that the rollout is risk-free.

#### Acceptance Criteria

1. THE existing tests in `packages/agent/tests/test_group_batch.py` SHALL
   pass without modification after this feature is implemented.
2. THE `GroupBatchChatDispatcher.__init__` signature SHALL accept the
   Attention_Probe as an optional injected dependency (default value
   indicating "no probe"), so that existing tests that omit the parameter
   continue to construct the dispatcher successfully.
3. THE new `GroupBatchConfig.attention_probe_enabled` field SHALL default to
   `false`, so that existing tests that instantiate `GroupBatchConfig`
   without the field continue to construct successfully and observe
   today's behaviour.
4. WHEN an operator's `bot.yaml` does not set
   `agent.group_batch_attention_probe_enabled` AND `.env` does not set any
   of `ATTENTION_PROBE_API_KEY`, `ATTENTION_PROBE_BASE_URL`, or
   `ATTENTION_PROBE_MODEL`, THE GroupBatchChatDispatcher SHALL behave
   identically to the current implementation in all observable respects
   (replies emitted, log records emitted at `info` level or above, drop
   timing).

### Requirement 16: Operator Documentation

**User Story:** As an operator, I want the new env vars and the new YAML
toggle to be documented in the standard places, so that I can discover them
without reading source code.

#### Acceptance Criteria

1. THE `.env.example` file SHALL document the three new env vars
   `ATTENTION_PROBE_API_KEY`, `ATTENTION_PROBE_BASE_URL`, and
   `ATTENTION_PROBE_MODEL` under a clearly labelled section, with comments
   that state the fallback chain to `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
   and the default agent's model.
2. THE `bot/bot.yaml` file SHALL include
   `group_batch_attention_probe_enabled` under the existing
   `agent.group_batch_*` block, with a comment that explains the toggle and
   its interaction with the env vars.

### Requirement 17: Correctness Properties

**User Story:** As a maintainer, I want the probe's invariants to be enforced
by automated property tests, so that future refactors cannot silently
regress the cost-control or routing semantics.

#### Acceptance Criteria

1. FOR ALL batches where at least one message is flagged by the
   Rule_Based_Attention_Detector, THE Attention_Probe SHALL NOT be invoked
   for that batch.
2. FOR ALL batches where Probe_Configured is false (probe disabled by
   config or by missing credentials), THE Attention_Probe SHALL NOT be
   invoked for that batch.
3. FOR ALL batches where the Attention_Probe returns Probe_Verdict=false,
   THE Main_LLM_Selector SHALL NOT be invoked for that batch.
4. FOR ALL batches where the Attention_Probe returns Probe_Verdict=true,
   THE Main_LLM_Selector SHALL be invoked exactly once for that batch with
   the same buffered messages that were probed.
5. FOR ALL Batch_Lifecycles, THE Attention_Probe SHALL be invoked at most
   once (idempotency within a batch).
6. FOR ALL batches where the Attention_Probe HTTP call raises an exception
   or returns malformed content, THE Main_LLM_Selector SHALL NOT be
   invoked for that batch.
