# 1 "caller.c"
# 1 "./stdarg.h"
 


typedef char *va_list;




















typedef char __unbake_stdarg_target[
    sizeof(int) == 4 && sizeof(long) == 4 && sizeof(long long) == 8 &&
    sizeof(double) == 8 && sizeof(void *) == 4 && sizeof(va_list) == 4 ? 1 : -1];

 








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

 







# 2 "caller.c"
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
    ((ap) = (va_list)((char *)&( named) + sizeof( named))) ;
    ((saved) = ( ap)) ;
    if ((*( int *)__unbake_stdarg_take(&(saved), sizeof( int),                          __builtin_alignof(*( int *)0) , (__builtin_classof(*( int *)0) == 1) ))  != 11) return 1;
    active = &ap;
    visits = 0;
    if ((*( int *)__unbake_stdarg_take(&(*cursor()), sizeof( int),                          __builtin_alignof(*( int *)0) , (__builtin_classof(*( int *)0) == 1) ))  != 11) return 2;
    if ((*( long *)__unbake_stdarg_take(&(*cursor()), sizeof( long),                          __builtin_alignof(*( long *)0) , (__builtin_classof(*( long *)0) == 1) ))  != 22L) return 3;
    value.ll = (*( long long *)__unbake_stdarg_take(&(*cursor()), sizeof( long long),                          __builtin_alignof(*( long long *)0) , (__builtin_classof(*( long long *)0) == 1) )) ;
    if (value.w[0] != 0x11223344U || value.w[1] != 0x55667788U) return 4;
    if ((*( int *)__unbake_stdarg_take(&(*cursor()), sizeof( int),                          __builtin_alignof(*( int *)0) , (__builtin_classof(*( int *)0) == 1) ))  != 44) return 5;
    if ((*( long *)__unbake_stdarg_take(&(*cursor()), sizeof( long),                          __builtin_alignof(*( long *)0) , (__builtin_classof(*( long *)0) == 1) ))  != 55L) return 6;
    value.ll = (*( long long *)__unbake_stdarg_take(&(*cursor()), sizeof( long long),                          __builtin_alignof(*( long long *)0) , (__builtin_classof(*( long long *)0) == 1) )) ;
    if (value.w[0] != 0x12345678U || value.w[1] != 0x23456789U) return 7;
    if ((*( int *)__unbake_stdarg_take(&(*cursor()), sizeof( int),                          __builtin_alignof(*( int *)0) , (__builtin_classof(*( int *)0) == 1) ))  != 77) return 8;
    value.d = (*( double *)__unbake_stdarg_take(&(*cursor()), sizeof( double),                          __builtin_alignof(*( double *)0) , (__builtin_classof(*( double *)0) == 1) )) ;
    if (value.w[0] != 0x400C0000U || value.w[1] != 0) return 9;
    value.d = (*( double *)__unbake_stdarg_take(&(*cursor()), sizeof( double),                          __builtin_alignof(*( double *)0) , (__builtin_classof(*( double *)0) == 1) )) ;
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 10;
    if ((*( unsigned short * *)__unbake_stdarg_take(&(*cursor()), sizeof( unsigned short *),                          __builtin_alignof(*( unsigned short * *)0) , (__builtin_classof(*( unsigned short * *)0) == 1) ))  != (unsigned short *)&items[0]) return 11;
    if ((*( unsigned long * *)__unbake_stdarg_take(&(*cursor()), sizeof( unsigned long *),                          __builtin_alignof(*( unsigned long * *)0) , (__builtin_classof(*( unsigned long * *)0) == 1) ))  != (unsigned long *)&items[1]) return 12;
    if ((*( unsigned long long * *)__unbake_stdarg_take(&(*cursor()), sizeof( unsigned long long *),                          __builtin_alignof(*( unsigned long long * *)0) , (__builtin_classof(*( unsigned long long * *)0) == 1) ))  != (unsigned long long *)&items[2]) return 13;
    if ((*( unsigned int * *)__unbake_stdarg_take(&(*cursor()), sizeof( unsigned int *),                          __builtin_alignof(*( unsigned int * *)0) , (__builtin_classof(*( unsigned int * *)0) == 1) ))  != (unsigned int *)&items[3]) return 14;
    if ((*( void * *)__unbake_stdarg_take(&(*cursor()), sizeof( void *),                          __builtin_alignof(*( void * *)0) , (__builtin_classof(*( void * *)0) == 1) ))  != &items[4]) return 15;
    if ((*( unsigned char * *)__unbake_stdarg_take(&(*cursor()), sizeof( unsigned char *),                          __builtin_alignof(*( unsigned char * *)0) , (__builtin_classof(*( unsigned char * *)0) == 1) ))  != (unsigned char *)&items[5]) return 16;
    ((void)(ap)) ;
    ((void)(saved)) ;
    return visits == 15 ? 0 : 17;
}
int named_fp(double first, ...) {
    va_list ap;
    union Wide value;
    ((ap) = (va_list)((char *)&( first) + sizeof( first))) ;
    value.d = (*( double *)__unbake_stdarg_take(&(ap), sizeof( double),                          __builtin_alignof(*( double *)0) , (__builtin_classof(*( double *)0) == 1) )) ;
    if (value.w[0] != 0x401E0000U || value.w[1] != 0) return 18;
    if ((*( int *)__unbake_stdarg_take(&(ap), sizeof( int),                          __builtin_alignof(*( int *)0) , (__builtin_classof(*( int *)0) == 1) ))  != 19) return 19;
    ((void)(ap)) ;
    return 0;
}
int exercise(void) {
    int result = collect(101, 11, 22L, 0x1122334455667788LL, 44, 55L,
        0x1234567823456789LL, 77, bits35.d, bits75.d, (unsigned short *)&items[0],
        (unsigned long *)&items[1], (unsigned long long *)&items[2],
        (unsigned int *)&items[3], (void *)&items[4], (unsigned char *)&items[5]);
    return result ? result : named_fp(bits25.d, bits75.d, 19);
}
