#ifndef FUNC_8021E2A0_DE_CLOSED_H
#define FUNC_8021E2A0_DE_CLOSED_H
#include "types.h"
struct Shared_Model { u16 unknown0; u16 flags; };
struct Shared_Placed { Vec3 pos; u32 unknownC; s16 kind; };
struct Shared_Scratch { union { struct { s32 damage[5]; } view0_0; struct { Vec3 to; } view0_1; } views0; };
struct Shared_StateInfo { u32 unknown0; void (*update)(void *, void *); s32 *flags; u32 unknownC[3]; };
struct Shared_Surface { u8 unknown0[0x28]; f32 timer; u8 unknown2C[0x18]; s32 flags; u32 unknown48; u16 item; u16 unknown4E; u16 sound; };
struct Shared_PickupDef { u32 unknown0; s32 flags; u8 unknown8[0x10]; f32 radius; u32 unknown1C; s32 item; };
struct Shared_Pickup { u32 unknown0[2]; Vec3 pos; u32 unknown14; struct Shared_PickupDef *def; };
struct Shared_CharInfo { u32 unknown0; u16 sound; u16 unknown6; s16 ammo; u16 unknownA; union { struct { s16 next_s; } viewC_0; struct { u16 next_u; } viewC_1; } viewsC; };
struct Shared_Floor { u8 unknown0[0xE8]; f32 y; };
struct Shared_Profile { u8 unknown0[0x81]; u8 team; u8 controllerFlag; u8 quantity; u8 unknown84[0xB]; u8 remote; u8 unknown90[4]; u8 counts; };
struct Shared_Hud { u8 unknown0[0x120]; s32 score; u16 unknown124; u16 bonus; };
struct Shared_Shadow { u32 unknown0[2]; Vec3 pos; };
struct Shared_Voice { u32 unknown0[4]; s32 bank; };

#endif
