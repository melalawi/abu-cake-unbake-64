#ifndef REAL_OWNER_H
#define REAL_OWNER_H
#include "types.h"
typedef struct Header_func_8025B5F0_de Header_func_8025B5F0_de;
struct Header_func_8025B5F0_de {
    char pad[0x102];
    s16 local;
};
typedef struct Owner_func_8025B5F0_de Owner_func_8025B5F0_de;
struct Owner_func_8025B5F0_de {
    char pad[0x84];
    char sound[0x58];
    s16 samples[20];
    char pad2[(0x104 - 0xDC) - 40];
    s32 key;
};
#endif
