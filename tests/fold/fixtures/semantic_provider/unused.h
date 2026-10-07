#ifndef REAL_UNUSED_H
#define REAL_UNUSED_H
#include "types.h"
#include "span/owner.h"
typedef struct RecordD90 RecordD90;
typedef struct SlotCC SlotCC;
struct SlotCC {
    s32 index;
    s32 state;
    s32 used;
    char pad0[4];
    s32 key;
    char pad1[0x26];
    s16 id;
    char pad1b[0x14];
    s32 flag;
    char pad2[0x50];
    s32 mode;
    s32 value;
    s32 active;
    struct Owner_func_8025B5F0_de *owner;
    char pad3[0x18];
};
struct RecordD90 {
    struct Header_func_8025B5F0_de *header;
    SlotCC slots[17];
};
#endif
