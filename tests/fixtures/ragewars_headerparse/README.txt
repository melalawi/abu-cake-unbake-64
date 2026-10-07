Real RageWars headerparse residual on tool main 47838d6 (also reported on 2eaef17).
func_802612A8_de.c keeps the original signature and conditional if-call from
the legacy source lines 27 and 49-65; unrelated statements are omitted.
generated.h contains the five original branch prototypes plus the caller
contract; types.h contains the original scalar provider.
The signature regex consumed the body call after masking directives and
failed with: header declaration: expected ;, found func_8025F074_us.
This happens in _source_drops during render, before validation can save a
held-header-context artifact. The earlier route 6 render artifact contains
no failing function prototype; no held context exists in either game tree.
