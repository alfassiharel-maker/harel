# 10 — Unit Economics, AI Cost Model and Risks

**Status:** awaiting review · **Purpose:** make the 15 ₪ price point a decision
backed by arithmetic

This document exists because the single largest threat to the business model is
not technical. It is that **AI inference cost per subscriber can exceed the gross
margin at 15 ₪/month** if the system is built naively. Everything in
`05-ai-architecture.md` §4 and §8 is designed around the numbers below.

---

## 1. What 15 ₪ actually yields

| Step | Amount (₪) | Note |
|---|---|---|
| List price | 15.00 | |
| Less Israeli VAT at 18% | 12.71 | Store prices are VAT-inclusive |
| Less store commission at 30% | **8.90** | Standard Apple/Google rate |
| Less store commission at 15% | **10.80** | Small Business Program / after year one |

At roughly 3.7 ₪/USD:

| Scenario | Net per subscriber / month |
|---|---|
| 30% commission | **≈ $2.41** |
| 15% commission | **≈ $2.92** |
| Web / PayPal (~3% fees) | **≈ $3.33** |

**Budget rule adopted here: AI inference ≤ 20% of net revenue**, leaving room for
infrastructure, payment costs, support and margin.

| Scenario | AI budget / subscriber / month |
|---|---|
| 30% commission | **$0.48** |
| 15% commission | **$0.58** |
| Web | **$0.67** |

Enrolling in Apple's Small Business Program and Google's equivalent is worth
roughly **$0.50/subscriber/month** — a larger effect than most engineering
optimisations available to us, and it is a form to fill in.

---

## 2. Model prices (Anthropic, as of 2026-06)

| Model | Input /M | Output /M | Context |
|---|---|---|---|
| `claude-opus-5` | $5.00 | $25.00 | 1M |
| `claude-sonnet-5` | $3.00 ($2.00 intro to 2026-08-31) | $15.00 ($10.00 intro) | 1M |
| `claude-haiku-4-5` | $1.00 | $5.00 | 200K |

Modifiers: cache read ≈ **0.1×** input · cache write ≈ **1.25×** (5-min TTL) or
**2×** (1-hour) · **Batch API −50%**. Minimum cacheable prefix: 512 tokens on
Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5.

---

## 3. Cost of one coach message

Assumed shape, from the context packet in `05-ai-architecture.md` §2:

| Segment | Tokens | Cacheable |
|---|---|---|
| System prompt + tool definitions | 2,500 | yes (weeks) |
| Athlete context packet | 3,000 | yes (a day) |
| Conversation history | 1,000 | no |
| The question | 50 | no |
| Output incl. adaptive thinking | 900 | — |

### On `claude-opus-5`, with caching working

| Component | Calculation | Cost |
|---|---|---|
| Cache read (5,500 tok) | 5,500 × $0.50/M | $0.00275 |
| Fresh input (1,050 tok) | 1,050 × $5/M | $0.00525 |
| Output (900 tok) | 900 × $25/M | $0.02250 |
| **Per message** | | **≈ $0.0305** |

**Output dominates at 74% of the cost.** That single fact determines the
optimisation strategy: reduce *output* tokens and reduce the *number of model
calls*. Shrinking the prompt further is close to pointless once caching works.

### Monthly cost per active subscriber

| Messages / month | Naive (Opus 5, all messages) | With the §4 controls |
|---|---|---|
| 10 | $0.31 | $0.11 |
| 30 | $0.92 | **$0.33** |
| 60 | $1.83 | $0.63 |
| 100 (Premium cap) | $3.05 | $1.02 |

Naive at 30 messages is **$0.92 — roughly 38% of net revenue at 30% commission**,
nearly double the budget. With the controls applied, 30 messages costs **$0.33**,
inside the $0.48 budget. This is the whole argument for the routing and caching
design, expressed in dollars.

---

## 4. The controls, with measured effect

| # | Control | Effect |
|---|---|---|
| 1 | **Deterministic routing.** ~40% of expected questions ("what is my readiness", "how much did I train") are rendered from `daily_metrics` through templates. | −40% of calls, and a *better* answer: identical to the chart rather than a paraphrase of it |
| 2 | **Effort tuning.** Routine chat at low/medium `effort`, deep review at high. Thinking tokens bill as output. | −30–50% output tokens on routine turns |
| 3 | **Prompt caching**, 1-hour TTL on system + packet | −85% on the repeated prefix |
| 4 | **Batch API for the weekly review** (overnight, latency-insensitive) | −50% on that call |
| 5 | **Tier quotas** — Free 5/month, Premium 100/month | hard ceiling on worst case |
| 6 | **Response caching** for identical (question, packet-hash) pairs | small but free |
| 7 | **Haiku 4.5 for classification and routing** | routing costs ~$0.0001 |

Applying 1–4 to the 30-message case: 18 model calls at ~$0.018 → **$0.33/month**.

