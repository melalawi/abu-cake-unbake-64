#include "types.h"
#include "common/unused.h"

extern size_t func_802BD400_de(const unsigned char *s);
extern void func_802BDDE0_de(_Pft *px, unsigned char code);
extern void func_802BE0C0_de(_Pft *px, unsigned char code);

void func_802BD974_de(_Pft *px, va_list *pap, int code, unsigned char *ac)
{
    px->n0 = px->nz0 = px->n1 = px->nz1 = px->n2 =
        px->nz2 = 0;

    /* Standard printf dispatch reconstructed from the conversion arms;
     * original table identity/entry audit remains pending. */
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
    ac[px->n0++] = ({ char *__slot = (char *)(((int)(*pap) + sizeof(int) - 1) & -(int)sizeof(int)); (*pap) = __slot + sizeof(int); *(int *)__slot; });
    return;
case_d:
    if (px->qual == 'l') {
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(long) - 1) & -(int)sizeof(long)); (*pap) = __slot + sizeof(long); *(long *)__slot; });
    } else if (px->qual == 'L') {
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(long long) - 1) & -(int)sizeof(long long)); (*pap) = __slot + sizeof(long long); *(long long *)__slot; });
    } else {
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(int) - 1) & -(int)sizeof(int)); (*pap) = __slot + sizeof(int); *(int *)__slot; });
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
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(long) - 1) & -(int)sizeof(long)); (*pap) = __slot + sizeof(long); *(long *)__slot; });
    } else if (px->qual == 'L') {
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(long long) - 1) & -(int)sizeof(long long)); (*pap) = __slot + sizeof(long long); *(long long *)__slot; });
    } else {
        px->v.ll = ({ char *__slot = (char *)(((int)(*pap) + sizeof(int) - 1) & -(int)sizeof(int)); (*pap) = __slot + sizeof(int); *(int *)__slot; });
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
    px->v.ld = px->qual == 'L' ? ({ char *__slot = (char *)(((int)(*pap) + sizeof(double) - 1) & -(int)sizeof(double)); (*pap) = __slot + sizeof(double); *(double *)__slot; }) : ({ char *__slot = (char *)(((int)(*pap) + sizeof(double) - 1) & -(int)sizeof(double)); (*pap) = __slot + sizeof(double); *(double *)__slot; });

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
        *({ char *__slot = (char *)(((int)(*pap) + sizeof(unsigned short *) - 1) & -(int)sizeof(unsigned short *)); (*pap) = __slot + sizeof(unsigned short *); *(unsigned short * *)__slot; }) = px->nchar;
    } else if (px->qual == 'l') {
        *({ char *__slot = (char *)(((int)(*pap) + sizeof(unsigned long *) - 1) & -(int)sizeof(unsigned long *)); (*pap) = __slot + sizeof(unsigned long *); *(unsigned long * *)__slot; }) = px->nchar;
    } else if (px->qual == 'L') {
        *({ char *__slot = (char *)(((int)(*pap) + sizeof(unsigned long long *) - 1) & -(int)sizeof(unsigned long long *)); (*pap) = __slot + sizeof(unsigned long long *); *(unsigned long long * *)__slot; }) = px->nchar;
    } else {
        *({ char *__slot = (char *)(((int)(*pap) + sizeof(unsigned int *) - 1) & -(int)sizeof(unsigned int *)); (*pap) = __slot + sizeof(unsigned int *); *(unsigned int * *)__slot; }) = px->nchar;
    }
    return;
case_p:
    px->v.ll = (long)({ char *__slot = (char *)(((int)(*pap) + sizeof(void *) - 1) & -(int)sizeof(void *)); (*pap) = __slot + sizeof(void *); *(void * *)__slot; });
    px->s = (unsigned char *)&ac[px->n0];
    func_802BDDE0_de(px, 'x');
    return;
case_s:
    px->s = ({ char *__slot = (char *)(((int)(*pap) + sizeof(unsigned char *) - 1) & -(int)sizeof(unsigned char *)); (*pap) = __slot + sizeof(unsigned char *); *(unsigned char * *)__slot; });
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
