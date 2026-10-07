#include "common/types_8fd754e1e915.h"
#include "span_1000/code_8021CD70.h"
#include "span_C76B0/data.h"

#include "types.h"
#include "common/types_8a8189af7b05.h"
/* Updates player movement, position, and movement sound selection. */
/* Complete legacy movement views from origin/legacy:src/func_802251B8.c. */
typedef struct func_802251B8_S1 func_802251B8_S1;
typedef struct func_802251B8_S2 func_802251B8_S2;
typedef struct func_802251B8_S3 func_802251B8_S3;
typedef struct func_802251B8_S4 func_802251B8_S4;
typedef struct func_802251B8_S5 func_802251B8_S5;
typedef union func_802251B8_S2_U724 { s32 v0; f32 v1; } func_802251B8_S2_U724;
typedef union func_802251B8_S2_U728 { s8 v0; f32 v1; } func_802251B8_S2_U728;
struct func_802251B8_S1 {
    char pad0[0x8];
    Vec3 pos;
    char pad10[0x4];
    func_802251B8_S3 * unk18;
    f32 unk1C;
    char pad1C[0x4];
    f32 unk24;
    char pad24[0x10];
    s32 unk38;
    char pad38[0x30];
    f32 unk6C;
};
struct func_802251B8_S2 {
    char pad0[0x38];
    s32 unk38;
    char pad38[0xA8];
    u16 unkE4;
    char padE4[0x4F2];
    func_802251B8_S5 * unk5D8;
    char pad5D8[0x4];
    s32 unk5E0;
    char pad5E0[0x74];
    f32 unk658;
    char pad658[0x10];
    f32 unk66C;
    char pad66C[0x2C];
    f32 unk69C;
    f32 unk6A0;
    f32 unk6A4;
    f32 unk6A8;
    char pad6A8[0x4];
    s32 unk6B0;
    char pad6B0[0xC];
    f32 unk6C0;
    f32 unk6C4;
    char pad6C4[0x18];
    f32 unk6E0;
    f32 unk6E4;
    f32 unk6E8;
    f32 unk6EC;
    f32 unk6F0;
    char pad6F0[0x30];
    func_802251B8_S2_U724 unk724;
    func_802251B8_S2_U728 unk728;
    f32 unk72C;
    char pad72C[0xB8];
    s32 unk7E8;
    char pad7E8[0x80];
    s32 unk86C;
    char pad86C[0x968];
    f32 unk11D8;
    char pad11D8[0x1D8];
    s32 *unk13B4;
    char pad13B4[0x308];
    f32 unk16C0;
};
struct func_802251B8_S3 {
    char pad0[0x14];
    s32 unk14;
};
struct func_802251B8_S4 {
    char pad0[0x1D];
    u8 unk1D;
    char pad1D[0x60E];
    s32 unk62C;
};
struct func_802251B8_S5 {
    char pad0[0x8F];
    u8 unk8F;
};


typedef struct { char pad[0x1D]; u8 flag; char pad1E[0x60E]; s32 unk62C; } MovementSettings;
typedef struct { char pad[6]; struct { s16 id; s16 pad; } sounds[1]; } MovementSounds;






extern const f32 D_800C2A64_de;
extern const f32 D_800C2A68_de;



























































s32 func_802227F4_de(void *, void *, s32);
int func_8024E62C_de(void *);
f32 func_802746A0_de(f32, f32, f32);
f32 func_802747A0_de(f32, f32);
void func_80274870_de(f32 *, f32, f32);
float func_802B6560_de(float);
float func_802B7130_de(float);
void func_8025DE54_de(s32, Vec3, s32, s32);
extern MovementSettings D_80142208_de;




extern s32 D_800C9AEC_de;                          /* unable to generate initializer: unknown type */

extern f32 D_800CD738[2];

                          /* const */

                          /* const */

             

/* const */


