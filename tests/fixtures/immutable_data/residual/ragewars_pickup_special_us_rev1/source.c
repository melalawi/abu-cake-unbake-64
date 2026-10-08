#include "resident_pickup_descriptors.h"

extern s32 func_802ABBDC_de(void *, void *);

/* 3 descriptors traversed by 802AB400/802AB6EC at stride 16.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D3F90..D3FC0. */
ResidentSpecialDescriptor D_800CE0B0[3] = {
    {{&D_800D7070[25], 3030, 3598, 0, 0}, (ResidentPickupHandler)((char *)func_802ABBDC_de - 0x80000000U)},
    {{&D_800D7070[26], 3031, 710, 0, 0}, (ResidentPickupHandler)((char *)func_802ABBDC_de - 0x80000000U)},
    {{&D_800D7070[29], 3032, 3530, 0, 0}, (ResidentPickupHandler)((char *)func_802ABBDC_de - 0x80000000U)},
};
