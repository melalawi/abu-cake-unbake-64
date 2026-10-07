Reduced saved BattleTanx and RageWars machine/decompiler inputs.

BT: func_80112E70_us, measured LW/SW after base + byte index * 4.
Assembly keeps instruction byte/address comments from the saved extraction.
C keeps the two opaque access expressions verbatim from raw m2c output.
The global was undeclared in its provider context; the old draft used an
opaque byte declaration. The symbol CSV is extraction identity only: its
size 4 establishes neither table length nor pointee layout.
The real reduced C/asm refuses on main 31df86d with data load lacks a
measured width; stripping comments alone removes that particular refusal.

RW: func_80219688_de, completed HI/LO address and numeric LBU at 0x1D.
The C expression is the raw field read; it already spells its width.
This comparable real evidence tests global provenance independently of
field/header normalization. It was found among at most 12 cached bodies.
Only general register-name normalization was needed for raw m2c output.

Provenance hashes identify complete saved assembly and raw output.
No table length, pointer target, or callback signature is inferred.
