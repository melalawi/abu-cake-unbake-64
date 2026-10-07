Small real native stdarg payloads, 2026-10-07. Public provider is independently
implemented from pinned native behavior and historical ultralib compiler headers.
Reference: https://github.com/decompals/ultralib/tree/main/include/compiler

caller.c exercises15 exact type requests from the real printf recipe,4/8byte slot
alignment and holes, va_copy independence, an lvalue function call evaluated
exactly15times and a floating named-argument prefix. All4compiled executables
return0 under qemu-mips -cpu24Kf. Native target sizes int,long,longlong,double,
pointer,va_list are4,4,8,8,4,4. IDO5.3/7.1 preserve saved-FP cursor tagging;
GNU2.7.2/2.8.1 use native next-argument builtin. Do not treat C functions named
__builtin_va_start as intrinsics: actual native probes emitted unresolved calls.

Public/raw kernels contain all15 requests in original order. GNU .text matches
byte-for-byte under each pinned compiler, no fabricated assembly/format. This
is NOT the entire printf function or new allholding game proof. normalized-printf.c
records one15site recipe; game owner proves and publishes it with the provider.

Commands: native registry cflags + cc1 -quiet (GNU), hostcpp -P -undef -nostdinc
-I. plus selected family public define; n64link asn64 --as mips-linux-gnu-as
-march=vr4300 -mabi=32 -EB -G0 --no-pad-sections. IDO native cc uses registry
cflags -I. plus its selected public define. Linux O32 harness start.s uses GNUas;
ld -static -e _start -Ttext=0x400100 start.o caller.o -o caller.elf.

SN64 -mfp64 execution additionally marks both object inputs -mfp64 so ELF/QEMU
selectFR=1. The same program underFR=0 failed case9, demonstrating the real
mode requirement; this is not an stdarg cursor exemption. Test harness declares
volatile bitpattern data for double values: bare li.d normalized literal pools
require ROM placement and would point at0 in a standalone link. No game source
or installed tool was changed to avoid either runtime condition.

GNU provider uses ordinary inline C so native expansions remain in the public
source-admission grammar. Compiler reserved operators are supplied to the generic
call validator solely through Family.source_intrinsics; unproved builtin-like
external calls and all manual raw pointer arithmetic remain refused.
