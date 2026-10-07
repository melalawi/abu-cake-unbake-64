#include "common/types_06e4f7ef1f9e.h"
#include "span_1000/code_80217388.h"
#include "span_C76B0/data.h"
#include "types.h"

typedef struct WeaponMenuSetup {
    Menu menu;
    s32 initialized;
    s32 held;
    s32 category;
    s32 dirty;
} WeaponMenuSetup;

extern void *D_800CB2EC[];
extern s32 func_8022EB0C_de(void *player, s32 weapon);

/* Rebuild the eight menu classes from the player's 22 weapon slots. */
void func_802181FC_de(void *object, s32 held, void *actor)
{
    WeaponMenuSetup *setup = object;
    SharedPlayer *player = actor;
    Slot_func_80217928_de *entry;
    s32 i;
    s32 weapon;
    s32 category;
    s32 enabled;

    setup->dirty = 0;
    setup->held = held;
    setup->menu.cursor = -1;
    for (i = 0; i < 8; i++) {
        entry = &setup->menu.slots[i];
        for (weapon = 0; weapon < 22; weapon++) {
            if (player->views5E8.view602_14.slots[weapon].pad1 == i) {
                break;
            }
        }
        if (weapon < 22) {
            category = setup->category;
            if (category == -1) {
                enabled = 1;
            } else if (category == 3 || category == 7) {
                enabled = i == category;
            } else if (category == 4) {
                enabled = i == 4;
            } else {
                enabled = i == category || i == category + 1;
            }
            if (enabled) {
                entry->weapon = weapon;
                if (player->views5E8.view62E_13.unk62E == weapon) {
                    setup->menu.cursor = i;
                }
                entry->info = D_800CB2EC[weapon];
                if (func_8022EB0C_de(actor, weapon) != 0) {
                    entry->owned = 1;
                } else {
                    entry->owned = 0;
                }
                entry->angle = (f32)i * D_800C2248_de;
                entry->y = 0;
                entry->scale = D_800C913C_de;
                continue;
            }
        }
        entry->weapon = -1;
        entry->owned = 0;
    }
    setup->initialized = 1;
}
