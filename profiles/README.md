# Profiles — one file describes the business, the code describes the machine

To run the engine for a new offer:

1. Copy `ruyatech.toml` to `<name>.toml` in this directory.
2. Edit the copy: `[identity]`, `[offers.*]`, `[segments.*]`, `[icp]`,
   `[voice]`, `[sequences]`, `[[derivation.rules]]`, `[scoring_criteria]`.
   Keep snake_case keys and the same table structure — see the header
   comment in `ruyatech.toml` for what each section means.
3. Select it with `LEAD_PROFILE=<name>` in the environment (or
   `[profile] name = "<name>"` in `config.toml`; env wins, default
   `ruyatech`).
4. Write ~15 golden cases under `profiles/<name>/golden/` and run
   `python tools/run_golden.py --profile <name>`.

No `.py` file needs editing to change the offer. `config.toml` keeps only
how the machine runs (rate limits, caps, delays, LLM, concurrency);
everything about what is sold and to whom lives here.
