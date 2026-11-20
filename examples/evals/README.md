# Eval suites for release

`bank-assistant/` holds three suites for the example `bank-assistant` (adapt the facts for your own agent):

| Suite | Required | Checks | Model |
|---|---|---|---|
| `guardrails` | yes | blocked topics (stock tips, crypto, gambling), escalation (complaint, human, fraud, compliance), prompt-injection block, IBAN / card redaction, disclaimer, cost and latency limits | any (controls are enforced in code) |
| `answers` | yes | product facts (1.75%, 2.40/2.60%, 10% early repayment, 4.5x income, phone and hours), the mortgage payment (about 1,771 EUR) **and** that the calculator tool was used | a real model; fails on `mock-*` on purpose |
| `quality` | no | LLM judge: no personal advice, no loan promise, "rates can change", admits unknowns, answers in Dutch, tone and format | judge `google-ai-flash` |

`suites.json` sets which suites are required and the suite-level checks applied to every case. Required
suites must pass 100% (platform policy `min_eval_pass_rate`) on exactly the draft being released.

## Run and release

```bash
python scripts/eval-release.py bank-assistant examples/evals/bank-assistant
python scripts/eval-release.py bank-assistant examples/evals/bank-assistant --release \
  --user tom@example.com --roles "" --approver fred@example.com --approver-roles "retail:approver,retail:publisher"
```

The script uploads the suites, runs **all** suites of the agent on the draft, lists every failed check
with the answer, and with `--release` approves (as a second person), publishes and points `prod`.

In the console the same steps are on the agent's 🚀 Release tab: upload one file per suite (tick "required"),
"Run all suites on draft", Approve (another person), Publish, move `prod`.
