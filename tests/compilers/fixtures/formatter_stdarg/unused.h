#ifndef FORMATTER_UNUSED_H
#define FORMATTER_UNUSED_H
typedef unsigned int size_t;
typedef char *va_list;
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

#endif
