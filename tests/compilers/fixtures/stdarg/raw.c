typedef char *va_list;
#define READ(ap,T) ({ char *__slot=(char*)(((int)(ap)+sizeof(T)-1)&-(int)sizeof(T)); (ap)=__slot+sizeof(T); *(T*)__slot; })
int read_0(va_list *pap) { return READ(*pap, int); }
long read_1(va_list *pap) { return READ(*pap, long); }
long long read_2(va_list *pap) { return READ(*pap, long long); }
int read_3(va_list *pap) { return READ(*pap, int); }
long read_4(va_list *pap) { return READ(*pap, long); }
long long read_5(va_list *pap) { return READ(*pap, long long); }
int read_6(va_list *pap) { return READ(*pap, int); }
double read_7(va_list *pap) { return READ(*pap, double); }
double read_8(va_list *pap) { return READ(*pap, double); }
unsigned short * read_9(va_list *pap) { return READ(*pap, unsigned short *); }
unsigned long * read_10(va_list *pap) { return READ(*pap, unsigned long *); }
unsigned long long * read_11(va_list *pap) { return READ(*pap, unsigned long long *); }
unsigned int * read_12(va_list *pap) { return READ(*pap, unsigned int *); }
void * read_13(va_list *pap) { return READ(*pap, void *); }
unsigned char * read_14(va_list *pap) { return READ(*pap, unsigned char *); }
