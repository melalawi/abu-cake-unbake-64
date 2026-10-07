typedef char *va_list;
typedef char __unbake_stdarg_target[
    sizeof(int) == 4 && sizeof(long) == 4 && sizeof(long long) == 8 &&
    sizeof(double) == 8 && sizeof(void *) == 4 && sizeof(va_list) == 4 ? 1 : -1];
extern void *__builtin_next_arg();
static inline char *__unbake_stdarg_take(va_list *ap, unsigned int size,
                                          unsigned int alignment)
{
    char *slot = (char *)(((unsigned int)*ap + alignment - 1) & -alignment);
    *ap = slot + ((size + 3) & -4);
    return slot;
}
union Wide { long long ll; double d; unsigned int w[2]; };
unsigned int target_sizes[] = {sizeof(int), sizeof(long), sizeof(long long), sizeof(double), sizeof(void *), sizeof(va_list)};
static volatile union Wide bits35 = {0x400C000000000000LL};
static volatile union Wide bits75 = {0x401E000000000000LL};
static volatile union Wide bits25 = {0x4004000000000000LL};
static int visits;
static va_list *active;
static int items[6];
static va_list *cursor(void) { ++visits; return active; }
int collect(int named, ...) {
    va_list ap, saved;
    union Wide value;
    ((ap) = (va_list)__builtin_next_arg(named));
    ((saved) = (ap));
    if ((*(int *)__unbake_stdarg_take(&(saved), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)) != 11) return 1;
    active = &ap;
    visits = 0;
    if ((*(int *)__unbake_stdarg_take(&(*cursor()), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)) != 11) return 2;
    if ((*(long *)__unbake_stdarg_take(&(*cursor()), sizeof(long), (sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)) != 22L) return 3;
    value.ll = (*(long long *)__unbake_stdarg_take(&(*cursor()), sizeof(long long), (sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4));
    if (value.w[0] != 0x11223344U || value.w[1] != 0x55667788U) return 4;
    if ((*(int *)__unbake_stdarg_take(&(*cursor()), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)) != 44) return 5;
    if ((*(long *)__unbake_stdarg_take(&(*cursor()), sizeof(long), (sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)) != 55L) return 6;
    value.ll = (*(long long *)__unbake_stdarg_take(&(*cursor()), sizeof(long long), (sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4));
    if (value.w[0] != 0x12345678U || value.w[1] != 0x23456789U) return 7;
    if ((*(int *)__unbake_stdarg_take(&(*cursor()), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)) != 77) return 8;
    value.d = (*(double *)__unbake_stdarg_take(&(*cursor()), sizeof(double), (sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4));
    if (value.w[0] != 0x400C0000U || value.w[1] != 0) return 9;
    value.d = (*(double *)__unbake_stdarg_take(&(*cursor()), sizeof(double), (sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4));
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 10;
    if ((*(unsigned short * *)__unbake_stdarg_take(&(*cursor()), sizeof(unsigned short *), (sizeof(struct { char pad; unsigned short * item; }) - sizeof(unsigned short *)) > 4 ? 8 : 4)) != (unsigned short *)&items[0]) return 11;
    if ((*(unsigned long * *)__unbake_stdarg_take(&(*cursor()), sizeof(unsigned long *), (sizeof(struct { char pad; unsigned long * item; }) - sizeof(unsigned long *)) > 4 ? 8 : 4)) != (unsigned long *)&items[1]) return 12;
    if ((*(unsigned long long * *)__unbake_stdarg_take(&(*cursor()), sizeof(unsigned long long *), (sizeof(struct { char pad; unsigned long long * item; }) - sizeof(unsigned long long *)) > 4 ? 8 : 4)) != (unsigned long long *)&items[2]) return 13;
    if ((*(unsigned int * *)__unbake_stdarg_take(&(*cursor()), sizeof(unsigned int *), (sizeof(struct { char pad; unsigned int * item; }) - sizeof(unsigned int *)) > 4 ? 8 : 4)) != (unsigned int *)&items[3]) return 14;
    if ((*(void * *)__unbake_stdarg_take(&(*cursor()), sizeof(void *), (sizeof(struct { char pad; void * item; }) - sizeof(void *)) > 4 ? 8 : 4)) != &items[4]) return 15;
    if ((*(unsigned char * *)__unbake_stdarg_take(&(*cursor()), sizeof(unsigned char *), (sizeof(struct { char pad; unsigned char * item; }) - sizeof(unsigned char *)) > 4 ? 8 : 4)) != (unsigned char *)&items[5]) return 16;
    ((void)(ap));
    ((void)(saved));
    return visits == 15 ? 0 : 17;
}
int named_fp(double first, ...) {
    va_list ap;
    union Wide value;
    ((ap) = (va_list)__builtin_next_arg(first));
    value.d = (*(double *)__unbake_stdarg_take(&(ap), sizeof(double), (sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4));
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 18;
    if ((*(int *)__unbake_stdarg_take(&(ap), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)) != 19) return 19;
    ((void)(ap));
    return 0;
}
int exercise(void) {
    int result = collect(101, 11, 22L, 0x1122334455667788LL, 44, 55L,
        0x1234567823456789LL, 77, bits35.d, bits75.d, (unsigned short *)&items[0],
        (unsigned long *)&items[1], (unsigned long long *)&items[2],
        (unsigned int *)&items[3], (void *)&items[4], (unsigned char *)&items[5]);
    return result ? result : named_fp(bits25.d, bits75.d, 19);
}
