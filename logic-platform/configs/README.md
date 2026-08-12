# configs/

Reserved for configuration once there is any.

Today the only tunable values are the execution limits, and they are passed
programmatically as `lml_runtime::Limits`, not read from a file. A config file
that is parsed but ignored — or one whose defaults silently disagree with the
constants in the code — is worse than none, so there is none.

When a file lands here it will be because something needs to be configured
without recompiling, and `docs/03_EXECUTION_MODEL.md` §5 will say which values
those are.
