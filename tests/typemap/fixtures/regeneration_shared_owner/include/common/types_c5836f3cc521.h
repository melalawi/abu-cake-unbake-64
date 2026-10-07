#ifndef UNBAKE_COMMON_TYPES_C5836F3CC521_H
#define UNBAKE_COMMON_TYPES_C5836F3CC521_H
#include "../types.h"
struct QueryResult;

/* unbake published declaration: published_60778d2a829571a8ff1f9275 */
struct QueryResult { int unused[2]; int kind; };


struct Shape_func_800E6970_us;

/* unbake published declaration: published_cd9b84adb68e2ec87de99591 */
struct Shape_func_800E6970_us {
    unsigned char padding_0[8];
    void * field_8;
    unsigned char padding_C[20];
    float field_20;
};


struct Shape_func_8011B9E0;

/* unbake published declaration: published_0756070c6fca00ba6039ab71 */
struct Shape_func_8011B9E0 {
    int field_0;
};


struct Shape_func_8007AAC0;

/* unbake published declaration: published_1181f3fa9fd9ec97bd9480ad */
struct Shape_func_8007AAC0 {
    int field_0;
    int field_4;
    unsigned char padding_8[456];
    int field_1D0;
    unsigned char padding_1D4[48];
    int field_204;
    unsigned char padding_208[12];
    unsigned char field_214;
    unsigned char field_215;
    unsigned char field_216;
    unsigned char field_217;
    int field_218;
};


struct QueryBox;

/* unbake published declaration: published_3d1e2993526d7186ea4bab56 */
struct QueryBox { short values[9]; };


struct QueryBox;

/* unbake published declaration: published_2324a3b162fd64225be1237f */
typedef struct QueryBox QueryBox;


struct func_800E9894_S3_Shared800EA1FC;

/* unbake published declaration: published_b2ba3a2faf3e1af012350de1 */
struct func_800E9894_S3_Shared800EA1FC {
    char pad0[0x4];
    u8 unk4;
    char pad5[0x9C - 0x5];
    u8 unk9C;
    char pad9D[0xCC - 0x9D];
    u8 unkCC;
};


struct func_800E9894_S1_Shared800EA1FC;

struct func_800E9894_S3_Shared800EA1FC;

/* unbake published declaration: published_2708dcf4243639cc076c04fc */
struct func_800E9894_S1_Shared800EA1FC {
    void * unk0;
    void * unk4;
    struct func_800E9894_S3_Shared800EA1FC * unk8;
    void * unkC;
    void * unk10;
    s32 unk14;
    s16 unk18;
    s16 unk1A;
    u8 unk1C;
    char pad1C[0x40 - 0x1C - sizeof(u8)];
    s32 unk40;
    s32 unk44;
};


struct func_800A52AC_S1_Shared800A51E8;

/* unbake published declaration: published_31b67f3424806b89296a5e12 */
struct func_800A52AC_S1_Shared800A51E8 {
    char pad0[0x1A6];
    u8 unk1A6;
    char pad1A6[0x71];
    s32 unk218;
    s32 unk21C;
    s8 unk220;
    char pad220[0x1F];
    s8 unk240;
};


struct Func_80096784_Value_Shared80096784;

/* unbake published declaration: published_54baaea733257b5566411ee4 */
struct Func_80096784_Value_Shared80096784 {
    u16 value;
    char pad_2[4];
};


struct Shape_func_800B8804_us;

/* unbake published declaration: published_e35999cbb490e5ae38fc178e */
struct Shape_func_800B8804_us {
    void * field_0;
};


/* unbake published declaration: published_7836b84287ac3397905b4f57 */
extern void * D_801257D0;


struct Func_80096784_Record_Shared80096784;

/* unbake published declaration: published_893bea6e3b3d5cde6401892f */
struct Func_80096784_Record_Shared80096784 {
    u16 value;
    u8 mode;
    u8 channel;
    char pad_4[2];
};


struct func_800E9894_S1_Shared800EA1FC;

/* unbake published declaration: published_b5643416a310f332554631ff */
typedef struct func_800E9894_S1_Shared800EA1FC func_800E9894_S1_Shared800EA1FC;


struct func_80078D90_S2_Shared80078D90;

/* unbake published declaration: published_cdc53a8f20d0da02a1807d59 */
typedef struct func_80078D90_S2_Shared80078D90 func_80078D90_S2_Shared80078D90;


struct func_80078D90_S2_Shared80078D90;

/* unbake published declaration: published_d1467f6d473c29ff5523d9e1 */
struct func_80078D90_S2_Shared80078D90 {
    char pad0[4];
    s32 unk4;
};


struct func_800A52AC_S1_Shared800A51E8;

/* unbake published declaration: published_f8eed9e799e6cf3ef884444a */
typedef struct func_800A52AC_S1_Shared800A51E8 func_800A52AC_S1_Shared800A51E8;


struct Shape_func_800E3F90_us;

struct Shape_func_800E3F90_us {
    void * field_0;
    unsigned char padding_4[8];
    void * field_C;
    unsigned char padding_10[8];
    unsigned char unknown_18[2];
};

struct Shape_func_800E73BC_us;

struct Shape_func_800E73BC_us {
    unsigned char padding_0[8];
    int field_8;
};

struct Shape_func_801125F0_us;

struct Shape_func_801125F0_us {
    unsigned char padding_0[5];
    unsigned char field_5;
    unsigned char field_6;
    unsigned char field_7;
    unsigned char field_8;
    unsigned char field_9;
    unsigned char padding_A[2];
    int field_C;
    unsigned char padding_10[4];
    int field_14;
    unsigned short field_18;
    unsigned short field_1A;
    int field_1C;
    unsigned char padding_20[4];
    int field_24;
    unsigned char padding_28[4];
    unsigned char unknown_2C[4];
    unsigned char padding_30[4];
    void * field_34;
    unsigned char padding_38[4];
    int field_3C;
    unsigned char padding_40[20];
    int field_54;
    unsigned char padding_58[4];
    int field_5C;
};

struct Shape_func_8011D6E0_us;

struct Shape_func_8011D6E0_us {
    unsigned char padding_0[28];
    int field_1C;
};
#endif
