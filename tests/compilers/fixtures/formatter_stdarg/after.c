#include <stdarg.h>
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
    ac[px->n0++] = va_arg(*pap, int);
    return;
case_d:
    if (px->qual == 'l') {
        px->v.ll = va_arg(*pap, long);
    } else if (px->qual == 'L') {
        px->v.ll = va_arg(*pap, long long);
    } else {
        px->v.ll = va_arg(*pap, int);
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
        px->v.ll = va_arg(*pap, long);
    } else if (px->qual == 'L') {
        px->v.ll = va_arg(*pap, long long);
    } else {
        px->v.ll = va_arg(*pap, int);
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
    px->v.ld = px->qual == 'L' ? va_arg(*pap, double) : va_arg(*pap, double);

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
        *va_arg(*pap, unsigned short *) = px->nchar;
    } else if (px->qual == 'l') {
        *va_arg(*pap, unsigned long *) = px->nchar;
    } else if (px->qual == 'L') {
        *va_arg(*pap, unsigned long long *) = px->nchar;
    } else {
        *va_arg(*pap, unsigned int *) = px->nchar;
    }
    return;
case_p:
    px->v.ll = (long)va_arg(*pap, void *);
    px->s = (unsigned char *)&ac[px->n0];
    func_802BDDE0_de(px, 'x');
    return;
case_s:
    px->s = va_arg(*pap, unsigned char *);
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
