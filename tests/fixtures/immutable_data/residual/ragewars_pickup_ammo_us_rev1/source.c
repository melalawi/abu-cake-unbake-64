#include "resident_pickup_descriptors.h"

extern s32 func_802ACFB0_de(void *, void *);

/* 16 descriptors traversed by 802AB400/802AB6EC at stride 24.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D40C0..D4240. */
ResidentAmmoDescriptor D_800CE1E0_de[16] = {
    {{&D_800D7070[102], 3033, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3034, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3035, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3036, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3037, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3038, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3039, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3040, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3041, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3042, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3043, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3044, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3045, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3046, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3047, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3048, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
};
