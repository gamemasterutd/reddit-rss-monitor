# Filtering Guide

Filtering is data-driven. You can create a new profile without changing Python source.

## Profile location

The default profile is `filters/finance.json`. Point `filter_profile` in the app config to another JSON file such as:

- `filters/technology.json`
- `filters/health.json`
- `filters/marketing.json`

## Model

A post is evaluated from its RSS title plus RSS-provided description/body. Ranking position affects the separate engagement label; ranking does not bypass hard exclusions or turn unrelated content into a relevant finance post.

A profile defines:

```json
{
  "weights": {
    "strong_phrase": 5.0,
    "supporting_phrase": 2.0,
    "decision_intent": 1.5,
    "personal_context": 1.0,
    "noise_penalty": 3.0
  },
  "thresholds": {
    "relevant": 5.5,
    "needs_review": 3.0
  },
  "decision_intent_phrases": [],
  "personal_context_phrases": [],
  "noise_phrases": [],
  "hard_exclusion_phrases": [],
  "review_triggers": [],
  "categories": []
}
```

## Categories

Each category has a unique `name` and may define `strong_phrases` and `supporting_phrases`.

Strong phrases represent high-precision topic evidence. Supporting phrases usually need decision intent, personal context, or multiple matches. The highest-scoring category becomes the post topic.

## Decision intent

Examples include `should I`, `switch`, `review`, `prepay`, `worth it`, `confused`, `continue or`, and `sell or`.

Decision intent adds weight only after a configured topic phrase matches. A generic post saying only `help` cannot become relevant by itself.

## Personal context

Phrases such as `my portfolio`, `my salary`, `my loan`, `my family`, or `my goals` add modest weight. They help distinguish real financial decisions from definitions/news.

## Noise penalties

Operational-only issues such as KYC processing, account activation, payment failures, mandate problems, login problems, and customer-service issues subtract score. A broader financial decision can still pass if it has enough independent evidence.

## Hard exclusions

Hard exclusions immediately classify a post as excluded. The default profile uses them for obvious referral/promotion, trading-signal, guaranteed-return, and similar spam patterns.

## Needs Review

Review triggers route sufficiently relevant posts to `Needs Review`. Defaults include:

- sensitive personal situations
- current tax/regulatory questions
- legal allegations / serious fraud claims

These posts do not enter the Relevant-only CSV/JSONL automatically.

## Word boundaries

The matcher is token-aware. `RIA` matches `an RIA` but not `criteria`; `FD` matches `an FD` but not `refund`.

## Audit output

`Reddit-All-Posts-Latest.csv` is the safest way to tune a profile. It shows every post actually evaluated that day, including:

- classification
- classification/exclusion reason
- internal score
- matched terms
- ranking evidence
- feeds where observed

Use that file to identify false negatives before lowering global thresholds.

## Safe tuning workflow

Prefer adding a missing high-precision phrase to the right category rather than broadly lowering thresholds. For every material filter change, add both a positive and a negative local test fixture.
