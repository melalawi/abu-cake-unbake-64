typedef unsigned int size_t;
typedef char * va_list;
struct _Pft;
typedef struct _Pft _Pft;
struct _Pft {
    union {
        long long ll;
        double ld;
    } v;
    unsigned char *s;
    int n0;
    int nz0;
    int n1;
    int nz1;
    int n2;
    int nz2;
    int prec;
    int width;
    size_t nchar;
    unsigned int flags;
    unsigned char qual;
};
extern void func_802BDDE0_de(_Pft *px, unsigned char code);
void func_802BE0C0_de(void *unused_state, unsigned char unused_code);
void func_802BD974_de(_Pft *px, va_list *pap, int code, unsigned char *ac)
{
    px->n0 = px->nz0 = px->n1 = px->nz1 = px->n2 =
        px->nz2 = 0;
    switch ((unsigned char)code) {
    case 'c': goto case_c;
    case 'd': case 'i': goto case_d;
    case 'o': case 'u': case 'x': case 'X': goto case_x;
    case 'e': case 'E': case 'f': case 'g': case 'G': goto case_e;
    case 'n': goto case_n;
    case 'p': goto case_p;
    case 's': goto case_s;
    case '%': goto case_percent;
    default: goto case_default;
    }
case_c:
    ac[px->n0++] = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(int) + 3) & -4); *(int *)__unbake_slot; });
    return;
case_d:
    if (px->qual == 'l') {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(long) + 3) & -4); *(long *)__unbake_slot; });
    } else if (px->qual == 'L') {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(long long) + 3) & -4); *(long long *)__unbake_slot; });
    } else {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(int) + 3) & -4); *(int *)__unbake_slot; });
    }
    if (px->qual == 'h') {
        px->v.ll = (short)px->v.ll;
    }
    if (px->v.ll < 0) {
        ac[px->n0++] = '-';
    } else if (px->flags & 2) {
        ac[px->n0++] = '+';
    } else if (px->flags & 1) {
        ac[px->n0++] = ' ';
    }
    px->s = (unsigned char *)&ac[px->n0];
    func_802BDDE0_de(px, (unsigned char)code);
    return;
case_x:
    if (px->qual == 'l') {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; long item; }) - sizeof(long)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(long) + 3) & -4); *(long *)__unbake_slot; });
    } else if (px->qual == 'L') {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; long long item; }) - sizeof(long long)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(long long) + 3) & -4); *(long long *)__unbake_slot; });
    } else {
        px->v.ll = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; int item; }) - sizeof(int)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(int) + 3) & -4); *(int *)__unbake_slot; });
    }
    if (px->qual == 'h') {
        px->v.ll = (unsigned short)px->v.ll;
    } else if (px->qual == 0) {
        px->v.ll = (unsigned int)px->v.ll;
    }
    if (px->flags & 8) {
        ac[px->n0++] = '0';
        if ((unsigned char)code == 'x' || (unsigned char)code == 'X') {
            ac[px->n0++] = code;
        }
    }
    px->s = (unsigned char *)&ac[px->n0];
    func_802BDDE0_de(px, (unsigned char)code);
    return;
case_e:
    px->v.ld = px->qual == 'L' ? ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(double) + 3) & -4); *(double *)__unbake_slot; }) : ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; double item; }) - sizeof(double)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(double) + 3) & -4); *(double *)__unbake_slot; });
    if ((((unsigned short *)&(px->v.ld))[0] & 0x8000))
        ac[px->n0++] = '-';
    else if (px->flags & 2)
        ac[px->n0++] = '+';
    else if (px->flags & 1)
        ac[px->n0++] = ' ';
    px->s = (unsigned char *)&ac[px->n0];
    func_802BE0C0_de(px, (unsigned char)code);
    return;
case_n:
    if (px->qual == 'h') {
        *({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; unsigned short * item; }) - sizeof(unsigned short *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; unsigned short * item; }) - sizeof(unsigned short *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(unsigned short *) + 3) & -4); *(unsigned short * *)__unbake_slot; }) = px->nchar;
    } else if (px->qual == 'l') {
        *({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; unsigned long * item; }) - sizeof(unsigned long *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; unsigned long * item; }) - sizeof(unsigned long *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(unsigned long *) + 3) & -4); *(unsigned long * *)__unbake_slot; }) = px->nchar;
    } else if (px->qual == 'L') {
        *({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; unsigned long long * item; }) - sizeof(unsigned long long *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; unsigned long long * item; }) - sizeof(unsigned long long *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(unsigned long long *) + 3) & -4); *(unsigned long long * *)__unbake_slot; }) = px->nchar;
    } else {
        *({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; unsigned int * item; }) - sizeof(unsigned int *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; unsigned int * item; }) - sizeof(unsigned int *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(unsigned int *) + 3) & -4); *(unsigned int * *)__unbake_slot; }) = px->nchar;
    }
    return;
case_p:
    px->v.ll = (long)({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; void * item; }) - sizeof(void *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; void * item; }) - sizeof(void *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(void *) + 3) & -4); *(void * *)__unbake_slot; });
    px->s = (unsigned char *)&ac[px->n0];
    func_802BDDE0_de(px, 'x');
    return;
case_s:
    px->s = ({ va_list *__unbake_cursor = &(*pap); char *__unbake_slot = (char *)(((unsigned int)*__unbake_cursor + ((sizeof(struct { char pad; unsigned char * item; }) - sizeof(unsigned char *)) > 4 ? 8 : 4) - 1) & -((sizeof(struct { char pad; unsigned char * item; }) - sizeof(unsigned char *)) > 4 ? 8 : 4)); *__unbake_cursor = __unbake_slot + ((sizeof(unsigned char *) + 3) & -4); *(unsigned char * *)__unbake_slot; });
    px->n1 = func_802BD400_de(px->s);
    if (px->prec >= 0 && px->prec < px->n1) {
        px->n1 = px->prec;
    }
    return;
case_percent:
    ac[px->n0++] = '%';
    return;
case_default:
    ac[px->n0++] = code;
}
