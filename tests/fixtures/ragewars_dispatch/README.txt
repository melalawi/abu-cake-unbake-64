Real RW guarded six-entry ROM jump table and full3612-byte texture binder.
Table800CA688 is24bytes, with the configured resident entry bias retained.
Four US revision1 callers prove the ninth O32 word at SP+32.
The de-only formatter caller and its16-byte saved-ra/sp epilogue preserve
the ordinary-call-clobber contradiction; their missing values are not invented.
Full bodies pin target hashes; the table itself remains separate ROM data.
