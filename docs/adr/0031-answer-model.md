# ADR 0031: Answer Model and Provider

**Status:** Accepted (decisions approved 2026-10-26). No code, accounts or keys were created for this ADR. The test step (below) produces the evidence that can still change the demo choice.

**Date:** 2026-10-26

**Deciders:** Hamza Asif

## Context

Retrieval (ADR 0030) finds the chunks. A language model must now write the answer from them, with citations, or refuse. SPEC 6.2 and 7 set the targets: an answer in at most 8 s at p95, a cost of at most $0.002 per answer "on free/cheap models", and one interface so that providers can be swapped.

This is the first place where a tenant's document text leaves our system. Every prompt carries the question and about five chunks of a tenant's documents to a third party. That makes the provider's terms a security decision, not only a cost decision.

Sizing used for all figures: about 1,100 input tokens (question plus five chunks) and about 200 output tokens per answer. All prices and terms were read on official pages on 2026-10-26 (sources at the end). Where a page was silent, this ADR says so.

## Decisions

### 1. The demo tenant uses Groq `openai/gpt-oss-120b` on the free tier, with fictional data only

- Why: it is free, fast (Groq lists about 500 tokens per second), and supports strict JSON-schema output. The demo corpus is fictional, so whatever the free tier does with the text does not matter.
- **Reasoning effort is set to `low`**, and real token use is measured (decision 6). gpt-oss is a reasoning model, and the page does not say whether reasoning tokens appear in the usage figures. Groq's free limits for this model (from its rate-limit page, which does not name the plan) are 30 requests per minute, 1,000 per day, 8,000 tokens per minute and 200,000 tokens per day. At about 1,300 tokens per answer that is roughly 150 answers per day. Reasoning tokens that count against the daily limit would shrink it, and repeated evaluation runs would hit it.
- **Groq is demo-only.** Its data page says inference requests are not retained by default and that zero data retention (ZDR) can be enabled, but it does not say whether customer data is used for training. The Data Processing Addendum was not read. Groq stays demo-only until that text confirms no training for the paid tier.

### 2. Candidates for the test step

| Candidate | Role | Why |
|---|---|---|
| Groq `openai/gpt-oss-120b`, free | Demo choice (decision 1) | Zero cost, fast, strict schema |
| Gemini 2.5 Flash-Lite, free | Second zero-cost candidate, fictional data only | $0 on the free tier; paid price is $0.10 / $0.40 per million tokens |
| Claude Haiku 5.5, paid | Paid, no-training reference | $0.10 / $0.50 per million tokens; no training by default; deleted within 30 days; ZDR by agreement; JSON-schema output supported |

Estimated cost per answer: gpt-oss-120b paid $0.00029, Gemini 2.5 Flash-Lite $0.00019, Haiku 5.5 $0.00021. All are under the $0.002 target. Claude Sonnet 5.5 ($0.0042) is over it and is not a candidate.

**Gemini's free tier is for fictional data only.** Google's terms say the free tier's prompts and responses are used to improve its products, that human reviewers may read them, and that confidential or personal data must not be submitted. They also require the paid tier for users in the EEA, Switzerland and the UK.

**Not evaluated** (no usable official evidence found, so they are not candidates): Mistral (the official pages I reached gave no API prices and no terms for its API free plan; its help centre says free-mode data may be used for training, with an opt-out) and Cerebras (no permanent free tier, only $5 of credits for 30 days after adding a payment method, 5 requests per minute; its privacy policy says it does not retain inputs and outputs but does not address training). OpenAI `gpt-5-nano` ($0.05 / $0.40) is a possible later swap if a paid alternative to Haiku is wanted. It was not chosen because the budget is one paid reference.

### 3. Requirement for real client data (all five must hold)

A provider may receive a real client's text only if:

1. **No training.** A contractual statement, for the exact tier in use, that submitted data is not used to train models.
2. **Short retention.** Retention of 30 days or less, or zero data retention available and enabled.
3. **A signed Data Processing Addendum and a stated processing region.**
4. **Commercial use** is allowed on that tier.
5. **The client has approved the provider** as a subprocessor, per tenant, before any text is sent. A tenant with no approved provider cannot call `/ask`.

Free tiers never qualify. Of what was checked: Anthropic (no training by default, deleted within 30 days, ZDR by agreement), OpenAI (no training by default, abuse logs up to 30 days, ZDR on approval, US or EU processing) and paid Gemini (no training; logged for a limited period; may be cached in any country where Google operates) state points 1 and 2 on their official pages. The remaining points (the signed DPA and the region) still need checking before a first real client.

**Recorded as a future option, not v1: a local-only mode.** The only way for text never to leave is a model on our own host. A candidate is Qwen3-4B-Instruct-2507 (Apache-2.0, 4.0 billion parameters, non-thinking only). It was not measured. It would need several GB of memory against the embedder's roughly 1.1 GB budget, and CPU generation speed is unknown. It needs its own measurement before it can be offered.

### 4. Provider-neutral interface

