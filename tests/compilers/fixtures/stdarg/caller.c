#include <stdarg.h>
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
    va_start(ap, named);
    va_copy(saved, ap);
    if (va_arg(saved, int) != 11) return 1;
    active = &ap;
    visits = 0;
    if (va_arg(*cursor(), int) != 11) return 2;
    if (va_arg(*cursor(), long) != 22L) return 3;
    value.ll = va_arg(*cursor(), long long);
    if (value.w[0] != 0x11223344U || value.w[1] != 0x55667788U) return 4;
    if (va_arg(*cursor(), int) != 44) return 5;
    if (va_arg(*cursor(), long) != 55L) return 6;
    value.ll = va_arg(*cursor(), long long);
    if (value.w[0] != 0x12345678U || value.w[1] != 0x23456789U) return 7;
    if (va_arg(*cursor(), int) != 77) return 8;
    value.d = va_arg(*cursor(), double);
    if (value.w[0] != 0x400C0000U || value.w[1] != 0) return 9;
    value.d = va_arg(*cursor(), double);
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 10;
    if (va_arg(*cursor(), unsigned short *) != (unsigned short *)&items[0]) return 11;
    if (va_arg(*cursor(), unsigned long *) != (unsigned long *)&items[1]) return 12;
    if (va_arg(*cursor(), unsigned long long *) != (unsigned long long *)&items[2]) return 13;
    if (va_arg(*cursor(), unsigned int *) != (unsigned int *)&items[3]) return 14;
    if (va_arg(*cursor(), void *) != &items[4]) return 15;
    if (va_arg(*cursor(), unsigned char *) != (unsigned char *)&items[5]) return 16;
    va_end(ap);
    va_end(saved);
    return visits == 15 ? 0 : 17;
}
int named_fp(double first, ...) {
    va_list ap;
    union Wide value;
    va_start(ap, first);
    value.d = va_arg(ap, double);
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 18;
    if (va_arg(ap, int) != 19) return 19;
    va_end(ap);
    return 0;
}
int exercise(void) {
    int result = collect(101, 11, 22L, 0x1122334455667788LL, 44, 55L,
        0x1234567823456789LL, 77, bits35.d, bits75.d, (unsigned short *)&items[0],
        (unsigned long *)&items[1], (unsigned long long *)&items[2],
        (unsigned int *)&items[3], (void *)&items[4], (unsigned char *)&items[5]);
    return result ? result : named_fp(bits25.d, bits75.d, 19);
}
