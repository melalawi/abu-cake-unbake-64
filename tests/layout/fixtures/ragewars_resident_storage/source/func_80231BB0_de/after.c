#include "span_16E000/code_8042F988.h"
#include "span_C76B0/data.h"
#include "types.h"
#include "common/unused.h"
#include "common/types_06e4f7ef1f9e.h"

/* Starts a player's action: when the current action is not allowed the idle state is refreshed; a player
 * flagged 0x4000 with no cooldown, no lock and the ammo option enabled checks its weapon's ammo, playing sound
 * 0xD4D and reporting the empty weapon when it is out, and takes action 8 when it may fire; otherwise the
 * default action for the player's state is started unless func_802301F4_de handled the request or the actor is
 * flagged 0x400. */

#if defined(VERSION_EU) || defined(VERSION_EU_X)
extern s32 D_800E0D44[], D_800DCC68[];

#else
#endif
extern WeaponActionRecord D_800C9698[];
extern MatchRewardsRecord D_800FEB00[];

extern s32 D_800D30BC;

extern char D_80140F80[];
extern char D_80140FC8;
extern s32 func_80222AA4_de(void *, s16);
extern s16 func_8022F96C_de(void *);
extern s32 func_8022F55C_de(void *, s16);
extern s32 func_8025DF34_de(s32);
extern s32 func_8022A5A0_de(void *, void *);
extern void func_80239908_de(void *, void *, s32, s32, f32);
extern s32 func_802301F4_de(void *, void *);
extern void func_80214178_de(void *, void *, s32);

static inline s32 can_fire(char *player) {
    s32 ammo;

    if (((SharedPlayer *)(player))->views5E8.view11D8_147.unk11D8 > 0.0f) {
        return 0;
    }
    if (((SharedPlayer *)(player))->views1450.view1450_0.unk1450 != 0) {
        return 1;
    }
    if (D_80142215 != 1) {
        return 1;
    }
    ammo = func_8022F55C_de(&D_800FEB00[((SharedPlayer *)(player))->views1C.view5D4_45.unk5D4], ((SharedPlayer *)(player))->views5E8.view62E_13.unk62E);
    if (ammo == 0) {
        func_8025DF34_de(0xD4D);
        if (((SharedPlayer *)(player))->views5DC.view5DC_0.unk5DC != 0) {
#if defined(VERSION_EU)
            func_80239908_de(&D_80140FC8, ((SharedPlayer *)(player))->views5DC.view5DC_0.unk5DC, ((D_800E0D44)[D_80152789]), func_8022A5A0_de(D_80140F80, player),
#elif defined(VERSION_EU_X)
            func_80239908_de(&D_80140FC8, ((SharedPlayer *)(player))->views5DC.view5DC_0.unk5DC, ((D_800DCC68)[D_80152789]), func_8022A5A0_de(D_80140F80, player),
#else
            func_80239908_de(&D_80140FC8, ((SharedPlayer *)(player))->views5DC.view5DC_0.unk5DC, (D_800D30BC), func_8022A5A0_de(D_80140F80, player),
#endif
                          D_800C2FB8_de);
        }
    }
    return ammo;
}

void func_80231BB0_de(void *actor, void *arg1) {
    char *player;
    s32 action;

    player = ((Shared_Actor *)(actor))->entity;
    action = D_800C9698[((SharedPlayer *)(player))->views5E8.view650_15.unk650].action;
    if (func_80222AA4_de(player, ((SharedPlayer *)(player))->views5E8.view62E_13.unk62E) == 0) {
        ((SharedPlayer *)(player))->views5E8.view770_91.unk770 = func_8022F96C_de(player);
        return;
    }
    if ((((SharedPlayer *)(player))->views5E8.view6B0_46.unk6B0 & 0x4000) && can_fire(player)) {
        func_80214178_de(actor, arg1, 8);
        return;
    }
    if (func_802301F4_de(actor, arg1) == 0 && !(((Shared_Actor *)(actor))->weaponFlags & 0x400)) {
        func_80214178_de(actor, arg1, action);
    }
}
