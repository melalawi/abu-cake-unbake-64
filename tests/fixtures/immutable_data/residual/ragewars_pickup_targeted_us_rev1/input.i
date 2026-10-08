typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef struct ResidentPickupPrefix {
    const char **labels;
    s16 itemId;
    s16 soundId;
    s16 effectId;
    u16 flags;
} ResidentPickupPrefix;
typedef s32 (*ResidentPickupHandler)(void *actor, void *entry);
typedef s32 (*ResidentTargetedPickupHandler)(void *actor, void *entry, s32 target);
typedef struct ResidentPowerupDescriptor {
    ResidentPickupPrefix common;
    s16 slotIndex;
    s16 powerupType;
    s32 amount;
    ResidentPickupHandler handler;
} ResidentPowerupDescriptor;
typedef struct ResidentWeaponDescriptor {
    ResidentPickupPrefix common;
    s16 weaponSlot;
    s16 ammoAmount;
    ResidentPickupHandler handler;
} ResidentWeaponDescriptor;
typedef struct ResidentHealthDescriptor {
    ResidentPickupPrefix common;
    s32 amount;
    s32 value;
    ResidentPickupHandler handler;
} ResidentHealthDescriptor;
typedef struct ResidentLifeDescriptor {
    ResidentPickupPrefix common;
    u16 amount;
    u16 flags;
    ResidentPickupHandler handler;
} ResidentLifeDescriptor;
typedef struct ResidentSpecialDescriptor {
    ResidentPickupPrefix common;
    ResidentPickupHandler handler;
} ResidentSpecialDescriptor;
typedef struct ResidentTargetedDescriptor {
    ResidentPickupPrefix common;
    ResidentTargetedPickupHandler handler;
} ResidentTargetedDescriptor;
typedef struct ResidentAmmoDescriptor {
    ResidentPickupPrefix common;
    s16 ammoAmount[3];
    u16 flags;
    ResidentPickupHandler handler;
} ResidentAmmoDescriptor;
extern const char *D_800D7070[103];
extern s32 func_802ACE38_de(void *, void *, s32);
ResidentTargetedDescriptor D_800CE0E0[16] = {
    {{&D_800D7070[9], 1800, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[10], 1801, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[11], 1802, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[12], 1803, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[14], 4380, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[15], 4381, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[16], 4382, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[17], 4383, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[18], 4384, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[19], 4400, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[20], 4401, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[21], 4402, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[22], 4403, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[23], 4404, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[24], 4500, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
    {{&D_800D7070[13], 1807, 1, 299, 0}, (ResidentTargetedPickupHandler)((char *)func_802ACE38_de - 0x80000000U)},
};
