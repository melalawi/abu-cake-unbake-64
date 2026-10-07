"""Public stdarg for the pinned family, verified by native caller/argument payloads.

Reference: https://github.com/decompals/ultralib/blob/main/include/compiler/gcc/stdarg.h
An independent implementation captures the cursor lvalue once. Existing target
va_list declarations may supply its public type through generic provisioning.
"""

from unbake.compilers.families.types import PublicHeader

SELECTOR = "__UNBAKE_STDARG_GCC"
BODY = r"""typedef char __unbake_stdarg_target[
    sizeof(int) == 4 && sizeof(long) == 4 && sizeof(long long) == 8 &&
    sizeof(double) == 8 && sizeof(void *) == 4 && sizeof(va_list) == 4 ? 1 : -1];
extern void *__builtin_next_arg();
#define va_start(ap, last) ((ap) = (va_list)__builtin_next_arg(last))
#define __unbake_va_alignment(type) (sizeof(struct { char pad; type item; }) - sizeof(type))
#ifdef __STRICT_ANSI__
#define __unbake_stdarg_inline
#else
#define __unbake_stdarg_inline inline
#endif
static __unbake_stdarg_inline char *__unbake_stdarg_take(va_list *ap, unsigned int size,
                                                     unsigned int alignment)
{
    char *slot = (char *)(((unsigned int)*ap + alignment - 1) & -alignment);
    *ap = slot + ((size + 3) & -4);
    return slot;
}
/* Keep the expression sequencing boundary of historical GNU va_arg expansion.
 * Inlining a C helper is ABI-equivalent but can reorder an enclosing assignment. */
#ifdef __STRICT_ANSI__
#define va_arg(ap, type) (*(type *)__unbake_stdarg_take(&(ap), sizeof(type), \
                         __unbake_va_alignment(type) > 4 ? 8 : 4))
#else
#define va_arg(ap, type) ({ \
    va_list *__unbake_cursor = &(ap); \
    char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + \
        (__unbake_va_alignment(type) > 4 ? 8 : 4) - 1) & \
        -(__unbake_va_alignment(type) > 4 ? 8 : 4)); \
    *__unbake_cursor = __unbake_slot + ((sizeof(type) + 3) & -4); \
    *(type *)__unbake_slot; })
#endif
#define va_end(ap) ((void)(ap))
#define va_copy(dst, src) ((dst) = (src))
"""


def header() -> PublicHeader:
    return PublicHeader("stdarg.h", SELECTOR, (("va_list", "char *", "typedef char *va_list;"),), BODY)