One protocol, `AnswerModel`, with one method, `answer(request) -> result`, in its own package. Adapters (Groq, Gemini, Anthropic) implement it. The rest of the application only sees the protocol.

- **Request:** the question, the retrieved chunks (an id and the text for each), the answer schema, the instructions, and limits (timeout, maximum output tokens).
- **Result:** the parsed structured answer, the token counts (input, output, and reasoning where reported), the latency, the provider and the model name.
- **Errors:** typed errors for timeout, rate limit, provider outage, refusal, and output that does not match the schema.
- **Outside the adapters:** retries with backoff, citation validation, the refusal rule (ADR in Step 4) and cost accounting. An adapter only translates one call.
- **Per-tenant configuration:** which provider a tenant uses, and whether external providers are allowed at all (decision 3, point 5).

### 5. A tested rule: a prompt contains only the question and chunk text

The prompt builder takes only the question, the chunk text and the fixed instructions. It never receives a user id, an email, a tenant id or a document id that identifies a person or tenant. A test builds prompts from inputs seeded with such values and asserts that none of them appears in the request sent to the adapter. Chunk identifiers inside the prompt are position numbers local to that request (for example `[1]` to `[5]`), mapped back to chunks by our code after the answer returns.

### 6. Per-answer metering for the cost cap

Every call result carries: input tokens, output tokens, reasoning tokens where the provider reports them, the model, the latency, and the computed cost from a price table kept in code and checked against the provider's page. In the test step (3b) these are written to the evaluation report only; there is no database table yet. The per-tenant metering table (with row-level security, grants and the catalog guard) is built in Step 5 with `POST /ask`, which uses its totals for a per-tenant cost cap. The first measurement in the test step is how many tokens gpt-oss-120b spends on reasoning at effort `low`.

### 7. Keys

API keys are held as `SecretStr` in `Settings`, read from the git-ignored `backend/.env` only, never committed and never logged. A test proves they cannot appear in logs, in `repr`, or in error messages (the test step spells this out).

## Alternatives considered

- **One provider for everything, paid from the start (Haiku only).** Simple and defensible for real data, but it makes the demo cost money and removes the zero-cost path SPEC 7.5 asks for.
- **Gemini free for the demo.** Explicit about using data for training and human review. It works for fictional data, but Groq is faster and its terms for the paid tier can be confirmed, so Gemini is the second candidate, not the first.
- **A local model for v1.** Text never leaves, but CPU speed and memory are unmeasured and are unlikely to fit the 8 s target on the first hosting. Deferred.
- **Mistral or Cerebras.** Not evaluable on official evidence today.

## Consequences

- **Positive:** the demo costs nothing, the paid reference sets a quality bar, and the interface keeps the choice reversible.
- **Negative:** the free tiers have small daily limits, and reasoning models may spend tokens invisibly. Evaluation runs must be sized to fit, or split across days.
- **Negative:** Groq's training terms are unconfirmed, so it can never serve a real client until they are.
- **Operational:** three providers means three keys and three sets of terms to keep current. The terms were read once, on 2026-10-26, and must be re-read before any real client, because they change.
- **Security:** document text leaves our system on every answer. SECURITY records the provider as a subprocessor and the controls above.

## Open questions (answered by the test step)

- How many reasoning tokens does gpt-oss-120b spend at effort `low`, and do they appear in the usage figures?
- Does Gemini 2.5 Flash-Lite support JSON-schema output? The structured-output page does not mention this model. Its free-tier limits are only shown in AI Studio.
- Real latency of each candidate, against the 8 s p95 target. Only Groq publishes a speed figure.
- Groq's paid-tier DPA text.

## Sources (official pages, read 2026-10-26)

- Groq: [your data](https://console.groq.com/docs/your-data), [rate limits](https://console.groq.com/docs/rate-limits), [models and prices](https://console.groq.com/docs/models), [structured outputs](https://console.groq.com/docs/structured-outputs), [reasoning](https://console.groq.com/docs/reasoning)
- Google: [Gemini API terms](https://ai.google.dev/gemini-api/terms), [pricing](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- Anthropic: [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [data retention and ZDR](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention), [retention policy](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), [training policy](https://privacy.claude.com/en/articles/7996868-is-my-data-used-for-model-training), [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- OpenAI: [data controls](https://developers.openai.com/api/docs/guides/your-data), [pricing](https://developers.openai.com/api/docs/pricing)
- Cerebras: [rate limits](https://inference-docs.cerebras.ai/support/rate-limits), [privacy policy](https://www.cerebras.ai/privacy-policy)
- Mistral: [Studio data help page](https://help.mistral.ai/en/articles/347617-do-you-use-my-user-data-to-train-your-models)
- Qwen: [Qwen3-4B-Instruct-2507 model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)

## Related decisions

- ADR 0030 (retrieval, which supplies the chunks)
- SPEC 6.2, 7 (answer targets), ARCHITECTURE (the answer flow, Step 4)
- SECURITY (secrets, subprocessors)
