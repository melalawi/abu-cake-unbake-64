#include "types.h"
#include "common/types_06e4f7ef1f9e.h"
#include "span_1000/code_802106E0.h"

extern s32 func_802744D4_de(void);
extern void func_80209988_de(void *object);

void func_80212544_eu(void *arg0) {
    void *level1 = ((Shared_Actor *)arg0)->entity;
    void *inner = ((func_80212828_S5 *)(level1))->unk1454;
    s32 r1, r2, r3;

    ((func_80212450_S3 *)(inner))->unk220 = 0;

    r1 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2D8 = r1 % 4 + 2;

    r2 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2DC = r2 % 2;

    r3 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2E0 = r3 % 32400 + 0x2710;

    func_80209988_de(inner);

    ((func_80212450_S3 *)(inner))->unk318 = 0;
    ((func_80212450_S3 *)(inner))->unk31C = 0;
    ((func_80212450_S3 *)(inner))->unk320 = -1;
}

/* Native resident constant storage; absolute access symbols retain their addresses. */
#if defined(VERSION_US)
const float unbake_rodata_800C4060_4 = 0.333333343f;
const float unbake_rodata_800C4064_4 = 0.5f;
const double unbake_rodata_800C4068_8 = 4294967296.0;
const float unbake_rodata_800C4070_4 = 1.0f;
const float unbake_rodata_800C4074_4 = 1.0f;
#elif defined(VERSION_US_REV1)
const float unbake_rodata_800C91F8_4 = 1.0f;
#elif defined(VERSION_EU)
const double unbake_rodata_800C4170_8 = 4294967296.0;
#elif defined(VERSION_EU_X)
const float unbake_rodata_800C4120_4 = 2.14748365e+09f;
const float unbake_rodata_800C4124_4 = 0.00787401572f;
const float unbake_rodata_800C4128_4 = 102.399994f;
const float unbake_rodata_800C412C_4 = 0.5f;
#elif defined(VERSION_DE)
const double unbake_rodata_800C40D0_8 = 4294967296.0;
const double unbake_rodata_800C40D8_8 = 4294967296.0;
const double unbake_rodata_800C40E0_8 = 4294967296.0;
const double unbake_rodata_800C40E8_8 = 4294967296.0;
const double unbake_rodata_800C40F0_8 = 4294967296.0;
#endif
