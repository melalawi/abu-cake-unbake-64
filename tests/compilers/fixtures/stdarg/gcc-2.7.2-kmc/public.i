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
int read_0(va_list *pap) { return (*(int *)__unbake_stdarg_take(&(*pap), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); }
long read_1(va_list *pap) { return (*(long *)__unbake_stdarg_take(&(*pap), sizeof(long), (sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)); }
long long read_2(va_list *pap) { return (*(long long *)__unbake_stdarg_take(&(*pap), sizeof(long long), (sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4)); }
int read_3(va_list *pap) { return (*(int *)__unbake_stdarg_take(&(*pap), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); }
long read_4(va_list *pap) { return (*(long *)__unbake_stdarg_take(&(*pap), sizeof(long), (sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)); }
long long read_5(va_list *pap) { return (*(long long *)__unbake_stdarg_take(&(*pap), sizeof(long long), (sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4)); }
int read_6(va_list *pap) { return (*(int *)__unbake_stdarg_take(&(*pap), sizeof(int), (sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); }
double read_7(va_list *pap) { return (*(double *)__unbake_stdarg_take(&(*pap), sizeof(double), (sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4)); }
double read_8(va_list *pap) { return (*(double *)__unbake_stdarg_take(&(*pap), sizeof(double), (sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4)); }
unsigned short * read_9(va_list *pap) { return (*(unsigned short * *)__unbake_stdarg_take(&(*pap), sizeof(unsigned short *), (sizeof(struct { char pad; unsigned short * item; }) - sizeof(unsigned short *)) > 4 ? 8 : 4)); }
unsigned long * read_10(va_list *pap) { return (*(unsigned long * *)__unbake_stdarg_take(&(*pap), sizeof(unsigned long *), (sizeof(struct { char pad; unsigned long * item; }) - sizeof(unsigned long *)) > 4 ? 8 : 4)); }
unsigned long long * read_11(va_list *pap) { return (*(unsigned long long * *)__unbake_stdarg_take(&(*pap), sizeof(unsigned long long *), (sizeof(struct { char pad; unsigned long long * item; }) - sizeof(unsigned long long *)) > 4 ? 8 : 4)); }
unsigned int * read_12(va_list *pap) { return (*(unsigned int * *)__unbake_stdarg_take(&(*pap), sizeof(unsigned int *), (sizeof(struct { char pad; unsigned int * item; }) - sizeof(unsigned int *)) > 4 ? 8 : 4)); }
void * read_13(va_list *pap) { return (*(void * *)__unbake_stdarg_take(&(*pap), sizeof(void *), (sizeof(struct { char pad; void * item; }) - sizeof(void *)) > 4 ? 8 : 4)); }
unsigned char * read_14(va_list *pap) { return (*(unsigned char * *)__unbake_stdarg_take(&(*pap), sizeof(unsigned char *), (sizeof(struct { char pad; unsigned char * item; }) - sizeof(unsigned char *)) > 4 ? 8 : 4)); }