void func_802251DC_de(struct func_802251B8_S2 *arg0, struct func_802251B8_S1 *arg1) {
    f32 temp_f0;
    f32 temp_f0_2;
    f32 temp_f1;
    f32 temp_f1_2;
    f32 temp_f1_3;
    f32 temp_f1_4;
    f32 temp_f20;
    f32 temp_f20_2;
    f32 temp_f20_3;
    f32 temp_f2;
    f32 temp_f2_2;
    f32 temp_f2_3;
    f32 temp_f2_4;
    f32 temp_f3;
    f32 temp_f3_2;
    f32 var_a1;
    f32 var_a2;
    f32 var_f0;
    f32 var_f0_3;
    f32 var_f0_4;
    f32 var_f0_5;
    f32 var_f1;
    f32 var_f1_2;
    s32 temp_v1;
    s32 var_v0;
    MovementSettings *settings;
    f32 deltaX, deltaZ, deltaY, cosine;
    f32 recoil;
    
    s32 actorId;
    s32 var_v0_2;
    s32 var_v0_3;
    s32 var_v1;

    if (!(arg1->unk38 & 0x100)) {
        arg0->unk724.v0 = 0;
        arg0->unk16C0 = 0.0f;
        if (func_8024E62C_de(arg1) == 0) {
            recoil = D_800C2A58_de;
            arg0->unk6E8 = (f32) (arg0->unk6E8 - (func_802B7130_de(arg1->unk6C) * recoil));
            arg0->unk6F0 = (f32) (arg0->unk6F0 - (func_802B6560_de(arg1->unk6C) * recoil));
        }
        func_802227F4_de(arg0, arg1, 5);
        return;
    }
    temp_f0 = arg0->unk69C;
    if ((temp_f0 > D_800C2A5C_de) || (temp_f0 < D_800C2A5C_de)) {
        arg0->unk16C0 = (f32) (arg0->unk16C0 + (temp_f0 * D_800C2A60_de));
    }
    temp_f0_2 = arg0->unk16C0;
    if (temp_f0_2 > D_800C2A64_de) {
        arg0->unk16C0 = D_800C2A64_de;
    } else if (temp_f0_2 < D_800C2A68_de) {
        arg0->unk16C0 = D_800C2A68_de;
    }
    
    func_80274870_de(&arg0->unk728.v1, ((struct func_802251B8_S2 *)arg0)->unk16C0 * D_800C2A6C_de, 0.5f);
    var_f1_2 = -arg0->unk6A0 * D_800C2A70_de;
    var_f0 = D_800C2A74_de;
    if ((var_f1_2 < D_800C2A74_de) || (var_f0 = D_800C2A78_de, (var_f1_2 > D_800C2A78_de))) {
        var_f1_2 = var_f0;
    }
    func_80274870_de(&arg0->unk724.v1, var_f1_2 * D_800C2A7C_de, 0.0625f);
    if ((arg0->unk7E8 == 0) && !(arg0->unk6E4 > D_800C2A80_de)) {
        if (!(arg1->unk38 & 0xC0000)) {
            if (arg1->unk18->unk14 & 2) {
                if ((arg0->unk6B0 & 0x10) && (arg0->unk11D8 <= 0.0f)) {
                    func_802227F4_de(arg0, arg1, 5);
                    var_v0 = 1;
                } else { var_v0 = 0; }
            } else { var_v0 = 0; }
        } else { var_v0 = 0; }
    } else { var_v0 = 0; }
    if (var_v0 != 0) {
        temp_f1 = arg1->unk6C + arg0->unk728.v1;
        arg1->unk6C = temp_f1;
        temp_f2 = arg0->unk728.v1;
        if (temp_f2 >= D_800C2A84_de) {
            temp_f0_2 = temp_f1 + D_800C2A88_de;
            goto block_25;
        }
        if (temp_f2 <= D_800C2A8C_de) {
            temp_f0_2 = temp_f1 - D_800C2A90_de;
block_25:
            arg1->unk6C = temp_f0_2;
        }
        temp_f20 = func_802B7130_de(arg1->unk6C + D_800C2A94_de);
        temp_f1_2 = arg1->unk24 + (func_802B6560_de(arg1->unk6C + D_800C2A94_de) * D_800C2A98_de);
        arg1->unk1C = (f32) (arg1->unk1C + (temp_f20 * D_800C2A98_de));
        arg1->unk24 = temp_f1_2;
        temp_f1_3 = arg0->unk728.v1;
        if (temp_f1_3 >= D_800C2A9C_de) {
            var_f0_3 = arg1->unk6C + D_800C2AA0_de;
            goto block_30;
        }
        if (temp_f1_3 <= D_800C2AA4_de) {
            var_f0_3 = arg1->unk6C - D_800C2AA8_de;
block_30:
            arg1->unk6C = var_f0_3;
        }
        arg0->unk728.v1 = 0.0f;
        arg0->unk16C0 = 0.0f;
        return;
    }
    temp_f2_2 = arg0->unk6E0 * arg0->unk6C0 * func_802B7130_de(arg0->unk658 * D_800C2AAC_de);
    temp_f3 = arg0->unk6A8;
    if ((temp_f3 > 0.0f) || (temp_f3 < 0.0f)) {
        func_80274870_de(&arg0->unk72C, temp_f2_2, 0.25f);
    } else {
        func_80274870_de(&arg0->unk72C, 0.0f, 0.9f);
    }
    temp_f2_3 = arg0->unk6A8;
    var_v1 = 1;
    if (!(temp_f2_3 > 0.0f)) {
        var_v1 = 0;
    }
    var_v0_2 = 1;
    if (!(temp_f2_3 < 0.0f)) {
        var_v0_2 = 0;
    }
    temp_f20_2 = arg0->unk6C0;
    if ((var_v1 != 0) || (var_v0_2 != 0)) {
        var_a1 = temp_f2_3 * D_800C2AB0_de;
        if (temp_f2_3 < 0.0f) {
            var_f0_4 = -temp_f2_3 * D_800C2AB4_de;
        } else {
            var_f0_4 = temp_f2_3 * D_800C2AB8_de;
        }
        arg0->unk6C0 = func_802746A0_de(arg0->unk6C0, var_a1, var_f0_4);
    } else {
        arg0->unk6C0 = func_802747A0_de(temp_f20_2, D_800C2ABC_de);
    }
    temp_f2_4 = arg0->unk6A4;
    if (temp_f2_4 != 0.0f) {
        var_a2 = temp_f2_4 * D_800C2AC0_de;
        if (temp_f2_4 < 0.0f) {
            var_f0_5 = -temp_f2_4 * D_800C2AC4_de;
        } else {
            var_f0_5 = temp_f2_4 * D_800C2AC8_de;
        }
        arg0->unk6C4 = func_802746A0_de(arg0->unk6C4, var_a2, var_f0_5);
    } else {
        arg0->unk6C4 = func_802747A0_de(arg0->unk6C4, D_800C2ACC_de);
    }
    if ((arg0->unk6C0 == 0.0f) && (arg0->unk6C4 == 0.0f)) {
        arg0->unk658 = 0.0f;
        if (temp_f20_2 != 0.0f) {
            arg0->unk6E0 = (f32) -arg0->unk6E0;
        }
    } else {
        temp_f3_2 = arg0->unk658;
        if (temp_f3_2 > D_800C2AD0_de) {
            arg0->unk6E0 = (f32) -arg0->unk6E0;
            arg0->unk658 = (f32) (temp_f3_2 - D_800C2AD0_de);
            arg0->unk6C0 = (f32) (arg0->unk6C0 * D_800C2AD4_de);
            arg0->unk6C4 = (f32) (arg0->unk6C4 * D_800C2AD4_de);
            settings = &D_80142208_de;
            if ((settings->flag != 0) && ((arg0->unk5D8->unk8F != 1) || (settings->unk62C == 0))) {
                temp_v1 = arg0->unk38;
                if (temp_v1 & 0x200) {
                    func_8025DE54_de(D_800C9FD6, arg1->pos, 0, -1);
                } else if (temp_v1 & 0x400) {
                    func_8025DE54_de(D_800C9FDA, arg1->pos, 0, -1);
                } else if (temp_v1 & 0x800) {
                    func_8025DE54_de(D_800C9FDE, arg1->pos, 0, -1);
                } else {
                    func_8025DE54_de(((MovementSounds *)&D_800CAB88_eu)->sounds[arg0->unk5E0].id, arg1->pos, 0, -1);
                }
            }
        }
    }
    temp_f20_3 = func_802B7130_de(arg1->unk6C - D_800C2AD8_de);
    cosine = func_802B6560_de(arg1->unk6C - D_800C2AD8_de);
    
    deltaX = temp_f20_3 * ((struct func_802251B8_S2 *)arg0)->unk6C4 * ((struct func_802251B8_S2 *)arg0)->unk66C;
    deltaZ = cosine * ((struct func_802251B8_S2 *)arg0)->unk6C4 * ((struct func_802251B8_S2 *)arg0)->unk66C;
    deltaY = arg0->unk6C0 * (*D_800CD738 * D_800C2ADC_de);
    deltaX = arg0->unk6E8 + deltaX;
    deltaZ = arg0->unk6F0 + deltaZ;
    deltaY = arg0->unk6EC + deltaY;
    actorId = arg0->unkE4;
    
    ((struct func_802251B8_S2 *)arg0)->unk6E8 = deltaX;
    ((struct func_802251B8_S2 *)arg0)->unk6F0 = deltaZ;
    ((struct func_802251B8_S2 *)arg0)->unk6EC = deltaY;
    if (actorId == D_800C922C) {
        var_v0_3 = 0xA46;
    } else if (arg0->unk6C0 > 0.0f) {
        
        var_v0_3 = (s32)arg0->unk13B4;
        if (var_v0_3 != (s32)&D_800C9AEC_de) {
            var_v0_3 = 0xA46;
        } else {
            var_v0_3 = 0x5E28;
        }
    } else {
        var_v0_3 = 0xA3C;
        if (arg0->unk13B4 == &D_800C9AEC_de) {
            var_v0_3 = 0x5E27;
        }
    }
    
    ((struct func_802251B8_S2 *)arg0)->unk86C = var_v0_3;
}

