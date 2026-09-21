# Evaluating OpenOCR locally

Quick, offline check that both models actually extract text well before
trusting the app, following the approach olmOCR 2 describes for its own
eval: run fixed fixtures through the model and check the output against
known-good text rather than trusting benchmark numbers alone.

```
cd Aman/OpenOCR
uv run eval/generate_fixtures.py     # writes eval/fixtures/*.png + *.gt.txt
uv run eval/run_eval.py --mode fast  # or --mode accurate / --mode both
```

`generate_fixtures.py` renders three synthetic documents with exactly known
text (a paragraph, a table, a receipt), so scoring against them is not
approximate. Drop real scans or screenshots into `eval/fixtures/` too --
without a matching `<name>.gt.txt` they still run, just without a similarity
score, so the extracted Markdown/JSON in `eval/results/` is there to read by
eye.

`run_eval.py` calls `engine.run_sync(...)` directly -- the same code path the
app's worker uses, minus the async job queue -- loads each model once, runs
every fixture through it, and writes `<fixture>__<mode>.md` / `.json` /
`.raw.txt` to `eval/results/`. Where a `.gt.txt` exists it also prints a
whitespace-normalized similarity ratio (`difflib.SequenceMatcher`) as a rough
sanity signal, not a substitute for reading the output.

First run downloads the model weights (Fast: ~700 MB, Accurate: ~3.5 GB) to
the shared Hugging Face cache -- the app itself reuses whatever is already
downloaded here.
