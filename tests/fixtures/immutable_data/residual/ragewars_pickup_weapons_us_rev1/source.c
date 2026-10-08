#include "resident_pickup_descriptors.h"

extern s32 func_802ACB18_de(void *, void *);

/* 6 descriptors traversed by 802AB400/802AB6EC at stride 20.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D3E30..D3EA8. */
ResidentWeaponDescriptor D_800CDF50[6] = {
    {{&D_800D7070[96], 3012, 3510, 0, 0}, 1, 100, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
    {{&D_800D7070[97], 3013, 3510, 0, 0}, 1, 100, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
    {{&D_800D7070[98], 3014, 3500, 0, 0}, 0, 50, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
    {{&D_800D7070[99], 3015, 3500, 0, 0}, 0, 50, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
    {{&D_800D7070[100], 3016, 3520, 0, 0}, 2, 5, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
    {{&D_800D7070[101], 3017, 3520, 0, 0}, 2, 5, (ResidentPickupHandler)((char *)func_802ACB18_de - 0x80000000U)},
};
