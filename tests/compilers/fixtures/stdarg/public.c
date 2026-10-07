#include <stdarg.h>
int read_0(va_list *pap) { return va_arg(*pap, int); }
long read_1(va_list *pap) { return va_arg(*pap, long); }
long long read_2(va_list *pap) { return va_arg(*pap, long long); }
int read_3(va_list *pap) { return va_arg(*pap, int); }
long read_4(va_list *pap) { return va_arg(*pap, long); }
long long read_5(va_list *pap) { return va_arg(*pap, long long); }
int read_6(va_list *pap) { return va_arg(*pap, int); }
double read_7(va_list *pap) { return va_arg(*pap, double); }
double read_8(va_list *pap) { return va_arg(*pap, double); }
unsigned short * read_9(va_list *pap) { return va_arg(*pap, unsigned short *); }
unsigned long * read_10(va_list *pap) { return va_arg(*pap, unsigned long *); }
unsigned long long * read_11(va_list *pap) { return va_arg(*pap, unsigned long long *); }
unsigned int * read_12(va_list *pap) { return va_arg(*pap, unsigned int *); }
void * read_13(va_list *pap) { return va_arg(*pap, void *); }
unsigned char * read_14(va_list *pap) { return va_arg(*pap, unsigned char *); }
