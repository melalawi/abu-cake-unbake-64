"""Public stdarg for the pinned family, verified by native caller/argument payloads.

Reference: https://github.com/decompals/ultralib/blob/main/include/compiler/ido/stdarg.h
An independent implementation captures the cursor lvalue once. Existing target
va_list declarations may supply its public type through generic provisioning.
"""

from unbake.compilers.families.types import PublicHeader

SELECTOR = "__UNBAKE_STDARG_IDO"
BODY = r"""typedef char __unbake_stdarg_target[
    sizeof(int) == 4 && sizeof(long) == 4 && sizeof(long long) == 8 &&
    sizeof(double) == 8 && sizeof(void *) == 4 && sizeof(va_list) == 4 ? 1 : -1];
#define va_start(ap, last) ((ap) = (va_list)((char *)&(last) + sizeof(last)))
/* The native compiler tags saved FP argument cursors. The host token provider
 * parses equivalent type expressions without sending its builtins to cfe. */
#ifdef __UNBAKE_HEADER_ANALYSIS
#define __unbake_va_align(type) (sizeof(type) > 4 ? 8 : 4)
#define __unbake_va_fp(type) (__builtin_classify_type(*(type *)0) == 8)
#else
#define __unbake_va_align(type) __builtin_alignof(*(type *)0)
#define __unbake_va_fp(type) (__builtin_classof(*(type *)0) == 1)
#endif
static char *__unbake_stdarg_take(va_list *ap, unsigned int size,
                                 unsigned int alignment, int floating)
{
    unsigned int cursor = (unsigned int)*ap;
    unsigned int next;
    char *slot;
    if (floating && alignment == 8 && (cursor & 1)) {
        next = cursor + 7;
        slot = (char *)(next - 6 - 16 - size);
    } else if (floating && alignment == 8 && (cursor & 2)) {
        next = cursor + 10;
        slot = (char *)(next - 24 - 16 - size);
    } else {
        if (alignment < 4) alignment = 4;
        slot = (char *)((cursor + alignment - 1) & -alignment);
        next = (unsigned int)slot + ((size + 3) & -4);
    }
    *ap = (char *)next;
    return slot;
}
#define va_arg(ap, type) (*(type *)__unbake_stdarg_take(&(ap), sizeof(type), \
                         __unbake_va_align(type), __unbake_va_fp(type)))
#define va_end(ap) ((void)(ap))
#define va_copy(dst, src) ((dst) = (src))
"""


def header() -> PublicHeader:
    return PublicHeader("stdarg.h", SELECTOR, (("va_list", "char *", "typedef char *va_list;"),), BODY)