**Worst case is bounded.** A Premium subscriber exhausting 100 messages costs
$1.02 — above the $0.48 budget but below the $2.41 net revenue, so it is a margin
compression, never a loss. The quota is what makes that guarantee hold.

---

## 5. Model tier — a business decision, with the numbers

The default here is `claude-opus-5`, the most capable model, because coach answer
quality *is* the product. Two alternatives exist, and this is the trade-off to
decide consciously rather than by drift:

| Route strategy | Cost / 30 msgs | Consideration |
|---|---|---|
| Opus 5 everywhere (**default**) | $0.33 | Best quality; inside budget with controls |
| Sonnet 5 for chat, Opus 5 for weekly review | ~$0.20 | ~40% cheaper; needs eval comparison before adopting |
| Sonnet 5 everywhere | ~$0.17 | Cheapest viable; quality delta must be measured, not assumed |

**Recommendation:** launch on Opus 5, instrument cost per message from day one, and
run the eval suite head-to-head against Sonnet 5 on the golden set. Move only if
the measured quality difference does not justify the measured cost difference. The
routing table is configuration, so this is a config change and not a deploy.

---

## 6. Infrastructure cost

| Stage | Athletes | Monthly | Per athlete |
|---|---|---|---|
| 0 | <1k | $150–250 | $0.25 |
| 1 | 1k–25k | $600–1,200 | $0.05 |
| 2 | 25k–150k | $3,000–6,000 | $0.04 |
| 3 | 150k–500k | $12,000–25,000 | $0.05 |

Managed Postgres with PITR, Redis, containers, object storage, CDN, monitoring.
Per-athlete infrastructure cost **falls** with scale, unlike AI cost which is
strictly linear — so AI is the number to watch, and it is why cost per message is
recorded on every row.

### Fully loaded, at 30% commission

| Line | Per subscriber / month |
|---|---|
| Net revenue | $2.41 |
| AI inference | −$0.33 |
| Infrastructure | −$0.05 |
| Payment/support overhead (est.) | −$0.15 |
| **Contribution margin** | **≈ $1.88 (78%)** |

Healthy — but only because the AI cost is engineered, not incidental. Naive
implementation turns that $1.88 into $1.29 and a much worse worst case.

---

## 7. Other revenue lines

The subscription is unlikely to be the whole business at 15 ₪.

| Line | Mechanism | Phase |
|---|---|---|
| Partner commissions | basis-point rate per attributed conversion | 4 |
| Sponsored challenges | fixed fee per campaign | 4 |
| Coach marketplace | commission on plan sales | 5 |
| Individual sensor unlocks | one-off purchases for Free users | 2 |
| Premium tier (30–50 ₪) | higher AI quota, deeper analytics | 3 |

Reward payouts are a **cost of acquisition**, not revenue. They are funded from
partner commissions, and the ledger's `house` account is what makes the funding
rate visible rather than assumed.

---

## 8. Risk register

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R1 | AI cost exceeds margin | **H** | **H** | §4 controls; per-message cost recorded; alert on anomaly; quotas bound the worst case |
| R2 | Garmin approval delayed or terms change | M | **H** | Apply week 1; provider adapter interface means a second provider is an adapter, not a rewrite |
| R3 | Store rejection over health data | M | M | Privacy manifests and disclosures as explicit tasks; assume one rejection round |
| R4 | Injury model never validates | M | M | Already labelled unvalidated; ship the heuristic honestly; do not ship a model that is not better |
| R5 | Reward economics gamed | M | **H** | Trust scoring before earning, holds, human payout approval, ledger immutability |
| R6 | Points-to-cash legally blocked in Israel | M | M | Partner-credit-only path ships independently; counsel before Phase 4.8 |
| R7 | Health-data breach | L | **Critical** | Dual-layer isolation; envelope-encrypted tokens; audit log; external pen test before launch |
| R8 | 15 ₪ is below willingness-to-pay floor | M | M | Instrument conversion; the price is a config value, not a schema value |
| R9 | Coach quality insufficient in Hebrew | M | **H** | Hebrew golden cases; native reviewer sign-off before launch |
| R10 | Single-AI-provider outage | M | M | Provider-agnostic client from day one |
| R11 | Amendment 13 obligations (DPO, registration) | M | M | Counsel; flagged in `01` §14 |

---

## 9. Metrics to instrument from day one

Not for a dashboard — these are the numbers that decide whether the model works.

| Metric | Target | Why |
|---|---|---|
| AI cost per active subscriber / month | < $0.48 | The binding constraint |
| Cache hit rate on the packet prefix | > 80% | Zero means a silent invalidator broke the cost model |
| Share of questions served deterministically | > 35% | The largest single cost lever |
| Free → Premium conversion | > 5% | |
| D30 retention | > 40% | |
| Coach messages per active athlete / week | 3–8 | Below: not valuable. Above: cost risk |
| Thumbs-up rate on coach answers | > 80% | |
| Ungrounded-number rate | **0** | Any non-zero value is a correctness incident |
| Athletes with measurable improvement at 90 days | > 50% | The claim the product is built on |
