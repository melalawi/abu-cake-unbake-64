#ifndef HEADER_SPLIT_ACTOR_H
#define HEADER_SPLIT_ACTOR_H
#include "vec.h"
typedef struct Actor_func_80214624_de Actor_func_80214624_de;
struct Actor_func_80214624_de {
    char p0[8];
    Vec3 pos;
    s32 unk14;
    s32 *unk18;
    char p1C[0x50];
    f32 unk6C;
    char p70[0x74];
    u16 unkE4;
    char pE6[0x1A];
    s32 unk100;
    char p104[0xD4];
    struct Runtime *unk1D8;
    char p1DC[0x104];
    s32 unk2E0;
};
#endif
