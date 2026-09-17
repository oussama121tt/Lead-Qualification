# Profiles — one file describes the business, the code describes the machine

To run the engine for a new offer:

1. Copy `ruyatech.toml` to `<name>.toml` in this directory.
2. Edit the copy: `[identity]`, `[offers.*]`, `[segments.*]`, `[criteria.*]`,
   `[icp]`, `[voice]`, `[scoring]`, `[[derivation.rules]]`, `[sequences]`,
   `[sequences.offers.*]`. Keep snake_case keys and the same table
   structure — see the header comment in `ruyatech.toml` for what each
   section means.
3. Select it with `LEAD_PROFILE=<name>` in the environment (or
   `[profile] name = "<name>"` in `config.toml`; env wins, default
   `ruyatech`). One profile per running instance.
4. Write ~15 golden cases under `profiles/<name>/golden/` and run
   `python tools/run_golden.py --profile <name>`.

No `.py` file needs editing to change the offer. `config.toml` keeps only
how the machine runs (rate limits, caps, delays, LLM, concurrency);
everything about what is sold and to whom lives here.

Reserved machine names: every profile must define an `"unclear"` segment
and use `"none"` for no offer — they are the catch-all unknowns the engine
falls back to. The `founder_profile` x `build_evidence` axis vocabulary is
engine-level; `[[derivation.rules]]` maps axis pairs to (segment, offer)
when the model returns `"unclear"`, and `[scoring]` prose (`axes_prose`,
`extra_rules`, `career_hint`, `unclear_note`) tells the model the same
rules in natural language. Keep the rules and the prose in sync.
