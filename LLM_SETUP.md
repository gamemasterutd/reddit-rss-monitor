# Add your own AI review

AI is optional. The collector and monthly source workbook work without an AI key while the upstream feed is available. You can attach the workbook to any model manually and use [MANUAL_CHATGPT_REVIEW.md](MANUAL_CHATGPT_REVIEW.md).

The local AI connector is experimental. It includes Gemini, Groq and OpenRouter request code, but release tests mock those services. Provider/model combinations have not been tested live for this public release. Pick a model that supports the connector's JSON-schema output; an arbitrary model ID may fail. Consult the provider's current documentation before enabling automatic runs.

## 1. Get a key from your chosen provider

Create your own key in your provider account. Check its pricing, available models, limits and handling of input data there. A free tier does not guarantee capacity or permanently free usage.

- Gemini: [structured output documentation](https://ai.google.dev/gemini-api/docs/structured-output)
- Groq: [structured output documentation](https://console.groq.com/docs/structured-outputs)
- OpenRouter: [structured output documentation](https://openrouter.ai/docs/guides/features/structured-outputs)

This connector restricts OpenRouter to an explicit model ending in `:free`, with provider fallback disabled. It does not support a paid OpenRouter model without a code change. Gemini and Groq billing depends on your account. A local request cap is not a dollar budget.

## 2. Save the key privately

Use a text editor to save just the key, on one line, in a file outside the repository, such as `~/Library/Application Support/RedditRSSMonitor/ai_api_key`. Do not paste it into a public issue, a prompt example or the example configuration.

Then run:

```bash
chmod 600 "$HOME/Library/Application Support/RedditRSSMonitor/ai_api_key"
```

The connector rejects key files readable by other users. It reads the key locally and sends it only as authentication to the selected provider. Your source excerpts and instructions are also sent to that provider for analysis.

## 3. Edit your private configuration

After normal setup, open `~/Library/Application Support/RedditRSSMonitor/config.json`. Set these fields, preserving the rest of your configuration:

```json
{
  "ai_enabled": false,
  "ai_provider": "gemini",
  "ai_model": "YOUR_PROVIDER_MODEL_ID",
  "ai_key_file": "~/Library/Application Support/RedditRSSMonitor/ai_api_key",
  "ai_max_requests_per_day": 12,
  "ai_content_hour_ist": 21,
  "ai_reply_prompt_file": "",
  "ai_content_prompt_file": ""
}
```

Choose `gemini`, `groq` or `openrouter`. Replace the model placeholder with an ID offered to your account. AI is disabled until you deliberately enable it. A model must be selected; this package does not silently choose one for you.

The current AI output timestamps use IST. Content generation waits until `ai_content_hour_ist` (0–23, India time), and the local daily request counter resets using America/Los_Angeles time. These are current implementation choices, separate from the Mac scheduler's local timezone.

## 4. Write instructions for your use case

The default prompts are examples for Indian personal-finance research. For another subject or tone, save your own UTF-8 instructions in a local text file and put its path in `ai_reply_prompt_file` or `ai_content_prompt_file`. Empty paths use the built-in examples in `reddit_monitor/ai_prompts.py`.

Describe the audience, what is relevant, useful reply style, prohibited claims and when to hold a draft. Tell the model to treat Reddit text as evidence, join only by Post ID, answer the actual question and state missing information. The program appends the saved source data and still requires its structured JSON contract. Custom instructions change editorial reasoning; they do not add arbitrary output columns or automatic web research.

The existing local validation includes finance-specific image checks and rejects unknown source IDs. If adapting to a different domain, review `reddit_monitor/ai_pipeline.py` and its tests as well as the prompts.

## 5. Test manually

First collect successfully and check the source workbook's Runs tab. Then, from the project folder:

```bash
python3 -m reddit_monitor.cli validate-config
python3 -m reddit_monitor.cli ai-run --kind replies
```

An explicit `ai-run` can call the provider even while `ai_enabled` is false; the flag controls automatic review after collection. The connector reads the saved JSON batch in `output/.data/`, rejects a batch generated over five hours ago and remembers drafts/idea keys in private runtime state. Batch generation time alone does not prove upstream freshness, so verify collection first.

Review `output/AI-Reply-Review-Latest.xlsx`. It has Drafts, Skipped and Run Log tabs. For content, run `python3 -m reddit_monitor.cli ai-run --kind content` after the configured India-time hour, then review `output/AI-Content-Ideas-Latest.xlsx`, which has Ideas and Audit tabs.

The current AI pass examines a bounded shortlist, generates at most five replies and up to four content ideas per pass, and supplies bounded source/comment excerpts. It does not promise a draft for every collected post. It does not browse live market information, read image-only allocations, upload AI output through the source-workbook uploader, send Slack alerts or publish to Reddit.

## 6. Enable only after reviewing results

If the provider combination works and the drafts meet your requirements, set `ai_enabled` to `true`. Scheduled collection can then run the local review stage. The same request caps and source limitations still apply. Keep a person responsible for reviewing facts, the live discussion and community rules before posting.
