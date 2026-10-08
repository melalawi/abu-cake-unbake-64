#include "resident_pickup_descriptors.h"

extern s32 func_802ACE38_de(void *, void *, s32);

/* 16 descriptors traversed by 802AB400/802AB6EC at stride 16.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D3FC0..D40C0. */
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
